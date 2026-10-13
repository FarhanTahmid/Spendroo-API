"""
Works out when rules fall due, and records what is to be recorded.

A rule has at most one `upcoming` occurrence: the next date it falls on. When
that day comes (in the owner's time zone) the occurrence becomes `due`, or
`done` if the rule records by itself, and the one after it is made. So the
upcoming occurrence is the cursor: nothing is stored about how far a rule has
got that the occurrences do not already say, and running this twice changes
nothing the second time.

Dates before a rule was made, resumed or changed are never filled in. Days
the server was not running are caught up, a few at a time.
"""
import logging
import threading
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from billing import gates
from billing.catalog.keys import F
from ledger_personal.allocations import categories_of
from ledger_personal.models import Transaction

from . import budgets, inbox, schedule
from .models import Occurrence, Rule

logger = logging.getLogger(__name__)

#: Dates one rule may fall due on in one run; the rest wait for the next
MAX_STEPS = 24
#: A rule further behind than this skips ahead instead of catching up day by day
CATCH_UP_DAYS = 60
#: The longest a reminder comes before its date (Rule.remind_days_before)
MAX_LEAD_DAYS = 30

_state = threading.local()


def _is_recording():
    return getattr(_state, 'recording', False)


def is_active(rule):
    """Whether the rule is to fall due at all: not paused, and within the owner's plan."""
    if rule.deleted_at is not None or rule.is_paused:
        return False
    return rule.is_reminder or gates.allows(rule.workspace, F.RECURRING_TRANSACTIONS)


def _alive(row):
    return row is not None and row.deleted_at is None


def _record(rule, occurrence):
    """
    The transactions the rule stands for, dated the day it fell due. None when
    they cannot be recorded now: an account was archived, deleted or locked.
    """
    workspace = rule.workspace
    accounts = [rule.account] + ([rule.to_account] if rule.kind == 'transfer' else [])
    if any(not _alive(account) or account.is_archived for account in accounts):
        return None
    try:
        for account in accounts:
            gates.assert_row_writable(workspace, F.PERSONAL_ACCOUNTS, account.id)
    except gates.PlanLimit:
        return None

    note = rule.note or rule.title
    common = {
        'workspace': workspace, 'occurred_on': occurrence.due_on, 'note': note, 'source': 'recurring',
        'occurrence': occurrence,
    }
    if rule.kind == 'transfer':
        group = uuid.uuid4()
        rows = [
            Transaction(account=account, kind='transfer', amount_minor=signed, transfer_group_id=group, **common)
            for account, signed in zip(accounts, (-rule.amount_minor, rule.amount_minor))
        ]
    else:
        alive = {str(category_id) for category_id in categories_of(workspace.id)}
        # A category deleted since the rule was written reads as none
        splits = [
            {**part, 'category_id': part['category_id'] if part['category_id'] in alive else None}
            for part in rule.splits
        ]
        rows = [Transaction(
            account=rule.account, kind=rule.kind, category=rule.category if _alive(rule.category) else None,
            amount_minor=-rule.amount_minor if rule.kind == 'expense' else rule.amount_minor, splits=splits, **common,
        )]
    if hasattr(_state, 'recorded'):
        _state.recorded.add(workspace.id)
    _state.recording = True
    try:
        for row in rows:
            row.currency = row.account.currency
            row.full_clean(exclude=['workspace'])
            row.save()
    finally:
        _state.recording = False
    return rows


def _fall_due(rule, occurrence, now):
    """The day has come: recorded at once, or handed to the user."""
    rows = _record(rule, occurrence) if rule.mode == Rule.MODE_AUTO and not rule.is_reminder else None
    occurrence.status = Occurrence.DONE if rows else Occurrence.DUE
    occurrence.save(update_fields=['status'])
    params = inbox.describe(rule, occurrence)
    if rows:
        inbox.tell(
            rule.workspace, 'posted', f'occ:{occurrence.id}:posted', params, inbox.TRANSACTIONS, rows[0].id, now,
            resolved=True,
        )
    elif rule.mode == Rule.MODE_AUTO and not rule.is_reminder:
        inbox.tell(
            rule.workspace, 'rule_blocked', f'occ:{occurrence.id}:blocked', params, inbox.OCCURRENCES, occurrence.id, now,
        )


def _next(rule, since):
    """The upcoming occurrence on or after `since`, made now; None when the rule has run its course."""
    generated = rule.occurrences.exclude(status=Occurrence.UPCOMING)
    if rule.max_count is not None and generated.count() >= rule.max_count:
        return None
    day = schedule.next_on_or_after(rule, since)
    if day is None:
        return None
    return Occurrence.objects.create(workspace=rule.workspace, rule=rule, due_on=day, status=Occurrence.UPCOMING)


def _drop(occurrence, now):
    occurrence.soft_delete()
    inbox.resolve(occurrence.id, now)


