"""
Tells the user how their savings goals are doing: each quarter of the way
once, the goal reached once, falling behind its date once a week at most, and
nothing put in for a month once a month at most.

Milestones are looked at after transactions or goals change; pace and quiet
spells once a day.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ledger_personal import goals as goal_math
from ledger_personal.models import SavingsGoal
from workspaces.models import Workspace

from . import engine, inbox
from .models import Rule

GOALS = 'savings_goals'
#: Days without money going in before the user is nudged
IDLE_AFTER_DAYS = 30


def _params(goal, progress, **extra):
    return {
        'title': goal.name, 'goal_id': str(goal.id), 'currency': goal.currency,
        'amount_minor': goal.target_minor, 'saved_minor': progress.saved_minor,
        'percent': progress.percent, 'remaining_minor': progress.remaining_minor, **extra,
    }


def _pause_rules_into(goal):
    """A goal that is reached stops taking money on a schedule; the user can start it again."""
    for rule in Rule.objects.filter(to_account=goal.account_id, is_paused=False).exclude(kind=Rule.KIND_REMINDER):
        rule.is_paused = True
        rule.needs_refresh = True
        rule.save(update_fields=['is_paused', 'needs_refresh'])
        transaction.on_commit(lambda rule_id=rule.pk: engine.refresh(rule_id), robust=True)


def check_milestones(goal, progress, now):
    """Says each quarter of the way once, and marks the goal reached the first time it is."""
    earned = goal_math.milestones_passed(progress.percent)
    new = earned & ~goal.milestones_hit
    changed = []
    if new:
        highest = max(
            mark for position, mark in enumerate(SavingsGoal.MILESTONES) if new & (1 << position)
        )
        # Only the highest one passed: money that jumps from 10% to 80% is one piece of news
        kind = 'goal_reached' if highest >= 100 else 'goal_milestone'
        inbox.tell(
            goal.workspace, kind, f'goal:{goal.id}:{highest}', _params(goal, progress, mark=highest),
            GOALS, goal.id, now, resolved=True,
        )
        goal.milestones_hit |= new
        changed.append('milestones_hit')
    if progress.percent >= 100 and goal.status == SavingsGoal.ACTIVE:
        goal.status = SavingsGoal.REACHED
        goal.reached_at = now
        changed += ['status', 'reached_at']
        _pause_rules_into(goal)
    if changed:
        goal.save(update_fields=changed)


def check_pace(goal, progress, today, now):
    """Falling behind the date, and a long spell with nothing put in."""
    if goal.status != SavingsGoal.ACTIVE:
        return
    if progress.pace == goal_math.BEHIND and goal.target_on and today < goal.target_on:
        year, week, _ = today.isocalendar()
        inbox.tell(
            goal.workspace, 'goal_behind', f'goal:{goal.id}:behind:{year}-{week:02d}',
            _params(
                goal, progress, behind_minor=progress.expected_minor - progress.saved_minor,
                needed_minor=progress.needed_per_month_minor, target_on=goal.target_on.isoformat(),
            ),
            GOALS, goal.id, now, resolved=True,
        )
        return  # one nudge at a time
    last = goal_math.last_contribution_on(goal) or goal.created_at.date()
    if today - last >= timedelta(days=IDLE_AFTER_DAYS):
        inbox.tell(
            goal.workspace, 'goal_idle', f'goal:{goal.id}:idle:{today:%Y-%m}',
            _params(goal, progress, days=(today - last).days),
            GOALS, goal.id, now, resolved=True,
        )


def _alive(workspace):
    return SavingsGoal.objects.filter(workspace=workspace).exclude(
        status=SavingsGoal.ARCHIVED,
    ).select_related('account', 'workspace')


def evaluate(workspace_id, now=None):
    """After money moved: milestones and reaching the goal."""
    now = now or timezone.now()
    workspace = Workspace.objects.filter(pk=workspace_id).first()
    if workspace is None:
        return
    today = inbox.today_in(workspace, now)
    with transaction.atomic():
        for goal in _alive(workspace).select_for_update(of=('self',)):
            check_milestones(goal, goal_math.progress(goal, today), now)


def daily(now=None):
    """Once a day, every goal: milestones a scheduled transfer passed, pace, quiet spells."""
    now = now or timezone.now()
    workspace_ids = SavingsGoal.objects.exclude(status=SavingsGoal.ARCHIVED).values_list(
        'workspace_id', flat=True,
    ).distinct()
    for workspace in Workspace.objects.filter(pk__in=workspace_ids):
        today = inbox.today_in(workspace, now)
        with transaction.atomic():
            for goal in _alive(workspace).select_for_update(of=('self',)):
                progress = goal_math.progress(goal, today)
                check_milestones(goal, progress, now)
                check_pace(goal, progress, today, now)

