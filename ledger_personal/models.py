import pghistory
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q

from attachments.models import Attachment
from attachments.storage import get_bill_storage
from spendroo.merchant import MAX_LENGTH as MERCHANT_LENGTH, set_merchant
from workspaces.models import WorkspaceOwnedModel, hex_color


class Account(WorkspaceOwnedModel):
    """Where personal money sits: a wallet, a bank account, a bKash balance."""
    KIND_CHOICES = [
        ('cash', 'Cash'),
        ('bank', 'Bank'),
        ('savings', 'Savings'),
        ('mobile_money', 'Mobile money'),
        ('card', 'Card'),
        # Holds the money of one savings goal; never offered as a place to spend from
        ('goal', 'Goal'),
    ]
    KIND_GOAL = 'goal'

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='cash')
    # One currency per account; every transaction on it carries the same code
    currency = models.CharField(max_length=3, default='BDT')
    opening_balance_minor = models.BigIntegerField(default=0)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.name} ({self.currency})"

    @property
    def is_goal(self):
        return self.kind == self.KIND_GOAL


class Category(WorkspaceOwnedModel):
    KIND_CHOICES = [
        ('expense', 'Expense'),
        ('income', 'Income'),
    ]
    DEFAULT_EXPENSE = [
        'Food', 'Transport', 'Housing', 'Utilities', 'Health', 'Education',
        'Shopping', 'Entertainment', 'Mobile & Internet', 'Gifts & Donations', 'Other',
    ]
    DEFAULT_INCOME = ['Salary', 'Business', 'Gift', 'Other income']
    # The starting categories each get a colour of their own, from the palette
    # the app offers, so no two of them look alike in a chart
    DEFAULT_COLORS = {
        'Food': '#F08A3C', 'Transport': '#3B9EE5', 'Housing': '#2F6FD0', 'Utilities': '#F5B530',
        'Health': '#D9485F', 'Education': '#7A5AF8', 'Shopping': '#E05C9A', 'Entertainment': '#B65FD6',
        'Mobile & Internet': '#12A5C4', 'Gifts & Donations': '#E8705F', 'Other': '#6B7C93',
        'Salary': '#2DB86F', 'Business': '#1FA6A0', 'Gift': '#8BBF3F', 'Other income': '#A9793E',
    }

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='expense')
    # One of the categories every account starts with. Set by the server only;
    # these do not count toward a plan's limit on custom categories.
    is_default = models.BooleanField(default=False, editable=False)
    # A starting category begins with its DEFAULT_COLORS entry; otherwise chosen
    # by the user, and without one the app picks a colour itself
    color = models.CharField(max_length=7, null=True, blank=True, validators=[hex_color])

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.kind})"


@pghistory.track()
class Transaction(WorkspaceOwnedModel):
    KIND_CHOICES = [
        ('expense', 'Expense'),
        ('income', 'Income'),
        ('transfer', 'Transfer'),
    ]
    SOURCE_CHOICES = [
        ('manual', 'Manual'),
        ('sms', 'SMS'),
        ('email', 'Email'),
        ('upload', 'Statement upload'),
        ('ai', 'AI'),
        # Recorded by the server from a recurring rule
        ('recurring', 'Recurring'),
    ]

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='transactions')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='expense')
    # Signed, in minor units of `currency`: money into the account is positive
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, editable=False)
    # The two legs of a transfer between own accounts share this id
    transfer_group_id = models.UUIDField(null=True, blank=True)
    occurred_on = models.DateField()
    note = models.CharField(max_length=200, blank=True, default='')
    # Who was paid, or who paid (spendroo/merchant.py). The key is the copy matched on; never synced.
    merchant = models.CharField(max_length=MERCHANT_LENGTH, blank=True, default='')
    merchant_key = models.CharField(max_length=MERCHANT_LENGTH, blank=True, default='', editable=False)
    # Ids of the workspace's tags (tags/cleaning.py)
    tag_ids = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='manual')
    # Filed under several categories at once: parts that add up to the amount
    # (allocations.py). While there are any, `category` is left empty.
    splits = models.JSONField(default=list, blank=True)
    # The date of a recurring rule this settles (reminders/). Both legs of a transfer carry it.
    occurrence = models.ForeignKey(
        'reminders.Occurrence', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions',
    )

    class Meta:
        ordering = ['-occurred_on', '-created_at']
        indexes = [
            # An account read newest first, a page at a time: the whole sort order
            models.Index(
                fields=['workspace', 'account', 'occurred_on', 'created_at', 'id'], name='ledger_transaction_feed',
            ),
            models.Index(fields=['workspace', 'category', 'occurred_on']),
            models.Index(fields=['workspace', 'merchant_key'], name='ledger_transaction_merchant'),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(amount_minor=0), name='transaction_amount_not_zero'),
            models.CheckConstraint(
                condition=(
                    Q(kind='expense', amount_minor__lt=0, transfer_group_id__isnull=True)
                    | Q(kind='income', amount_minor__gt=0, transfer_group_id__isnull=True)
                    | Q(kind='transfer', transfer_group_id__isnull=False)
                ),
                name='transaction_sign_matches_kind',
            ),
            # An occurrence is settled once: confirming it on two phones keeps the first
            models.UniqueConstraint(
                fields=['occurrence'], condition=Q(deleted_at__isnull=True) & ~Q(kind='transfer'),
                name='one_transaction_per_occurrence',
            ),
            models.UniqueConstraint(
                fields=['occurrence'], condition=Q(deleted_at__isnull=True, kind='transfer', amount_minor__lt=0),
                name='one_transfer_out_per_occurrence',
            ),
            models.UniqueConstraint(
                fields=['occurrence'], condition=Q(deleted_at__isnull=True, kind='transfer', amount_minor__gt=0),
                name='one_transfer_in_per_occurrence',
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.amount_minor} {self.currency} on {self.occurred_on}"

    def get_parent_workspace_id(self):
        return self.account.workspace_id

    def clean(self):
        from .allocations import cleaned
        self.splits = cleaned(self.splits, self.kind, self.amount_minor)
        # A transfer is two rows sharing transfer_group_id: money leaves one
        # account and arrives in another. Each leg is pushed on its own, so the
        # pair is checked against whichever leg is already stored.
        if self.kind != 'transfer' or self.transfer_group_id is None:
            return
        others = Transaction.objects.filter(transfer_group_id=self.transfer_group_id).exclude(id=self.id)
        if others.count() >= 2:
            raise ValidationError('A transfer has exactly two legs.')
        for other in others:
            if other.workspace_id != self.workspace_id:
                raise ValidationError('Both legs of a transfer must be in the same workspace.')
            if (other.amount_minor > 0) == (self.amount_minor > 0):
                raise ValidationError('The legs of a transfer must move money in opposite directions.')

    def save(self, *args, **kwargs):
        self.currency = self.account.currency
        if self.splits:
            self.category = None
        set_merchant(self, kwargs)
        super().save(*args, **kwargs)

    def soft_delete(self):
        """Through the API or a phone's sync alike: the transaction's receipts go with it."""
        from attachments.service import discard
        from .receipts import RECEIPT
        with transaction.atomic():
            super().soft_delete()
            discard(RECEIPT, TransactionReceipt.all_objects.filter(transaction=self))


