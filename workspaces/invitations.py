"""
Inviting people into a business, and joining one.

    invitation, code = invite(workspace, owner, 'staff', email='bob@example.com')
    accept(code, bob)

The code is shown once (and emailed when the invitation is by email); only its
hash is stored. An invitation not taken up holds a seat until it expires, so
an owner cannot hand out more invitations than the plan has seats.
"""
import hashlib
import secrets
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from billing import gates
from billing.catalog.keys import F
from .errors import refuse
from .models import Invitation, Membership, Workspace
from .tenancy import add_workspace_to_context

# No 0/O, 1/I/L: the code is read out and typed in
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
CODE_LENGTH = 10
EXPIRES_AFTER = timedelta(days=7)


def new_code():
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(code):
    return ''.join(code.split()).replace('-', '').upper()


def hash_code(code):
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def pending(workspace):
    """Invitations of the workspace that can still be taken up."""
    return workspace.invitations.filter(accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())


def _check_can_invite(workspace, inviter, role):
    if workspace.kind != Workspace.KIND_BUSINESS or workspace.is_demo:
        refuse('not_a_business', 'People can only be invited to a business.')
    if workspace.role_of(inviter) not in Membership.MANAGER_ROLES:
        raise PermissionDenied('Only the owner or an admin can invite people.')
    if role == Membership.ROLE_ADMIN and inviter.pk != workspace.owner_id:
        raise PermissionDenied('Only the owner can invite an admin.')


def invite(workspace, inviter, role, email='', phone=''):
    """
    Invite someone by email or by phone. Returns (invitation, code). Inviting
    the same address again replaces the earlier invitation and its code.
    """
    _check_can_invite(workspace, inviter, role)
    if email and workspace.memberships.filter(user__email__iexact=email).exists():
        refuse('already_member', 'This person is already in the business.')

    with transaction.atomic():
        # Two invitations at once cannot both take the last seat
        Workspace.objects.select_for_update().filter(pk=workspace.pk).values_list('pk', flat=True).first()
        same_contact = pending(workspace).filter(email__iexact=email) if email else pending(workspace).filter(phone=phone)
        same_contact.update(revoked_at=timezone.now())
        # Each invitation not yet taken up holds a seat, as does the one being made
        gates.check_limit(workspace, F.TEAM_SEATS, adding=1 + pending(workspace).count())
        code = new_code()
        invitation = Invitation.objects.create(
            workspace=workspace, email=email, phone=phone, role=role, code_hash=hash_code(code),
            invited_by=inviter, expires_at=timezone.now() + EXPIRES_AFTER,
        )
        if email:
            _send(invitation, code)
    return invitation, code


def _send(invitation, code):
    context = {
        'workspace': invitation.workspace.name,
        'inviter': invitation.invited_by.full_name or invitation.invited_by.email,
        'role': invitation.get_role_display(),
        'code': code,
        'expires_at': invitation.expires_at,
    }
    subject = render_to_string('workspaces/email/invitation_subject.txt', context).strip()
    body = render_to_string('workspaces/email/invitation_message.txt', context)
    send_mail(settings.ACCOUNT_EMAIL_SUBJECT_PREFIX + subject, body, None, [invitation.email])


def find(code, lock=False):
    """The pending invitation with this code, or a ValidationError saying why there is none."""
    invitations = Invitation.objects.select_related('workspace', 'invited_by')
    if lock:
        invitations = invitations.select_for_update(of=('self',))
    invitation = invitations.filter(code_hash=hash_code(code or '')).first()
    if invitation is None or invitation.workspace.deleted_at is not None:
        refuse('invalid', 'This code is not valid.')
    if invitation.status == Invitation.STATUS_EXPIRED:
        refuse('expired', 'This invitation has expired. Ask for a new one.')
    if not invitation.is_pending:
        refuse('used', 'This invitation can no longer be used.')
    return invitation


def accept(code, user):
    """Join the business the code invites to. Returns the workspace."""
    with transaction.atomic():
        invitation = find(code, lock=True)
        workspace = invitation.workspace
        if invitation.email and not EmailAddress.objects.filter(
            user=user, email__iexact=invitation.email, verified=True,
        ).exists():
            refuse('wrong_account', f'This invitation is for {invitation.email}. Sign in with that address.')
        if workspace.memberships.filter(user=user).exists():
            refuse('already_member', 'You are already in this business.')
        # The seat was held while the invitation waited; a downgrade since may have taken it
        gates.check_limit(workspace, F.TEAM_SEATS)
        Membership.objects.create(workspace=workspace, user=user, role=invitation.role, added_by=invitation.invited_by)
        invitation.accepted_at = timezone.now()
        invitation.accepted_by = user
        invitation.save(update_fields=['accepted_at', 'accepted_by'])
    add_workspace_to_context(workspace.id)
    return workspace


def revoke(invitation, user):
    if invitation.workspace.role_of(user) not in Membership.MANAGER_ROLES:
        raise PermissionDenied('Only the owner or an admin can cancel an invitation.')
    if not invitation.is_pending:
        refuse('used', 'This invitation can no longer be used.')
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=['revoked_at'])
