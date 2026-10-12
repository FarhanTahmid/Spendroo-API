"""
The questions features ask before doing something a plan may not include.

    gates.require(workspace, F.REPORTS_EXCEL)                 a flag
    gates.check_limit(workspace, F.BUSINESS_CASHBOOKS)        room for one more
    with gates.consume(user, F.AI_CREDITS, 3, key=...): ...   a quota, given back if the block fails
    gates.assert_row_writable(workspace, F.PERSONAL_ACCOUNTS, account_id)

`subject` is a user or a workspace; a workspace is judged on its owner's plan.
Every refusal is a PlanLimit: HTTP 402 with code "plan_limit". It is never a
403, which the app takes to mean its session ended and wipes the device.
"""
from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.exceptions import APIException

from workspaces.models import Workspace

from . import locks, usage
from .catalog.keys import F, FLAG
from .entitlements import cached, for_workspace, owner_of, resolve
from .models import BillingSettings, FunnelEvent, Plan, PlanFeature
from .rules import rule_for

MB = 1024 * 1024

#: Marks a view whose writes are checked against the plan (see tests/test_structure.py)
GATED = 'gated'


def exempt(reason):
    """Marks a view that takes writes no plan limits, and says why."""
    return ('exempt', reason)


REASON_FEATURE = 'feature'
REASON_LIMIT = 'limit'
REASON_QUOTA = 'quota'
REASON_LOCKED = 'locked'

MESSAGES = {
    REASON_FEATURE: '{name} is not part of the {plan} plan.',
    REASON_LIMIT: '{name}: the {plan} plan allows {limit}.',
    REASON_QUOTA: 'This month\'s {name} on the {plan} plan are used up.',
    REASON_LOCKED: 'This is read-only on the {plan} plan. Upgrade, or choose what to keep.',
}


class PlanLimit(APIException):
    status_code = 402
    default_code = 'plan_limit'

    def __init__(self, entitlements, feature, reason, *, limit=None, current=None):
        self.entitlements = entitlements
        self.feature_key = str(feature)
        self.reason = reason
        name = getattr(feature, 'name', None) or self.feature_key
        self.message = MESSAGES[reason].format(name=name, plan=entitlements.plan.name, limit=limit)
        self.meta = {
            'feature': self.feature_key,
            'reason': reason,
            'limit': limit,
            'current': current,
            'plan': entitlements.plan.code,
            'upgrade_to': upgrade_target(entitlements.plan, feature),
        }
        self.detail = {'code': self.default_code, 'detail': self.message, **self.meta}


def upgrade_target(plan, feature):
    """Code of the cheapest public plan above this one that gives more of the feature, or None."""
    key = str(feature)

    def find():
        better = Plan.objects.filter(is_public=True, is_active=True, rank__gt=plan.rank).order_by('rank')
        rows = {
            row.plan_id: row
            for row in PlanFeature.objects.filter(plan__in=better, feature__key=key).select_related('feature')
        }
        own = PlanFeature.objects.filter(plan=plan, feature__key=key).first()
        for candidate in better:
            row = rows.get(candidate.id)
            if row is None:
                continue
            if row.feature.kind == FLAG:
                if row.enabled:
                    return candidate.code
            elif row.unlimited or (row.limit or 0) > ((own.limit or 0) if own else 0):
                return candidate.code
        return None

    return cached(('upgrade_to', plan.pk, key), find)


def payer_of(subject):
    return owner_of(subject) if isinstance(subject, Workspace) else subject


def entitlements_for(subject):
    return for_workspace(subject) if isinstance(subject, Workspace) else resolve(subject)


def record_hit(user, feature_key, plan_code, enforced=True):
    FunnelEvent.objects.create(
        user=user, kind=FunnelEvent.KIND_LIMIT_HIT, feature_key=feature_key, plan_code=plan_code, enforced=enforced,
    )


def record_refusal(error, user):
    """
    Note a refusal for the dashboard. Call it after the transaction the gate
    ran in has rolled back, or the note is rolled back with it.
    """
    record_hit(user, error.feature_key, error.meta['plan'])


def _refuse(entitlements, subject, feature, reason, **numbers):
    mode = entitlements.settings.enforcement_mode
    if mode == BillingSettings.MODE_OFF:
        return
    if mode == BillingSettings.MODE_LOG_ONLY:
        record_hit(payer_of(subject), str(feature), entitlements.plan.code, enforced=False)
        return
    raise PlanLimit(entitlements, feature, reason, **numbers)


def allows(subject, feature):
    """Whether the plan includes the feature at all. For branching, not for refusing."""
    return entitlements_for(subject).can(feature)


