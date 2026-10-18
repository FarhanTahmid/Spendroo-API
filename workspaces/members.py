"""
Changing what a team member may do, taking someone out of a business, and
handing the business to someone else.

    change_role(shop, alice, bob_membership, 'viewer')
    remove(shop, alice, bob_membership)       # or bob himself, to leave
    transfer_ownership(shop, alice, carol_membership)

Access is checked against the membership on every request (the workspace
list, sync, cashbooks and row-level security), so a removed member loses the
business on their next request without being signed out of Spendroo. The app
drops the business's rows when it leaves their workspace list.

A business has one owner. The owner can't be removed or demoted; they hand
the business to an admin first, and stay on as an admin.
"""
from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from billing import gates, locks
from billing.catalog.keys import F
from billing.entitlements import forget, forget_counts
from billing.rules import rule_for
from sync.guard import tombstone
from . import invitations
from .errors import refuse
from .models import Membership, Workspace


def _check_business(workspace):
    if workspace.kind != Workspace.KIND_BUSINESS or workspace.is_demo:
        refuse('not_a_business', 'Only a business has a team.')


def _check_can_manage(workspace, actor, membership):
    """The owner manages everyone else; an admin manages staff and viewers."""
    actor_role = workspace.role_of(actor)
    if actor_role not in Membership.MANAGER_ROLES:
        raise PermissionDenied('Only the owner or an admin can manage the team.')
    if actor_role == Membership.ROLE_ADMIN and membership.role == Membership.ROLE_ADMIN:
        raise PermissionDenied('Only the owner can change or remove an admin.')


def _locked(membership):
    return Membership.objects.select_for_update().get(pk=membership.pk)


def change_role(workspace, actor, membership, role):
    """Give a member another role. Returns the role they had."""
    _check_business(workspace)
    if membership.role == Membership.ROLE_OWNER:
        refuse('owner_role', "The owner's role can't be changed. Transfer ownership instead.")
    if membership.user_id == actor.pk:
        raise PermissionDenied("You can't change your own role.")
    _check_can_manage(workspace, actor, membership)
    if role == Membership.ROLE_ADMIN and actor.pk != workspace.owner_id:
        raise PermissionDenied('Only the owner can make someone an admin.')

    # Imported here: cashbook depends on workspaces, not the other way round
    from cashbook.models import CashBookAdditionalMember

    with transaction.atomic():
        membership = _locked(membership)
        old = membership.role
        if old != role:
            membership.role = role
            membership.save(update_fields=['role'])
            # Grants to single books are for staff; anyone else reaches books through their role
            if old == Membership.ROLE_STAFF:
                tombstone(CashBookAdditionalMember.objects.filter(workspace=workspace, member_id=membership.user_id))
    return old


def remove(workspace, actor, membership):
    """
    Take a member out of the business, or let them leave it. Their grants to
    single cashbooks go too, so being invited back starts afresh.
    """
    # Imported here: cashbook depends on workspaces, not the other way round
    from cashbook.models import CashBookAdditionalMember

    _check_business(workspace)
    if membership.role == Membership.ROLE_OWNER:
        refuse('owner', "The owner can't be removed. Transfer ownership first.")
    if membership.user_id != actor.pk:
        _check_can_manage(workspace, actor, membership)

    with transaction.atomic():
        membership = _locked(membership)
        membership.soft_delete()
        tombstone(CashBookAdditionalMember.objects.filter(workspace=workspace, member_id=membership.user_id))
    forget_counts()


def transfer_ownership(workspace, actor, membership):
    """
    Hand the business to one of its admins; the owner stays on as an admin.
    From then on it runs on the new owner's plan, so that plan must have
    room for it: refused (402) if it can't hold another business, the team
    or the cashbooks.
    """
    if actor.pk != workspace.owner_id:
        raise PermissionDenied('Only the owner can hand over the business.')
    _check_business(workspace)
    if membership.role != Membership.ROLE_ADMIN:
        refuse('not_admin', 'Make them an admin first; only an admin can take over the business.')

    old_owner = workspace.owner
    with transaction.atomic():
        workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        membership = _locked(membership)
        new_owner = membership.user
        gates.check_limit(new_owner, F.BUSINESS_WORKSPACES)

        workspace.owner = new_owner
        workspace.save(update_fields=['owner'])
        membership.role = Membership.ROLE_OWNER
        membership.save(update_fields=['role'])
        former = Membership.objects.select_for_update().get(workspace=workspace, user=old_owner)
        former.role = Membership.ROLE_ADMIN
        former.save(update_fields=['role'])
        # Plans and counts were worked out for the old owner earlier in this request
        forget()

        # The old owner now takes a seat and the new one gives theirs up; invitations waiting hold one each
        gates.check_limit(workspace, F.TEAM_SEATS, adding=invitations.pending(workspace).count())
        gates.check_limit(workspace, F.BUSINESS_CASHBOOKS, adding=0)
        # Rows the old owner chose to keep under their plan mean nothing under the new one
        for feature in (F.TEAM_SEATS, F.BUSINESS_CASHBOOKS):
            locks.reset(old_owner, rule_for(feature), workspace)
    return workspace
