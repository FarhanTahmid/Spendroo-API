"""
Reading one account: its transactions under a filter, and what they add up to.

The list and the totals are built from the same filter here, so they can never
disagree. The app answers the same questions from its own copy of the data
(lib/features/personal/account_ledger.dart); the two must give the same result.
"""
import uuid
from dataclasses import dataclass
from datetime import date

from django.db.models import (
    BigIntegerField, Case, CharField, Count, ExpressionWrapper, F, Max, Min, OuterRef, Q, Subquery, Sum, UUIDField,
    Value, When,
)
from django.db.models.functions import Abs, Cast
from rest_framework.exceptions import ParseError

from spendroo.money import currency_exponent
from . import allocations, trends
from .models import Transaction

KINDS = tuple(kind for kind, _ in Transaction.KIND_CHOICES)
UNCATEGORISED = 'none'
# A span longer than this is charted month by month
SERIES_DAILY_LIMIT = 92
MAX_QUERY_LENGTH = 100
_ASCII_DIGITS = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')


@dataclass(frozen=True)
class Filters:
    """The dates set the period; kind, categories and search pick rows inside it."""
    date_from: date | None = None
    date_to: date | None = None
    kind: str | None = None
    category_ids: tuple = ()
    # Income and expenses filed under no category, alongside category_ids
    uncategorised: bool = False
    q: str = ''

    @property
    def by_category(self):
        return bool(self.category_ids or self.uncategorised)

    @property
    def is_narrowed(self):
        return bool(self.kind or self.by_category or self.q)


def _date(params, name):
    value = params.get(name)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ParseError(f'{name} must be a date as YYYY-MM-DD.')


def parse_filters(params):
    """Filters from ?from&to&kind&category&q. `category` repeats or takes commas; 'none' is uncategorised."""
    date_from, date_to = _date(params, 'from'), _date(params, 'to')
    if date_from and date_to and date_from > date_to:
        raise ParseError('from must not be after to.')
    kind = params.get('kind') or None
    if kind is not None and kind not in KINDS:
        raise ParseError(f"kind must be one of: {', '.join(KINDS)}.")

    category_ids, uncategorised = [], False
    for value in params.getlist('category'):
        for token in filter(None, (part.strip() for part in value.split(','))):
            if token == UNCATEGORISED:
                uncategorised = True
                continue
            try:
                category_ids.append(uuid.UUID(token))
            except ValueError:
                raise ParseError(f"category must be ids or '{UNCATEGORISED}'.")
    q = (params.get('q') or '').strip().translate(_ASCII_DIGITS)[:MAX_QUERY_LENGTH]
    return Filters(date_from, date_to, kind, tuple(category_ids), uncategorised, q)


def _other_leg(field):
    """Of a transfer: a column of the leg on the other account."""
    return Subquery(
        Transaction.objects.filter(transfer_group_id=OuterRef('transfer_group_id'))
        .exclude(id=OuterRef('id')).order_by().values(field)[:1]
    )


def _alive_category(field, output_field):
    """A deleted category reads as no category, as it does in the app."""
    return Case(When(category__deleted_at__isnull=True, then=F(f'category__{field}')), output_field=output_field)


def account_transactions(account):
    """Every live transaction of the account, carrying what a row shows beside its own columns."""
    return Transaction.objects.filter(account=account).annotate(
        category_name=_alive_category('name', CharField()),
        category_color=_alive_category('color', CharField()),
        other_account_id=_other_leg('account_id'),
        other_account_name=_other_leg('account__name'),
    )


def _period(filters):
    condition = Q()
    if filters.date_from:
        condition &= Q(occurred_on__gte=filters.date_from)
    if filters.date_to:
        condition &= Q(occurred_on__lte=filters.date_to)
    return condition


