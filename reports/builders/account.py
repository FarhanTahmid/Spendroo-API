"""One personal account: what it held, what moved, line by line."""
from django.db.models import Count, Q, Sum

from ledger_personal import queries

from .. import narrow as narrowing
from ..document import DATE, MONEY, TONE_BALANCE, TONE_IN, TONE_OUT, Column, Money, Table, Totals, net
from ..models import SCOPE_ACCOUNT
from .common import money, share_table

# The kinds of line that can have a category
PLAIN = ('income', 'expense')


def _moved(lines):
    """What the lines add up to, each figure positive."""
    sums = lines.aggregate(
        income=Sum('amount_minor', filter=Q(kind='income')),
        expense=Sum('amount_minor', filter=Q(kind='expense')),
        transfer_in=Sum('amount_minor', filter=Q(kind='transfer', amount_minor__gt=0)),
        transfer_out=Sum('amount_minor', filter=Q(kind='transfer', amount_minor__lt=0)),
    )
    moved = {name: abs(value or 0) for name, value in sums.items()}
    moved['cash_in'] = moved['income'] + moved['transfer_in']
    moved['cash_out'] = moved['expense'] + moved['transfer_out']
    return moved


def movements(words, moved, currency, narrow):
    rows = [
        ('income', [words.t('row.income'), money(moved['income'], currency), None]),
        ('transfer', [words.t('row.transfers_in'), money(moved['transfer_in'], currency), None]),
        ('expense', [words.t('row.spending'), None, money(moved['expense'], currency)]),
        ('transfer', [words.t('row.transfers_out'), None, money(moved['transfer_out'], currency)]),
    ]
    return Table(
        title=words.t('table.movements'), currency=currency,
        columns=[
            Column(words.t('col.item'), width=3),
            Column(words.t('col.in'), MONEY, TONE_IN, width=1.6),
            Column(words.t('col.out'), MONEY, TONE_OUT, width=1.6),
        ],
        rows=[row for kind, row in rows if narrow.shows(kind)],
        total=[words.t('row.total'), Money(moved['cash_in'], currency), Money(moved['cash_out'], currency)],
    )


def category_table(words, lines, kind, currency):
    rows = lines.filter(kind=kind).order_by().values('category_name').annotate(total=Sum('amount_minor'), n=Count('id'))
    expense = kind == 'expense'
    return share_table(
        words, words.t('table.spending_by_category' if expense else 'table.income_by_category'),
        [(row['category_name'] or words.t('row.uncategorised'), row['n'], abs(row['total'])) for row in rows],
        currency, TONE_OUT if expense else TONE_IN,
    )


def statement(words, lines, period, currency, moved, opening):
    """Line by line. With `opening` the lines are all of them, so each carries the balance after it."""
    running = opening is not None
    rows, balance = [], opening or 0
    for line in lines.order_by('occurred_on', 'created_at', 'id'):
        balance += line.amount_minor
        if line.kind == 'transfer':
            key = 'row.transfer_from' if line.amount_minor > 0 else 'row.transfer_to'
            what = words.t(key, name=line.other_account_name) if line.other_account_name else words.t('row.transfer')
        else:
            what = line.category_name or words.t('row.uncategorised')
        rows.append([
            line.occurred_on, line.note, what,
            money(max(line.amount_minor, 0), currency), money(max(-line.amount_minor, 0), currency),
            *([Money(balance, currency)] if running else []),
        ])
    sums = [Money(moved['cash_in'], currency), Money(moved['cash_out'], currency)]
    return Table(
        title=words.t('table.statement'), currency=currency, is_detail=True, empty=words.t('empty.transactions'),
        columns=[
            Column(words.t('col.date'), DATE, width=1.5),
            Column(words.t('col.note'), width=3),
            Column(words.t('col.category'), width=2.4),
            Column(words.t('col.in'), MONEY, TONE_IN, width=1.6),
            Column(words.t('col.out'), MONEY, TONE_OUT, width=1.6),
            *([Column(words.t('col.balance'), MONEY, TONE_BALANCE, width=1.7)] if running else []),
        ],
        first=[period.date_from, words.t('row.opening'), '', None, None, Money(opening, currency)] if running else None,
        rows=rows,
        total=(
            [period.date_to, words.t('row.closing'), '', *sums, Money(balance, currency)] if running
            else ([None, words.t('row.total'), '', *sums] if rows else None)
        ),
    )


def build(account, context):
    words, period, narrow = context.words, context.period, context.narrow
    currency = account.currency
    lines = narrowing.lines(
        queries.account_transactions(account).filter(occurred_on__range=(period.date_from, period.date_to)),
        narrow, 'kind', PLAIN,
    )
    moved = _moved(lines)
    if narrow.active:
        opening = None
        totals = net(currency, moved['cash_in'], moved['cash_out'])
    else:
        opening = queries.summarize(
            account, queries.Filters(period.date_from, period.date_to),
        )['period_opening_minor']
        totals = Totals(
            currency, opening, moved['cash_in'], moved['cash_out'], opening + moved['cash_in'] - moved['cash_out'],
        )
    return context.report(
        SCOPE_ACCOUNT, account.name,
        totals=[totals],
        notes=context.notes(
            words.t('note.equation'),
            words.t('note.account_transfers'),
            words.t('note.opening', date=words.date(period.date_from)),
            words.t('note.running'),
        ),
        tables=[
            movements(words, moved, currency, narrow),
            *(category_table(words, lines, kind, currency) for kind in ('expense', 'income') if narrow.shows(kind)),
            statement(words, lines, period, currency, moved, opening),
        ],
    )
