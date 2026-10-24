import pghistory
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Lower

from attachments.models import Attachment
from attachments.storage import get_bill_storage
from spendroo.merchant import MAX_LENGTH as MERCHANT_LENGTH, set_merchant
from workspaces.models import AliveManager, Membership, WorkspaceOwnedModel, hex_color


class CashBookQuerySet(models.QuerySet):
    def accessible_to(self, user):
        """
        Cashbooks the user may at least view: every book of a workspace where
        they are owner, admin or viewer, plus books granted to them individually.
        """
        return self.filter(workspace__deleted_at__isnull=True).filter(
            Q(
                workspace__memberships__user=user,
                workspace__memberships__deleted_at__isnull=True,
                workspace__memberships__role__in=[
                    Membership.ROLE_OWNER, Membership.ROLE_ADMIN, Membership.ROLE_VIEWER
                ],
            )
            | Q(
                cashbookadditionalmember__member=user,
                cashbookadditionalmember__deleted_at__isnull=True,
            )
        ).distinct()


class CashBookManager(AliveManager.from_queryset(CashBookQuerySet)):
    pass


@pghistory.track()
class CashBook(WorkspaceOwnedModel):
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='cashbooks_created'
    )

    book_name = models.CharField(null=False, blank=False, max_length=100)
    description = models.TextField(null=True, blank=True)
    # One currency per book; every entry in the book carries the same code
    currency = models.CharField(max_length=3, default='BDT')

    objects = CashBookManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = 'Cash Book'
        verbose_name_plural = 'Cash Books'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'created_at']),
        ]

    def __str__(self):
        return f"{self.book_name} - {self.workspace.name}"

    @property
    def owner(self):
        return self.workspace.owner

    def get_totals(self, entries=None):
        """Return (cash_in, cash_out) in minor units"""
        if entries is None:
            entries = self.entry_set.all()
        totals = entries.aggregate(
            cash_in=Sum('amount_minor', filter=Q(entry_type='cash_in')),
            cash_out=Sum('amount_minor', filter=Q(entry_type='cash_out')),
        )
        return totals['cash_in'] or 0, totals['cash_out'] or 0

    def soft_delete(self):
        """Tombstone the book and everything in it, so a pull carries every deletion."""
        with transaction.atomic():
            super().soft_delete()
            stamp = {'deleted_at': self.deleted_at, 'updated_at': self.deleted_at, 'version': F('version') + 1}
            for model in (Entry, EntryCategory, PaymentMethod, EntryField, CashBookAdditionalMember):
                model.all_objects.filter(cashbook=self, deleted_at__isnull=True).update(**stamp)
            # Bills also give the owner's storage back and leave the disk
            from attachments.service import discard
            from .bills import BILL
            discard(BILL, EntryBills.all_objects.filter(cashbook=self))

    def get_balance(self):
        """Calculate current balance in minor units"""
        cash_in, cash_out = self.get_totals()
        return cash_in - cash_out

    def has_permission(self, user, permission_type='view'):
        """
        Whether the user may 'view' this book, 'add' entries to it, 'edit' its
        entries and the book, or 'admin' it (categories, payment methods and
        who has access). Managers may do everything and viewers only look;
        staff get what their grant to this book says.
        """
        workspace_role = self.workspace.role_of(user)
        if workspace_role in Membership.MANAGER_ROLES:
            return True
        if workspace_role == Membership.ROLE_VIEWER:
            return permission_type == 'view'
        if workspace_role != Membership.ROLE_STAFF:
            return False

        grant = self.cashbookadditionalmember_set.filter(member=user).first()
        if not grant:
            return False
        return permission_type in CashBookAdditionalMember.PERMISSIONS.get(grant.role, ())


@pghistory.track()
class CashBookAdditionalMember(WorkspaceOwnedModel):
    """Per-book grant for workspace staff."""
    ROLE_VIEWER = 'viewer'
    ROLE_CONTRIBUTOR = 'contributor'  # adds entries, never changes them
    ROLE_EDITOR = 'editor'
    ROLE_ADMIN = 'admin'
    ROLE_CHOICES = [
        (ROLE_VIEWER, 'Viewer'),
        (ROLE_CONTRIBUTOR, 'Contributor'),
        (ROLE_EDITOR, 'Editor'),
        (ROLE_ADMIN, 'Admin'),
    ]
    #: what each grant allows, as CashBook.has_permission is asked
    PERMISSIONS = {
        ROLE_VIEWER: ('view',),
        ROLE_CONTRIBUTOR: ('view', 'add'),
        ROLE_EDITOR: ('view', 'add', 'edit'),
        ROLE_ADMIN: ('view', 'add', 'edit', 'admin'),
    }

    cashbook = models.ForeignKey(CashBook, on_delete=models.CASCADE, null=False)
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=False, blank=False)
    role = models.CharField(max_length=12, choices=ROLE_CHOICES, default=ROLE_VIEWER)

    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='members_added')

    class Meta:
        verbose_name = 'Cash Book Additional Member'
        verbose_name_plural = 'Cash Book Additional Members'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['cashbook', 'member'],
                condition=Q(deleted_at__isnull=True),
                name='one_grant_per_member_per_cashbook',
            ),
        ]

    def __str__(self):
        return f"{self.member.email} - {self.role} in {self.cashbook.book_name}"

    def get_parent_workspace_id(self):
        return self.cashbook.workspace_id


