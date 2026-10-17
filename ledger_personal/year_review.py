"""
A workspace's year in one answer: what came in and went out, month by month
and by category, set against the year before.

The app works the same review out from its own copy of the data
(lib/features/insights/year_review.dart, YearReview.of); the two must give the
same result, so the rules below are kept short and mirrored there:

- A month is "closed" once it is over. Ranking months (biggest, smallest, best)
  and the monthly average look at closed months only, so a month still under
  way never reads as the smallest.
- Closed months are "tracked" from the month of the workspace's first
  transaction: the average and the months in the green leave out months from
  before anything was recorded.
- Percentages are rounded half away from zero, as Dart's round() does.
- Transfers are neither income nor expense; each part of a split counts under
  its own category; a deleted category reads as no category.
"""
from dataclasses import dataclass
from datetime import date

from django.db.models import Min
from rest_framework.exceptions import ParseError

from . import allocations, trends
from .models import SavingsGoal, Transaction

FIRST_YEAR = 2000
TOP_CATEGORIES = 5


def default_year(today):
    """In January last year is the one worth looking back on; otherwise this one so far."""
    return today.year - 1 if today.month == 1 else today.year


def percent(part, whole):
    """Whole percent of `whole`, rounded half away from zero. None when there is nothing to compare with."""
    if not whole:
        return None
    value = abs(part) * 100
    rounded = (2 * value + abs(whole)) // (2 * abs(whole))
    return rounded if (part < 0) == (whole < 0) else -rounded


@dataclass(frozen=True)
class YearRequest:
    year: int
    currency: str

    @property
    def window(self):
        return date(self.year, 1, 1), date(self.year, 12, 31)


def parse_year_params(params, today, default_currency):
    """?year&currency: which year (this one at the latest) and in which currency."""
    raw = params.get('year')
    if raw in (None, ''):
        year = default_year(today)
    else:
        try:
            year = int(raw)
        except ValueError:
            raise ParseError('year must be a whole number.')
        if not FIRST_YEAR <= year <= today.year:
            raise ParseError(f'year must be from {FIRST_YEAR} to {today.year}.')
    currency = (params.get('currency') or default_currency).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ParseError('currency must be a three-letter code.')
    return YearRequest(year, currency)


def closed_months(year, today):
    """How many months of `year` are over."""
    if year < today.year:
        return 12
    return today.month - 1 if year == today.year else 0


def category_totals(workspace, currency, first, last, previous_first):
    """
    Expense per category from `first` to `last`, largest first, each carrying
    what it took from `previous_first` up to `first`. Categories with nothing
    in the period itself are left out.
    """
    alive = allocations.categories_of(workspace.id)
    rows = (
        Transaction.objects.filter(
            workspace=workspace, currency=currency, kind='expense',
            occurred_on__gte=previous_first, occurred_on__lte=last,
        ).order_by().values('occurred_on', 'amount_minor', 'category_id', 'splits')
    )
    found = {}
    for row in rows:
        current = row['occurred_on'] >= first
        for category_id, amount, _ in allocations.parts(row['amount_minor'], row['category_id'], row['splits']):
            key = category_id if category_id in alive else None
            total = found.setdefault(key, {'amount_minor': 0, 'count': 0, 'largest_minor': 0, 'previous_minor': 0})
            if current:
                total['amount_minor'] += abs(amount)
                total['count'] += 1
                total['largest_minor'] = max(total['largest_minor'], abs(amount))
            else:
                total['previous_minor'] += abs(amount)
    return [
        {
            'category_id': key, 'name': alive[key][0] if key else None, 'color': alive[key][1] if key else None,
            **total,
        }
        for key, total in sorted(found.items(), key=lambda item: -item[1]['amount_minor'])
        if total['count']
    ]


def _ranked(months, measure, largest):
    """The month with the largest (or smallest) measure; the earliest wins a tie."""
    if not months:
        return None
    pick = max if largest else min
    best = pick(measure(m) for m in months)
    return next(m for m in months if measure(m) == best)


def review(workspace, currency, year, today):
    """Everything the year-in-review screen shows."""
    first, last = date(year, 1, 1), date(year, 12, 31)
    totals = trends.monthly_totals(workspace, currency, date(year - 1, 1, 1), last)
    before, months = totals[:12], totals[12:]
    income = sum(m['income_minor'] for m in months)
    expense = sum(m['expense_minor'] for m in months)
    previous_income = sum(m['income_minor'] for m in before)
    previous_expense = sum(m['expense_minor'] for m in before)

    closed = months[:closed_months(year, today)]
    since = Transaction.objects.filter(workspace=workspace).aggregate(first=Min('occurred_on'))['first']
    tracked = [m for m in closed if since and m['month'] >= since.replace(day=1)]
    spent = [m for m in closed if m['expense_minor'] > 0]
    biggest = _ranked(spent, lambda m: m['expense_minor'], largest=True)
    smallest = _ranked(spent, lambda m: m['expense_minor'], largest=False) if len(spent) > 1 else None
    saving = [m for m in tracked if m['income_minor'] - m['expense_minor'] > 0]
    best = _ranked(saving, lambda m: m['income_minor'] - m['expense_minor'], largest=True)

    categories = category_totals(workspace, currency, first, last, date(year - 1, 1, 1))
    return {
        'year': year,
        'currency': currency,
        'is_partial': year == today.year,
        'date_from': first,
        'date_to': last,
        'income_minor': income,
        'expense_minor': expense,
        'net_minor': income - expense,
        'savings_rate': percent(income - expense, income) if income > 0 else None,
        'previous': (
            {
                'income_minor': previous_income,
                'expense_minor': previous_expense,
                'net_minor': previous_income - previous_expense,
            }
            if previous_income or previous_expense else None
        ),
        'months': months,
        'biggest_month': biggest and {'month': biggest['month'], 'amount_minor': biggest['expense_minor']},
        'smallest_month': smallest and {'month': smallest['month'], 'amount_minor': smallest['expense_minor']},
        'best_month': best and {'month': best['month'], 'amount_minor': best['income_minor'] - best['expense_minor']},
        'months_in_green': len(saving),
        'tracked_months': len(tracked),
        'average_expense_minor': sum(m['expense_minor'] for m in tracked) // len(tracked) if tracked else None,
        'largest_expense_minor': max((c['largest_minor'] for c in categories), default=0),
        'top_categories': categories[:TOP_CATEGORIES],
        'goals_reached': SavingsGoal.objects.filter(workspace=workspace, reached_at__year=year).count(),
    }
