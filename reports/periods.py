"""
The periods a report can cover. The presets are the ones the app's account
screen offers (lib/features/personal/account_ledger.dart) and mean the same days.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from rest_framework.exceptions import ParseError

THIS_MONTH = 'this_month'
LAST_MONTH = 'last_month'
LAST_3_MONTHS = 'last_3_months'
THIS_YEAR = 'this_year'
CUSTOM = 'custom'
PRESETS = (THIS_MONTH, LAST_MONTH, LAST_3_MONTHS, THIS_YEAR)
ALL = PRESETS + (CUSTOM,)


@dataclass(frozen=True)
class Period:
    name: str
    date_from: date
    date_to: date

    @property
    def is_custom(self):
        return self.name == CUSTOM


def _month_start(day, months_back=0):
    index = day.year * 12 + day.month - 1 - months_back
    return date(index // 12, index % 12 + 1, 1)


def _month_end(day):
    return _month_start(day, -1) - timedelta(days=1)


def _date(params, name):
    value = params.get(name)
    if not value:
        raise ParseError(f'{name} is required for a custom period, as YYYY-MM-DD.')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ParseError(f'{name} must be a date as YYYY-MM-DD.')


def resolve(params, today):
    """The period asked for with ?period&from&to. Without `period` it is this month."""
    name = params.get('period') or THIS_MONTH
    if name == THIS_MONTH:
        return Period(name, _month_start(today), _month_end(today))
    if name == LAST_MONTH:
        start = _month_start(today, 1)
        return Period(name, start, _month_end(start))
    if name == LAST_3_MONTHS:
        return Period(name, _month_start(today, 2), _month_end(today))
    if name == THIS_YEAR:
        return Period(name, date(today.year, 1, 1), date(today.year, 12, 31))
    if name == CUSTOM:
        date_from, date_to = _date(params, 'from'), _date(params, 'to')
        if date_from > date_to:
            raise ParseError('from must not be after to.')
        return Period(name, date_from, date_to)
    raise ParseError(f"period must be one of: {', '.join(ALL)}.")