def _narrow(account, filters):
    """Kind, categories and search as one condition over account_transactions()."""
    condition = Q()
    if filters.kind:
        condition &= Q(kind=filters.kind)
    if filters.by_category:
        # A split is kept when any of its parts is; _split_parts() says how much of it counts
        condition &= allocations.condition(filters.category_ids, filters.uncategorised)
    if filters.q:
        either = (
            Q(note__icontains=filters.q) | Q(category_name__icontains=filters.q)
            | Q(other_account_name__icontains=filters.q)
        )
        # "1,250" or "1250.50": also the amount, by its whole units
        number = filters.q.replace(',', '')
        whole, _, fraction = number.partition('.')
        if whole.isascii() and whole.isdigit() and (not fraction or (fraction.isascii() and fraction.isdigit())):
            either |= Q(whole_units__contains=whole)
        condition &= either
    return condition


def _with_units(queryset, account):
    """Adds the amount in whole units as text, for searching by amount."""
    return queryset.annotate(whole_units=Cast(
        ExpressionWrapper(Abs(F('amount_minor')) / Value(10 ** currency_exponent(account.currency)),
                          output_field=BigIntegerField()),
        CharField(),
    ))


def filtered(account, filters):
    """The account's transactions inside the filter, unordered."""
    return _with_units(account_transactions(account), account).filter(_period(filters), _narrow(account, filters))


def _split_parts(account, queryset, filters):
    """
    (day, kind, category id, amount) for each part of the split transactions
    in `queryset` that the filter's categories keep.
    """
    alive = allocations.categories_of(account.workspace_id)
    return [
        (row['occurred_on'], row['kind'], category_id if category_id in alive else None, amount)
        for row in allocations.split_only(queryset).order_by().values('occurred_on', 'kind', 'amount_minor', 'splits')
        for category_id, amount, _ in allocations.kept(
            allocations.parts(row['amount_minor'], None, row['splits']),
            filters.category_ids, filters.uncategorised, alive,
        )
    ]


def _total(queryset, condition=Q()):
    return queryset.filter(condition).aggregate(total=Sum('amount_minor'))['total'] or 0


def balance_of(account):
    return account.opening_balance_minor + _total(Transaction.objects.filter(account=account))


def summarize(account, filters):
    """What moved inside the filter (all positive), and the balance on either side of the period."""
    rows = filtered(account, filters)
    # Under a choice of categories a split counts for its chosen parts only
    partial = _split_parts(account, rows, filters) if filters.by_category else []
    totals = rows.aggregate(
        income=Sum('amount_minor', filter=Q(kind='income')),
        expense=Sum('amount_minor', filter=Q(kind='expense')),
        transfer_in=Sum('amount_minor', filter=Q(kind='transfer', amount_minor__gt=0)),
        transfer_out=Sum('amount_minor', filter=Q(kind='transfer', amount_minor__lt=0)),
        # Expenses are negative, so the smallest is the biggest
        largest=Min('amount_minor', filter=Q(kind='expense')),
        count=Count('id'),
        first_day=Min('occurred_on'),
        last_day=Max('occurred_on'),
    )
    if filters.by_category:
        whole = allocations.unsplit_only(rows).aggregate(
            income=Sum('amount_minor', filter=Q(kind='income')),
            expense=Sum('amount_minor', filter=Q(kind='expense')),
            largest=Min('amount_minor', filter=Q(kind='expense')),
        )
        kept = {kind: sum(amount for _, part_kind, _, amount in partial if part_kind == kind)
                for kind in allocations.PLAIN_KINDS}
        totals['income'] = (whole['income'] or 0) + kept['income']
        totals['expense'] = (whole['expense'] or 0) + kept['expense']
        totals['largest'] = min(
            [whole['largest'] or 0, *(amount for _, part_kind, _, amount in partial if part_kind == 'expense')]
        )
    income, expense = totals['income'] or 0, -(totals['expense'] or 0)
    transfer_in, transfer_out = totals['transfer_in'] or 0, -(totals['transfer_out'] or 0)
    everything = Transaction.objects.filter(account=account)
    # Nothing before an open start; everything up to an open end
    before = _total(everything, Q(occurred_on__lt=filters.date_from)) if filters.date_from else 0
    through = _total(everything, Q(occurred_on__lte=filters.date_to) if filters.date_to else Q())
    return {
        'income_minor': income,
        'expense_minor': expense,
        'transfer_in_minor': transfer_in,
        'transfer_out_minor': transfer_out,
        'cash_in_minor': income + transfer_in,
        'cash_out_minor': expense + transfer_out,
        'net_minor': income + transfer_in - expense - transfer_out,
        'largest_expense_minor': -(totals['largest'] or 0),
        'count': totals['count'],
        'first_day': totals['first_day'],
        'last_day': totals['last_day'],
        'period_opening_minor': account.opening_balance_minor + before,
        'period_closing_minor': account.opening_balance_minor + through,
    }


