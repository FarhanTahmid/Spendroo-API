"""
Income and expense month by month across a workspace: the cash-flow trend.

The app draws the same trend from its own copy of the data
(lib/features/personal/ledger_repository.dart, watchMonthlyTotals); the two
must give the same result. How many months may be asked for is the plan's
insights.trend_months.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from rest_framework.exceptions import ParseError

from .models import Transaction

DEFAULT_MONTHS = 6
# Past any plan's limit; keeps a request from asking for centuries
MAX_MONTHS = 120


def add_months(month, count):
    """The first day of the month `count` months from `month` (negative goes back)."""
    index = month.year * 12 + month.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def month_end(month):
    return add_months(month, 1) - timedelta(days=1)


def month_window(last_month, count):
    """(first day, last day) of the `count` months ending with `last_month`."""
    first = add_months(last_month.replace(day=1), -(count - 1))
    return first, month_end(last_month.replace(day=1))


def months_between(first_month, last_month):
    """Every month from the first to the last, both included."""
    first, last = first_month.replace(day=1), last_month.replace(day=1)
    months, month = [], first
    while month <= last:
        months.append(month)
        month = add_months(month, 1)
    return months


def monthly_totals(workspace, currency, first_month, last_month):
    """
    Income and expense (both positive) for each month from the first to the
    last, oldest first, empty months included. Transfers move money between
    the user's own accounts, so they are neither.
    """
    first, last = first_month.replace(day=1), month_end(last_month.replace(day=1))
    found = {
        row['month']: row
        for row in Transaction.objects.filter(
            workspace=workspace, currency=currency, occurred_on__gte=first, occurred_on__lte=last,
        ).exclude(kind='transfer')
        .annotate(month=TruncMonth('occurred_on'))
        .order_by().values('month')
        .annotate(
            income=Sum('amount_minor', filter=Q(kind='income')),
            expense=Sum('amount_minor', filter=Q(kind='expense')),
        )
    }
    totals = []
    for month in months_between(first, last):
        row = found.get(month, {})
        totals.append({
            'month': month,
            'income_minor': row.get('income') or 0,
            'expense_minor': -(row.get('expense') or 0),
        })
    return totals


@dataclass(frozen=True)
class TrendRequest:
    months: int
    last_month: date
    currency: str

    @property
    def window(self):
        return month_window(self.last_month, self.months)


def parse_trend_params(params, today, default_currency):
    """?months&to&currency: how many months, ending with which (YYYY-MM), in which currency."""
    raw = params.get('months')
    if raw in (None, ''):
        months = DEFAULT_MONTHS
    else:
        try:
            months = int(raw)
        except ValueError:
            raise ParseError('months must be a whole number.')
        if not 1 <= months <= MAX_MONTHS:
            raise ParseError(f'months must be from 1 to {MAX_MONTHS}.')

    raw = params.get('to')
    if raw:
        try:
            last_month = date.fromisoformat(f'{raw}-01')
        except ValueError:
            raise ParseError('to must be a month as YYYY-MM.')
    else:
        last_month = today.replace(day=1)

    currency = (params.get('currency') or default_currency).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ParseError('currency must be a three-letter code.')
    return TrendRequest(months, last_month, currency)


def trim_months(points, last_month, max_months, key='date'):
    """
    Of month-by-month points, those in the `max_months` months ending with
    `last_month`; and how many months before that were left out.
    """
    if max_months is None or not points:
        return points, 0
    cutoff = add_months(last_month.replace(day=1), -(max_months - 1))
    kept = [point for point in points if point[key] >= cutoff]
    if len(kept) == len(points):
        return points, 0
    earliest = min(point[key] for point in points)
    hidden = (cutoff.year - earliest.year) * 12 + cutoff.month - earliest.month
    return kept, hidden