def advance(rule_id, now=None):
    """Brings one rule up to date. Safe to call at any time, any number of times."""
    now = now or timezone.now()
    with transaction.atomic():
        rule = Rule.all_objects.select_for_update().select_related('workspace').filter(pk=rule_id).first()
        if rule is None:
            return
        today = inbox.today_in(rule.workspace, now)
        waiting = list(rule.occurrences.filter(status=Occurrence.UPCOMING).order_by('due_on'))

        if rule.deleted_at is not None:
            for occurrence in rule.occurrences.filter(status__in=Occurrence.OPEN):
                _drop(occurrence, now)
            return
        active = is_active(rule)
        if rule.needs_refresh or not active:
            # Its dates moved, or it is to rest: what was coming no longer is
            for occurrence in waiting:
                _drop(occurrence, now)
            waiting = []
        ended = False
        upcoming = waiting[0] if waiting else None
        if active:
            steps = 0
            while True:
                if upcoming is None:
                    last = rule.occurrences.exclude(status=Occurrence.UPCOMING).order_by('-due_on').first()
                    # Never a date that already has an occurrence, and never one gone by
                    since = max(today, rule.start_on, last.due_on + timedelta(days=1) if last else today)
                    upcoming = _next(rule, since)
                    if upcoming is None:
                        ended = True
                        break
                if upcoming.due_on > today or steps >= MAX_STEPS:
                    break
                if upcoming.due_on < today - timedelta(days=CATCH_UP_DAYS):
                    # Too far behind to be worth telling the user about each one
                    _drop(upcoming, now)
                    upcoming = _next(rule, today - timedelta(days=CATCH_UP_DAYS))
                    if upcoming is None:
                        ended = True
                        break
                    continue
                _fall_due(rule, upcoming, now)
                # Days missed while the server was down are caught up one after another
                following = _next(rule, upcoming.due_on + timedelta(days=1))
                steps += 1
                if following is None:
                    upcoming, ended = None, True
                    break
                upcoming = following

        state = {
            'next_due_on': upcoming.due_on if upcoming else None,
            'done_count': rule.occurrences.filter(status=Occurrence.DONE).count(),
            'ended': ended,
            'needs_refresh': False,
        }
        changed = [name for name, value in state.items() if getattr(rule, name) != value]
        if changed:
            for name in changed:
                setattr(rule, name, state[name])
            # Only when something moved: every save sends the rule to the app again
            rule.save(update_fields=changed)


def refresh(rule_id):
    """A rule was written or changed by the app: its dates are worked out now, not at the next run."""
    advance(rule_id)


def settled(occurrence_id, now=None):
    """An occurrence was recorded, skipped or put off: tidy up after it and line up the next."""
    now = now or timezone.now()
    occurrence = Occurrence.all_objects.select_related('rule', 'workspace').filter(pk=occurrence_id).first()
    if occurrence is None:
        return
    if occurrence.status not in Occurrence.OPEN or occurrence.deleted_at is not None:
        inbox.resolve(occurrence.id, now)
    advance(occurrence.rule_id, now)


def transaction_saved(row):
    """
    A transaction that settles an occurrence arrived (the user confirmed it,
    perhaps ahead of its day) or was deleted.
    """
    if row.occurrence_id is None or _is_recording():
        return
    occurrence = Occurrence.all_objects.filter(pk=row.occurrence_id).first()
    if occurrence is None:
        return
    if row.deleted_at is None:
        if occurrence.status in Occurrence.OPEN and occurrence.deleted_at is None:
            occurrence.status = Occurrence.DONE
            occurrence.save(update_fields=['status'])
            settled(occurrence.id)
    elif occurrence.status == Occurrence.DONE and not occurrence.transactions.exists():
        # Taken back: it is not asked about again
        occurrence.status = Occurrence.SKIPPED
        occurrence.save(update_fields=['status'])
        settled(occurrence.id)


def retry(occurrence, now):
    """An auto rule that could not record on the day tries again."""
    rule = occurrence.rule
    if rule.mode != Rule.MODE_AUTO or rule.is_reminder or not is_active(rule):
        return
    with transaction.atomic():
        locked = Occurrence.objects.select_for_update().filter(pk=occurrence.pk, status=Occurrence.DUE).first()
        if locked is None:
            return
        rows = _record(rule, locked)
        if not rows:
            return
        locked.status = Occurrence.DONE
        locked.save(update_fields=['status'])
        inbox.resolve(locked.id, now)
        inbox.tell(
            rule.workspace, 'posted', f'occ:{locked.id}:posted', inbox.describe(rule, locked),
            inbox.TRANSACTIONS, rows[0].id, now, resolved=True,
        )
    advance(rule.id, now)


def run(now=None):
    """
    One pass over everything: rules whose day may have come, then what there
    is to tell the user. Works from what the database holds, not from when it
    was last run, so a late or missed run loses nothing.
    """
    now = now or timezone.now()
    # Workspaces something was recorded in: their budgets are looked at afterwards
    _state.recorded = set()
    # A day begins up to a day earlier east of the server than in UTC
    horizon = now.date() + timedelta(days=1)
    to_advance = Rule.all_objects.filter(
        Q(needs_refresh=True)
        | Q(deleted_at__isnull=True, is_paused=False, ended=False)
        & (Q(next_due_on__isnull=True) | Q(next_due_on__lte=horizon))
    ).values_list('id', flat=True)
    for rule_id in list(to_advance):
        try:
            advance(rule_id, now)
        except Exception:
            logger.exception('Recurring rule %s could not be advanced', rule_id)

    waiting = Occurrence.objects.filter(
        Q(status=Occurrence.DUE)
        | Q(status=Occurrence.UPCOMING, due_on__lte=horizon + timedelta(days=MAX_LEAD_DAYS)),
        rule__deleted_at__isnull=True,
    ).select_related('rule', 'workspace')
    for occurrence in waiting:
        try:
            if occurrence.status == Occurrence.DUE:
                retry(occurrence, now)
                occurrence.refresh_from_db()
            inbox.remind(occurrence, now)
        except Exception:
            logger.exception('Occurrence %s could not be looked at', occurrence.pk)

    recorded, _state.recorded = _state.recorded, set()
    for workspace_id in recorded:
        try:
            budgets.evaluate(workspace_id, now)
        except Exception:
            logger.exception('Budgets of workspace %s could not be looked at', workspace_id)
    del _state.recorded