def by_category(account, filters, kind):
    """Expense or income per category inside the filter, largest first. Each part of a split counts under its own."""
    alive = allocations.categories_of(account.workspace_id)
    rows = filtered(account, filters).filter(kind=kind).order_by().values('amount_minor', 'category_id', 'splits')
    found = {}
    for row in rows:
        chosen = allocations.kept(
            allocations.parts(row['amount_minor'], row['category_id'], row['splits']),
            filters.category_ids, filters.uncategorised, alive,
        )
        for category_id, amount, _ in chosen:
            # A deleted category reads as no category, as it does in the app
            key = category_id if category_id in alive else None
            total = found.setdefault(key, {'amount_minor': 0, 'count': 0, 'largest_minor': 0})
            total['amount_minor'] += abs(amount)
            total['count'] += 1
            total['largest_minor'] = max(total['largest_minor'], abs(amount))
    return [
        {
            'category_id': key, 'name': alive[key][0] if key else None, 'color': alive[key][1] if key else None,
            **total,
        }
        for key, total in sorted(found.items(), key=lambda item: -item[1]['amount_minor'])
    ]


def series(account, filters, max_months=None):
    """
    Cash in, cash out and the closing balance, step by step through the period:
    by day, or by month once the span is long. In and out follow the whole
    filter; the balance counts every transaction, as a balance must.

    A month-by-month series keeps only the `max_months` months up to its latest
    (the plan's insights.trend_months); `hidden_months` says how many went.
    """
    narrow = _narrow(account, filters)
    everything = _with_units(account_transactions(account), account).filter(_period(filters))
    partial = {}
    if filters.by_category:
        # Whole rows are added up below; a split adds only the parts the categories keep
        for day, _, _, amount in _split_parts(account, everything.filter(narrow), filters):
            moved = partial.setdefault(day, [0, 0])
            moved[0 if amount > 0 else 1] += amount
        narrow &= Q(splits=[])
    days = list(
        everything
        .order_by().values('occurred_on')
        .annotate(
            net=Sum('amount_minor'),
            cash_in=Sum('amount_minor', filter=narrow & Q(amount_minor__gt=0)),
            cash_out=Sum('amount_minor', filter=narrow & Q(amount_minor__lt=0)),
        )
        .order_by('occurred_on')
    )
    opening = account.opening_balance_minor
    if filters.date_from:
        opening += _total(Transaction.objects.filter(account=account), Q(occurred_on__lt=filters.date_from))
    if not days:
        return {'monthly': False, 'opening_minor': opening, 'points': [], 'hidden_months': 0}
    first = filters.date_from or days[0]['occurred_on']
    last = filters.date_to or days[-1]['occurred_on']
    monthly = (last - first).days > SERIES_DAILY_LIMIT
    points, balance = [], opening
    for day in days:
        step = day['occurred_on'].replace(day=1) if monthly else day['occurred_on']
        balance += day['net']
        part_in, part_out = partial.get(day['occurred_on'], (0, 0))
        cash_in, cash_out = (day['cash_in'] or 0) + part_in, -((day['cash_out'] or 0) + part_out)
        if points and points[-1]['date'] == step:
            points[-1]['in_minor'] += cash_in
            points[-1]['out_minor'] += cash_out
            points[-1]['balance_minor'] = balance
        else:
            points.append({'date': step, 'in_minor': cash_in, 'out_minor': cash_out, 'balance_minor': balance})
    hidden = 0
    if monthly:
        kept, hidden = trends.trim_months(points, points[-1]['date'], max_months)
        if hidden:
            # The balance the first month kept starts from
            opening = points[len(points) - len(kept) - 1]['balance_minor']
            points = kept
    return {'monthly': monthly, 'opening_minor': opening, 'points': points, 'hidden_months': hidden}
