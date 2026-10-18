"""
Which cashbooks a business's staff may use, and how far.

    grant(till, alice, carol_membership, 'contributor')  # Carol may add entries
    change(carol_grant, alice, 'editor')                  # ...and now change them
    revoke(carol_grant, alice)                            # Till disappears for her

Owners, admins and viewers reach every book through their role; only staff
need a grant, and only people already on the team (they joined through an
invitation, which is where seats are counted) can be given one. Whoever may
administer a book manages its grants, but only the owner or an admin can hand
out that same power.
"""
from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from workspaces.errors import refuse
from workspaces.models import Membership, Workspace
from .models import CashBookAdditionalMember

ROLES = [role for role, _ in CashBookAdditionalMember.ROLE_CHOICES]


def _check_can_manage(cashbook, actor, role=None):
    if cashbook.workspace.kind != Workspace.KIND_BUSINESS or cashbook.workspace.is_demo:
        refuse('not_a_business', 'Only a business has a team.')
    if not cashbook.has_permission(actor, 'admin'):
        raise PermissionDenied("Only someone who manages this cashbook can change who has access.")
    if role == CashBookAdditionalMember.ROLE_ADMIN and cashbook.workspace.role_of(actor) not in Membership.MANAGER_ROLES:
        raise PermissionDenied('Only the owner or an admin can let someone manage a cashbook.')


def _check_not_self(user_id, actor):
    if user_id == actor.pk:
        raise PermissionDenied("You can't change your own access.")


def staff(cashbook):
    """Every staff member of the book's business with their grant here, or None."""
    memberships = cashbook.workspace.memberships.filter(role=Membership.ROLE_STAFF).select_related('user')
    grants = {g.member_id: g for g in cashbook.cashbookadditionalmember_set.all()}
    return [(membership, grants.get(membership.user_id)) for membership in memberships.order_by('created_at')]


def grant(cashbook, actor, membership, role):
    """Give a staff member access to the book. Returns the grant."""
    _check_can_manage(cashbook, actor, role)
    _check_not_self(membership.user_id, actor)
    if membership.workspace_id != cashbook.workspace_id or membership.role != Membership.ROLE_STAFF:
        refuse('not_staff', 'Only staff of this business get access to single cashbooks.')

    with transaction.atomic():
        # Locked, so a removal or role change at the same time can't leave a stray grant
        membership = Membership.objects.select_for_update().get(pk=membership.pk)
        if membership.deleted_at is not None or membership.role != Membership.ROLE_STAFF:
            refuse('not_staff', 'Only staff of this business get access to single cashbooks.')
        if cashbook.cashbookadditionalmember_set.filter(member_id=membership.user_id).exists():
            refuse('already_granted', 'They already have access to this cashbook. Change it instead.')
        return CashBookAdditionalMember.objects.create(
            workspace=cashbook.workspace, cashbook=cashbook, member_id=membership.user_id,
            role=role, added_by=actor,
        )


def change(grant, actor, role):
    """Give a staff member another level of access. Returns the level they had."""
    _check_can_manage(grant.cashbook, actor, role)
    _check_not_self(grant.member_id, actor)
    # Taking the power to manage away is as much the managers' call as giving it
    if grant.role == CashBookAdditionalMember.ROLE_ADMIN:
        _check_can_manage(grant.cashbook, actor, CashBookAdditionalMember.ROLE_ADMIN)

    with transaction.atomic():
        grant = CashBookAdditionalMember.objects.select_for_update().get(pk=grant.pk)
        old = grant.role
        if old != role:
            grant.role = role
            grant.save(update_fields=['role'])
    return old


def revoke(grant, actor):
    """Take a staff member's access to the book away."""
    _check_can_manage(grant.cashbook, actor)
    _check_not_self(grant.member_id, actor)
    if grant.role == CashBookAdditionalMember.ROLE_ADMIN:
        _check_can_manage(grant.cashbook, actor, CashBookAdditionalMember.ROLE_ADMIN)
    grant.soft_delete()