@pghistory.track()
class EntryCategory(WorkspaceOwnedModel):
    GENERAL_CATEGORIES = [
        'Housing', 'Transportation', 'Food', 'Utilities', 'Clothing', 'Medical/Healthcare',
        'Insurance', 'Household Items/Supplies', 'Personal', 'Debt', 'Retirement', 'Education',
        'Savings', 'Gifts/Donations', 'Entertainment', 'Income', 'Other'
    ]

    cashbook = models.ForeignKey(CashBook, on_delete=models.CASCADE, null=False, blank=False)
    category_name = models.CharField(max_length=100, null=False, blank=False)
    is_default = models.BooleanField(default=False)
    # Chosen by the user; without one the app picks a colour itself
    color = models.CharField(max_length=7, null=True, blank=True, validators=[hex_color])

    class Meta:
        verbose_name = "Entry Category"
        verbose_name_plural = "Entry Categories"
        constraints = [
            models.UniqueConstraint(
                fields=['cashbook', 'category_name'],
                condition=Q(deleted_at__isnull=True),
                name='one_category_name_per_cashbook',
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "cashbook", "category_name"]),
        ]
        ordering = ['category_name']

    def __str__(self):
        return f"{self.category_name} - {self.cashbook.book_name}"

    def get_parent_workspace_id(self):
        return self.cashbook.workspace_id


@pghistory.track()
class PaymentMethod(WorkspaceOwnedModel):
    GENERAL_PAYMENT_METHODS = [
        'Cash', 'Credit Card', 'Debit Card', 'bKash', 'Nagad',
        'Rocket', 'Bank Transfer', 'Online', 'Other'
    ]

    cashbook = models.ForeignKey(CashBook, on_delete=models.CASCADE, null=False, blank=False)
    payment_method_name = models.CharField(max_length=100, null=False, blank=False)
    is_default = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Payment Method"
        verbose_name_plural = "Payment Methods"
        constraints = [
            models.UniqueConstraint(
                fields=['cashbook', 'payment_method_name'],
                condition=Q(deleted_at__isnull=True),
                name='one_payment_method_name_per_cashbook',
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "cashbook", "payment_method_name"]),
        ]
        ordering = ['payment_method_name']

    def __str__(self):
        return f"{self.payment_method_name} - {self.cashbook.book_name}"

    def get_parent_workspace_id(self):
        return self.cashbook.workspace_id


@pghistory.track()
class EntryField(WorkspaceOwnedModel):
    """
    A field a business adds to every entry of one book, such as an invoice
    number or the party paid. Entries keep their values in
    Entry.custom_values, checked by cashbook/custom_fields.py.
    """
    TEXT = 'text'
    NUMBER = 'number'
    DATE = 'date'
    CHOICE = 'choice'
    KIND_CHOICES = [(TEXT, 'Text'), (NUMBER, 'Number'), (DATE, 'Date'), (CHOICE, 'Choice')]
    MAX_OPTIONS = 30
    NAME_LENGTH = 60

    cashbook = models.ForeignKey(CashBook, on_delete=models.CASCADE, related_name='fields')
    name = models.CharField(max_length=NAME_LENGTH)
    # Fixed once made: values already kept would not fit another kind
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    # What a choice field offers; empty for every other kind
    options = models.JSONField(default=list, blank=True)
    # Asked for by the app on new entries; the server never refuses an entry for a missing value
    required = models.BooleanField(default=False)
    # The one field shown on the book's entry list; the last one pinned wins (sync/registry.py)
    show_in_list = models.BooleanField(default=False)
    position = models.IntegerField(default=0)

    class Meta:
        verbose_name = 'Entry Field'
        verbose_name_plural = 'Entry Fields'
        ordering = ['position', 'created_at']
        constraints = [
            models.UniqueConstraint(
                Lower('name'), 'cashbook',
                condition=Q(deleted_at__isnull=True),
                name='one_field_name_per_cashbook',
            ),
        ]
        indexes = [
            models.Index(fields=['workspace', 'cashbook']),
        ]

    def __str__(self):
        return f"{self.name} ({self.kind}) - {self.cashbook.book_name}"

    def get_parent_workspace_id(self):
        return self.cashbook.workspace_id

    def clean(self):
        self.name = ' '.join((self.name or '').split())
        if not self.name:
            raise ValidationError({'name': 'A field needs a name.'})
        if self.kind != self.CHOICE:
            if self.options:
                raise ValidationError({'options': 'Only a choice field has options.'})
            self.options = []
            return
        if not isinstance(self.options, list) or not all(isinstance(option, str) for option in self.options):
            raise ValidationError({'options': 'Options are a list of names.'})
        cleaned, seen = [], set()
        for option in self.options:
            option = ' '.join(option.split())
            if not option or option.casefold() in seen:
                continue
            if len(option) > self.NAME_LENGTH:
                raise ValidationError({'options': f'An option is at most {self.NAME_LENGTH} characters.'})
            seen.add(option.casefold())
            cleaned.append(option)
        if not cleaned:
            raise ValidationError({'options': 'A choice field needs at least one option.'})
        if len(cleaned) > self.MAX_OPTIONS:
            raise ValidationError({'options': f'A choice field offers at most {self.MAX_OPTIONS} options.'})
        self.options = cleaned


