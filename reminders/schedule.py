"""
When a rule falls due. Pure dates: no database, no clock.

A schedule is anything with these attributes (a Rule has them):

    freq         once, daily, weekly, monthly or yearly
    interval     every N of them
    weekday_mask days of the week, Monday as bit 0; 0 means the weekday of start_on
    month_day    1-31 or -1 for the last day; None means the day of start_on.
                 A month too short for the day uses its last day, so "the 31st"
                 is the 30th in April and the 28th or 29th in February.
    set_pos      with weekday_mask: the first (1) to fourth (4) or the last (-1)
                 of those weekdays in the month, as in "the first Friday"
    week_start   the day weeks start on, Monday as 0: it decides which weeks
                 "every 2 weeks" skips when several weekdays are chosen
    start_on     the first day it may fall on
    end_on       the last day it may fall on, or None

The app works out the same dates to preview a rule
(lib/features/recurring/recurrence.dart). Both are tested against
reminders/tests/recurrence_cases.json; change them together.
"""
import calendar
from datetime import date, timedelta

from django.core.exceptions import ValidationError

ONCE, DAILY, WEEKLY, MONTHLY, YEARLY = 'once', 'daily', 'weekly', 'monthly', 'yearly'
FREQUENCIES = (ONCE, DAILY, WEEKLY, MONTHLY, YEARLY)
LAST = -1
SET_POSITIONS = (1, 2, 3, 4, LAST)
MAX_INTERVAL = 999


def check(schedule):
    """ValidationError when the fields do not describe a schedule."""
    errors = {}
    if schedule.freq not in FREQUENCIES:
        errors['freq'] = f"Must be one of: {', '.join(FREQUENCIES)}."
    if not 1 <= (schedule.interval or 0) <= MAX_INTERVAL:
        errors['interval'] = f'Must be 1 to {MAX_INTERVAL}.'
    if not 0 <= (schedule.weekday_mask or 0) <= 127:
        errors['weekday_mask'] = 'Must be 0 to 127.'
    if not 0 <= (schedule.week_start or 0) <= 6:
        errors['week_start'] = 'Must be 0 (Monday) to 6 (Sunday).'
    if schedule.month_day is not None and schedule.month_day != LAST and not 1 <= schedule.month_day <= 31:
        errors['month_day'] = 'Must be 1 to 31, or -1 for the last day.'
    if schedule.set_pos is not None:
        if schedule.set_pos not in SET_POSITIONS:
            errors['set_pos'] = 'Must be 1 to 4, or -1 for the last.'
        elif schedule.freq not in (MONTHLY, YEARLY) or not schedule.weekday_mask:
            errors['set_pos'] = 'Needs a weekday, in a monthly or yearly schedule.'
        elif schedule.month_day is not None:
            errors['set_pos'] = 'Use a day of the month or a weekday of the month, not both.'
    if schedule.end_on is not None and schedule.start_on is not None and schedule.end_on < schedule.start_on:
        errors['end_on'] = 'Must not be before the start.'
    if errors:
        raise ValidationError(errors)


def _weekdays(schedule):
    mask = schedule.weekday_mask or (1 << schedule.start_on.weekday())
    return [weekday for weekday in range(7) if mask & (1 << weekday)]


def _week_of(day, week_start):
    """The first day of the week `day` is in."""
    return day - timedelta(days=(day.weekday() - week_start) % 7)


def _day_in_month(schedule, year, month):
    """The day of that month the schedule falls on."""
    length = calendar.monthrange(year, month)[1]
    if schedule.set_pos is not None:
        weekdays = _weekdays(schedule)
        days = [day for day in range(1, length + 1) if date(year, month, day).weekday() in weekdays]
        return date(year, month, days[-1] if schedule.set_pos == LAST else days[schedule.set_pos - 1])
    wanted = schedule.month_day if schedule.month_day is not None else schedule.start_on.day
    return date(year, month, length if wanted == LAST else min(wanted, length))


def _daily(schedule, day):
    behind = (day - schedule.start_on).days % schedule.interval
    return day + timedelta(days=(schedule.interval - behind) % schedule.interval)


def _weekly(schedule, day):
    weekdays = _weekdays(schedule)
    first_week = _week_of(schedule.start_on, schedule.week_start)
    week = _week_of(day, schedule.week_start)
    behind = ((week - first_week).days // 7) % schedule.interval
    if behind:
        # A week the schedule skips: on to the next one it keeps
        week += timedelta(weeks=schedule.interval - behind)
        day = week
    while True:
        for offset in range(7):
            candidate = week + timedelta(days=offset)
            if candidate >= day and candidate.weekday() in weekdays:
                return candidate
        week += timedelta(weeks=schedule.interval)
        day = week


def _monthly(schedule, day):
    start = schedule.start_on
    months = (day.year - start.year) * 12 + day.month - start.month
    months += (schedule.interval - months % schedule.interval) % schedule.interval
    while True:
        year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
        candidate = _day_in_month(schedule, year, month + 1)
        if candidate >= day:
            return candidate
        months += schedule.interval


def _yearly(schedule, day):
    start = schedule.start_on
    years = day.year - start.year
    years += (schedule.interval - years % schedule.interval) % schedule.interval
    while True:
        candidate = _day_in_month(schedule, start.year + years, start.month)
        if candidate >= day:
            return candidate
        years += schedule.interval


def next_on_or_after(schedule, day):
    """The first day on or after `day` the schedule falls on, or None when there is none."""
    day = max(day, schedule.start_on)
    if schedule.freq == ONCE:
        found = schedule.start_on if day == schedule.start_on else None
    else:
        found = {DAILY: _daily, WEEKLY: _weekly, MONTHLY: _monthly, YEARLY: _yearly}[schedule.freq](schedule, day)
    if found is not None and schedule.end_on is not None and found > schedule.end_on:
        return None
    return found


def occurrences(schedule, since, count):
    """Up to `count` days the schedule falls on, from `since` on."""
    found, day = [], since
    while len(found) < count:
        day = next_on_or_after(schedule, day)
        if day is None:
            break
        found.append(day)
        day += timedelta(days=1)
    return found
