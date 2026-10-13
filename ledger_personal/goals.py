"""
How far a savings goal has come, and whether it keeps pace with its date.

The app works the same numbers out from its own copy of the data
(lib/features/goals/goal_math.dart); the two must give the same answer, so
the rules live in one place each and are kept short.
"""
import math
from dataclasses import dataclass
from datetime import date

from django.db.models import Max

from .models import SavingsGoal, Transaction
from .queries import balance_of

#: Below this share of where the date says it should be, a goal is behind
ON_TRACK_SHARE = 90
#: Above this share of where the date says it should be, a goal is ahead
AHEAD_SHARE = 110

ON_TRACK = 'on_track'
AHEAD = 'ahead'
BEHIND = 'behind'
NO_DATE = 'no_date'
REACHED = 'reached'


@dataclass(frozen=True)
class Progress:
    saved_minor: int
    target_minor: int
    #: Whole percent of the target saved, floored; may pass 100
    percent: int
    remaining_minor: int
    #: Where a steady saver would be today, given the start and the date; None without a date
    expected_minor: int | None
    #: Still to put in each month to make the date; None without a date or once past it
    needed_per_month_minor: int | None
    pace: str

    def as_dict(self):
        return self.__dict__.copy()


def months_between(start, end):
    """Months from start to end, counting a part month as a part: 1 Jan to 16 Feb is about 1.5."""
    return max((end - start).days, 0) / 30.4375


def expected_by(target_minor, start, target_on, today):
    """A straight line from nothing on the start day to the target on the date."""
    if target_on <= start or today >= target_on:
        return target_minor
    if today <= start:
        return 0
    return target_minor * (today - start).days // (target_on - start).days


def pace_of(saved_minor, target_minor, expected_minor):
    if saved_minor >= target_minor:
        return REACHED
    if expected_minor is None:
        return NO_DATE
    if saved_minor * 100 < expected_minor * ON_TRACK_SHARE:
        return BEHIND
    if saved_minor * 100 > expected_minor * AHEAD_SHARE:
        return AHEAD
    return ON_TRACK


def progress(goal, today, saved_minor=None):
    """The goal as of `today`. `saved_minor` saves a query when the balance is already known."""
    saved = balance_of(goal.account) if saved_minor is None else saved_minor
    target = goal.target_minor
    remaining = max(target - saved, 0)
    expected = needed = None
    if goal.target_on is not None:
        start = goal.created_at.date() if goal.created_at else today
        expected = expected_by(target, start, goal.target_on, today)
        months = months_between(today, goal.target_on)
        if remaining and months > 0:
            # Rounded up: paying this each month never falls short
            needed = math.ceil(remaining / months)
    return Progress(
        saved_minor=saved, target_minor=target, percent=max(saved, 0) * 100 // target,
        remaining_minor=remaining, expected_minor=expected, needed_per_month_minor=needed,
        pace=pace_of(saved, target, expected),
    )


def milestones_passed(percent):
    """The bits of SavingsGoal.milestones_hit that `percent` has earned."""
    bits = 0
    for position, mark in enumerate(SavingsGoal.MILESTONES):
        if percent >= mark:
            bits |= 1 << position
    return bits


def last_contribution_on(goal) -> date | None:
    """The day money last went into the goal."""
    return Transaction.objects.filter(account=goal.account_id, amount_minor__gt=0).aggregate(
        last=Max('occurred_on'),
    )['last']