@pghistory.track()
class Entry(WorkspaceOwnedModel):
    ENTRY_TYPES = (
        ('cash_in', 'Cash In'),
        ('cash_out', 'Cash Out'),
    )
    SOURCE_CHOICES = (
        ('manual', 'Manual'),
        ('sms', 'SMS'),
        ('email', 'Email'),
        ('upload', 'Statement upload'),
        ('ai', 'AI'),
    )

    cashbook = models.ForeignKey(CashBook, on_delete=models.CASCADE, null=False, blank=False)
    category = models.ForeignKey(EntryCategory, on_delete=models.SET_NULL, null=True, blank=True)
    payment_method = models.ForeignKey(PaymentMethod, on_delete=models.SET_NULL, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='entries_created')

    entry_type = models.CharField(null=False, blank=False, choices=ENTRY_TYPES, default='cash_out', max_length=10)
    # Always positive, in minor units of `currency`; direction comes from entry_type
    amount_minor = models.BigIntegerField(null=False, blank=False)
    currency = models.CharField(max_length=3, editable=False)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='manual')

    title = models.CharField(max_length=200, null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)
    # The party paid or paid by (spendroo/merchant.py). The key is the copy matched on; never synced.
    merchant = models.CharField(max_length=MERCHANT_LENGTH, blank=True, default='')
    merchant_key = models.CharField(max_length=MERCHANT_LENGTH, blank=True, default='', editable=False)
    # Ids of the workspace's tags (tags/cleaning.py)
    tag_ids = models.JSONField(default=list, blank=True)

    entry_date = models.DateField(null=False, blank=False)
    # The book's own fields (EntryField): {field id: value as text}, checked by cashbook/custom_fields.py
    custom_values = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Cashbook Entry"
        verbose_name_plural = "Cashbook Entries"
        ordering = ['-entry_date', '-created_at']
        indexes = [
            models.Index(fields=['workspace', 'cashbook', 'entry_date']),
            models.Index(fields=['workspace', 'merchant_key'], name='cashbook_entry_merchant'),
            models.Index(fields=['entry_type']),
            models.Index(fields=['created_by']),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name='entry_amount_positive'),
        ]

    def __str__(self):
        return f"{self.title or self.entry_type} - {self.amount_minor} {self.currency} - {self.cashbook.book_name}"

    def get_parent_workspace_id(self):
        return self.cashbook.workspace_id

    def save(self, *args, **kwargs):
        self.currency = self.cashbook.currency
        set_merchant(self, kwargs)
        super().save(*args, **kwargs)

    def clean(self):
        from .custom_fields import cleaned_shape
        self.custom_values = cleaned_shape(self.custom_values)

    def soft_delete(self):
        """Through the API or a phone's sync alike: the entry's bills go with it."""
        from attachments.service import discard
        from .bills import BILL
        with transaction.atomic():
            super().soft_delete()
            discard(BILL, EntryBills.all_objects.filter(entry=self))


def get_user_bill_filepath(instance, filename):
    entry = instance.entry
    return f"workspaces/{entry.workspace_id}/cashbooks/{entry.cashbook_id}/entries/{entry.id}/bills/{filename}"


class EntryBills(Attachment):
    """
    A photo or PDF kept with an entry as proof. Added and removed only through
    attachments/service.py (cashbook/bills.py says who may), which checks the
    file and counts it against the owner's storage. Phones get these rows by
    sync (read-only) and each file through a signed link (attachments/storage.py).
    """
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name='bills')
    # The entry's book, kept here so sync can narrow bills to the books a member sees
    cashbook = models.ForeignKey(CashBook, on_delete=models.CASCADE, related_name='bills', editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='bills_added',
    )

    bill_file = models.FileField(
        null=False,
        blank=False,
        upload_to=get_user_bill_filepath,
        storage=get_bill_storage,
        # The folder alone is about 150 characters (three ids); the default of 100 fits no file
        max_length=300,
    )

    class Meta:
        verbose_name = "Bill of Entry"
        verbose_name_plural = "Bills of Entries"
        ordering = ['created_at']

    def __str__(self):
        return f"Bill for {self.entry.title or self.entry.id}"

    def get_parent_workspace_id(self):
        return self.entry.workspace_id

    def save(self, *args, **kwargs):
        if self.cashbook_id is None:
            self.cashbook_id = self.entry.cashbook_id
        super().save(*args, **kwargs)