def require(subject, feature):
    entitlements = entitlements_for(subject)
    if not entitlements.can(feature):
        _refuse(entitlements, subject, feature, REASON_FEATURE)


def _lock_for_count(model, pk):
    # Serialises racing creates. Only possible inside a transaction; callers
    # that create right after checking run both in one atomic block.
    if connection.in_atomic_block:
        model.objects.select_for_update().filter(pk=pk).values_list('pk', flat=True).first()


def check_limit(subject, feature, adding=1):
    """Refuse when `adding` more rows would go over the limit. Counts what rules.py says counts."""
    rule = rule_for(feature)
    if rule.per_workspace != isinstance(subject, Workspace):
        raise TypeError(f'{feature} is counted per {"workspace" if rule.per_workspace else "user"}.')
    entitlements = entitlements_for(subject)
    value = entitlements.value(feature)
    if value.limit is None:
        return
    owner = payer_of(subject)
    workspace = subject if rule.per_workspace else None
    if rule.per_workspace:
        _lock_for_count(Workspace, subject.pk)
    else:
        _lock_for_count(get_user_model(), owner.pk)
    current = rule.queryset(owner, workspace).count()
    if current + adding > value.limit:
        _refuse(entitlements, subject, feature, REASON_LIMIT, limit=value.limit, current=current)


def keep_state(subject, feature):
    """Which rows of a limit are kept and which are locked, for this workspace or user."""
    rule = rule_for(feature)
    entitlements = entitlements_for(subject)
    return locks.state(
        payer_of(subject), rule, subject if rule.per_workspace else None,
        entitlements.limit_of(feature), entitlements.settings.reselect_cooldown_days,
    )


def assert_row_writable(workspace, feature, row_id):
    """Refuse a write to a row the owner's plan has locked."""
    rule = rule_for(feature)
    if row_id in keep_state(workspace if rule.per_workspace else owner_of(workspace), feature).locked:
        entitlements = for_workspace(workspace)
        _refuse(entitlements, workspace, feature, REASON_LOCKED, limit=entitlements.limit_of(feature))


def assert_workspace_writable(workspace, user):
    """
    Refuse writes into a business the owner's plan has locked, and writes by a
    team member whose seat it has locked. Deletes do not come through here:
    removing things is how a user gets back under a limit.
    """
    if workspace.kind == Workspace.KIND_BUSINESS and not workspace.is_demo:
        assert_row_writable(workspace, F.BUSINESS_WORKSPACES, workspace.pk)
    if user.pk != workspace.owner_id:
        membership_id = cached(
            ('membership', workspace.pk, user.pk),
            lambda: workspace.memberships.filter(user=user).values_list('id', flat=True).first(),
        )
        if membership_id is not None:
            assert_row_writable(workspace, F.TEAM_SEATS, membership_id)


def assert_cashbook_writable(cashbook, user):
    assert_workspace_writable(cashbook.workspace, user)
    assert_row_writable(cashbook.workspace, F.BUSINESS_CASHBOOKS, cashbook.pk)


def take(subject, feature, amount, key, *, actor=None, ref=''):
    """Count `amount` of a monthly quota. Refuses when it does not fit."""
    entitlements = entitlements_for(subject)
    value = entitlements.value(feature)
    workspace = subject if isinstance(subject, Workspace) else None
    event, taken = usage.take(
        payer_of(subject), feature, amount, key, value.limit, workspace=workspace, actor=actor, ref=ref,
    )
    if not taken:
        _refuse(entitlements, subject, feature, REASON_QUOTA, limit=value.limit)
    return event


@contextmanager
def consume(subject, feature, amount, key, *, actor=None, ref=''):
    """Take from a quota for the length of the block; an exception inside gives it back."""
    take(subject, feature, amount, key, actor=actor, ref=ref)
    try:
        yield
    except BaseException:
        usage.give_back(key)
        raise


def store(workspace, size_bytes, key, *, actor=None):
    """Count an attachment against the owner's storage. Refuses when it does not fit."""
    entitlements = for_workspace(workspace)
    limit_mb = entitlements.limit_of(F.STORAGE_ATTACHMENTS_MB)
    event, taken = usage.take(
        owner_of(workspace), F.STORAGE_ATTACHMENTS_MB, max(size_bytes, 1), key,
        None if limit_mb is None else limit_mb * MB,
        period=usage.UsageCounter.LIFETIME, workspace=workspace, actor=actor,
    )
    if not taken:
        _refuse(entitlements, workspace, F.STORAGE_ATTACHMENTS_MB, REASON_LIMIT, limit=limit_mb)
    return event


def release_storage(key):
    usage.give_back(key)