def receipt_path(instance, filename):
    txn = instance.transaction
    return f"workspaces/{txn.workspace_id}/transactions/{txn.id}/receipts/{filename}"


class TransactionReceipt(Attachment):
    """
    A photo or PDF of the receipt of an expense or income. Added and removed
    only through attachments/service.py (ledger_personal/receipts.py says
    when), which checks the file and counts it against the owner's storage,
    the same room business bills take. Phones get these rows by sync
    (read-only) and each file through a signed link (attachments/storage.py).
    """
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='receipts')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='receipts_added',
    )
    receipt_file = models.FileField(
        upload_to=receipt_path,
        storage=get_bill_storage,
        # The folder alone is about 120 characters (two ids); the default of 100 fits no file
        max_length=300,
    )

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Receipt for {self.transaction}"

    def get_parent_workspace_id(self):
        return self.transaction.workspace_id


class Budget(WorkspaceOwnedModel):
    """
    Spending limit for one expense category. With no month it applies to
    every month; with a month (its first day) it overrides that month only.
    """
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='budgets')
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='BDT')
    month = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.CheckConstraint(condition=Q(amount_minor__gt=0), name='budget_amount_positive'),
            models.CheckConstraint(condition=Q(month__isnull=True) | Q(month__day=1), name='budget_month_first_day'),
            models.UniqueConstraint(
                fields=['category'], condition=Q(deleted_at__isnull=True, month__isnull=True),
                name='one_recurring_budget_per_category',
            ),
            models.UniqueConstraint(
                fields=['category', 'month'], condition=Q(deleted_at__isnull=True, month__isnull=False),
                name='one_budget_per_category_month',
            ),
        ]

    def __str__(self):
        period = self.month.strftime('%Y-%m') if self.month else 'monthly'
        return f"{self.category.name} {period}: {self.amount_minor} {self.currency}"

    def get_parent_workspace_id(self):
        return self.category.workspace_id

    def clean(self):
        if self.month is not None and self.month.day != 1:
            raise ValidationError({'month': 'Use the first day of the month.'})
        if self.category_id is not None and self.category.kind != 'expense':
            raise ValidationError({'category': 'Budgets apply to expense categories only.'})


@pghistory.track()
class SavingsGoal(WorkspaceOwnedModel):
    """
    Money put aside for something. The money sits in an account of its own
    (kind 'goal'): putting money in or taking it out is a transfer, so the
    account it came from shows less to spend and nothing is counted twice.
    """
    ACTIVE = 'active'
    REACHED = 'reached'
    ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (ACTIVE, 'Active'),
        (REACHED, 'Reached'),
        (ARCHIVED, 'Archived'),
    ]
    #: Percentages of the target that are celebrated, in the order of their bits in milestones_hit
    MILESTONES = (25, 50, 75, 100)

    account = models.OneToOneField(Account, on_delete=models.PROTECT, related_name='goal')
    name = models.CharField(max_length=100)
    target_minor = models.BigIntegerField()
    # The account's, copied on save
    currency = models.CharField(max_length=3, editable=False)
    target_on = models.DateField(null=True, blank=True)
    # A name from the app's goal icon set
    icon = models.CharField(max_length=40, blank=True, default='')
    color = models.CharField(max_length=7, null=True, blank=True, validators=[hex_color])
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=ACTIVE)
    # Set by the server the first time the saved amount reaches the target
    reached_at = models.DateTimeField(null=True, blank=True, editable=False)
    # One bit per MILESTONES entry already told to the user, so each is celebrated once
    milestones_hit = models.PositiveSmallIntegerField(default=0, editable=False)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.CheckConstraint(condition=Q(target_minor__gt=0), name='goal_target_positive'),
        ]

    def __str__(self):
        return f"{self.name} ({self.target_minor} {self.currency})"

    def get_parent_workspace_id(self):
        return self.account.workspace_id

    def clean(self):
        if self.account_id is not None and not self.account.is_goal:
            raise ValidationError({'account': 'A goal keeps its money in an account of kind goal.'})

    def save(self, *args, **kwargs):
        self.currency = self.account.currency
        super().save(*args, **kwargs)
