import pghistory
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from spendroo.ids import uuid7

# A colour the user picked for something of theirs, as '#RRGGBB'
hex_color = RegexValidator(r'^#[0-9A-Fa-f]{6}\Z', 'Enter a colour as #RRGGBB.')


class AliveManager(models.Manager):
    """Default manager: hides tombstoned rows."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SyncModel(models.Model):
    """
    Base for every row a client can sync.
    - id: UUIDv7, so clients can create rows offline and ids sort by age
    - version: bumped on every save
    - deleted_at: tombstone; rows are never hard-deleted by the API
    """
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = AliveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def save(self, *args, **kwargs):
        if not self._state.adding:
            self.version += 1
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'version', 'updated_at'}
        super().save(*args, **kwargs)

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.save(update_fields=['deleted_at'])

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=['deleted_at'])


class WorkspaceOwnedModel(SyncModel):
    """
    Base for every tenant-owned row. Subclasses that hang off a parent row
    implement get_parent_workspace_id() so the tenant key is filled in on save.
    """
    workspace = models.ForeignKey(
        'workspaces.Workspace', on_delete=models.CASCADE,
        related_name='%(app_label)s_%(class)s_set', editable=False,
    )
    # Change cursor for sync. Assigned by a database trigger on every insert
    # and update (see sync/sql.py), so the value held in Python may be stale.
    server_seq = models.BigIntegerField(default=0, editable=False)

    class Meta:
        abstract = True

    def get_parent_workspace_id(self):
        return None

    def save(self, *args, **kwargs):
        if self.workspace_id is None:
            self.workspace_id = self.get_parent_workspace_id()
        super().save(*args, **kwargs)


class Workspace(SyncModel):
    """The tenant. A personal space has one owner; a business has memberships."""
    KIND_PERSONAL = 'personal'
    KIND_BUSINESS = 'business'
    KIND_CHOICES = [
        (KIND_PERSONAL, 'Personal'),
        (KIND_BUSINESS, 'Business'),
    ]

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_PERSONAL)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='owned_workspaces')
    default_currency = models.CharField(max_length=3, default='BDT')
    # Sample business created during onboarding; safe to throw away
    is_demo = models.BooleanField(default=False)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['owner'],
                condition=models.Q(kind='personal', deleted_at__isnull=True),
                name='one_personal_workspace_per_user',
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.kind})"

    def role_of(self, user):
        """Return the user's role in this workspace, or None if not a member."""
        membership = self.memberships.filter(user=user).first()
        return membership.role if membership else None


@pghistory.track()
class Membership(SyncModel):
    ROLE_OWNER = 'owner'
    ROLE_ADMIN = 'admin'
    ROLE_STAFF = 'staff'
    ROLE_VIEWER = 'viewer'
    ROLE_CHOICES = [
        (ROLE_OWNER, 'Owner'),
        (ROLE_ADMIN, 'Admin'),
        (ROLE_STAFF, 'Staff'),    # access only to books granted individually
        (ROLE_VIEWER, 'Viewer'),  # read-only on every book
    ]
    MANAGER_ROLES = (ROLE_OWNER, ROLE_ADMIN)

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='memberships')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_STAFF)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='memberships_added'
    )

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'user'],
                condition=models.Q(deleted_at__isnull=True),
                name='one_membership_per_user_per_workspace',
            ),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.role} in {self.workspace.name}"


@pghistory.track()
class Invitation(models.Model):
    """
    An owner or admin asks someone to join a business with a role. The person
    joins by entering the code; only its hash is kept. An invitation by email
    can only be taken up by the account with that verified address; one by
    phone by whoever the owner gave the code to.
    """
    ROLE_CHOICES = [choice for choice in Membership.ROLE_CHOICES if choice[0] != Membership.ROLE_OWNER]

    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_REVOKED = 'revoked'
    STATUS_EXPIRED = 'expired'

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='invitations')
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=Membership.ROLE_STAFF)
    code_hash = models.CharField(max_length=64, unique=True, editable=False)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='invitations_sent'
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='invitations_accepted'
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(email='') & ~models.Q(phone='')) | (~models.Q(email='') & models.Q(phone='')),
                name='invitation_has_email_or_phone',
            ),
        ]

    def __str__(self):
        return f"{self.email or self.phone} as {self.role} in {self.workspace.name}"

    @property
    def status(self):
        if self.accepted_at is not None:
            return self.STATUS_ACCEPTED
        if self.revoked_at is not None:
            return self.STATUS_REVOKED
        if self.expires_at <= timezone.now():
            return self.STATUS_EXPIRED
        return self.STATUS_PENDING

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING
