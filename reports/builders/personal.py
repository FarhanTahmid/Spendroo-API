"""The whole personal space: every account, income against spending."""
from django.db.models import Count, Q, Sum

from ledger_personal.models import Account, Transaction

from .. import narrow as narrowing
from ..document import DATE, MONEY, TONE_BALANCE, TONE_IN, TONE_OUT, Column, Money, Table, Totals, net
from ..models import SCOPE_PERSONAL
from .account import PLAIN
from .common import alive_name, by_currency, money, share_table, titled


def _figures(transactions, period):
    """Per account: what came before the period, and what moved inside it."""
    inside = Q(occurred_on__range=(period.date_from, period.date_to))
    rows = transactions.order_by().values('account_id').annotate(
        before=Sum('amount_minor', filter=Q(occurred_on__lt=period.date_from)),
        money_in=Sum('amount_minor', filter=inside & Q(amount_minor__gt=0)),
        money_out=Sum('amount_minor', filter=inside & Q(amount_minor__lt=0)),
        income=Sum('amount_minor', filter=inside & Q(kind='income')),
        expense=Sum('amount_minor', filter=inside & Q(kind='expense')),
        transfers=Sum('amount_minor', filter=inside & Q(kind='transfer')),
    )
    return {row['account_id']: {name: value or 0 for name, value in row.items()} for row in rows}


def _accounts_table(words, accounts, figures, currency, several):
    rows, opening_total, closing_total = [], 0, 0
    for account in accounts:
        moved = figures.get(account.id, {})
        opening = account.opening_balance_minor + moved.get('before', 0)
        money_in, money_out = moved.get('money_in', 0), -moved.get('money_out', 0)
        closing = opening + money_in - money_out
        if account.is_archived and not (opening or money_in or money_out):
            continue  # put away and empty: nothing to report
        opening_total += opening
        closing_total += closing
        rows.append([
            words.t('row.archived', name=account.name) if account.is_archived else account.name,
            words.t(f'kind.{account.kind}'),
            Money(opening, currency), money(money_in, currency), money(money_out, currency), Money(closing, currency),
        ])
    return Table(
        title=titled(words.t('table.accounts'), currency, several), currency=currency,
        note=words.t('table.accounts.note'),
        columns=[
            Column(words.t('col.account'), width=2.6),
            Column(words.t('col.type'), width=1.5),
            Column(words.t('col.opening'), MONEY, TONE_BALANCE, width=1.6),
            Column(words.t('col.in'), MONEY, TONE_IN, width=1.6),
            Column(words.t('col.out'), MONEY, TONE_OUT, width=1.6),
            Column(words.t('col.closing'), MONEY, TONE_BALANCE, width=1.6),
        ],
        rows=rows,
        # In and out are left out of the total: a transfer would be counted on both sides
        total=[words.t('row.total'), '', Money(opening_total, currency), None, None, Money(closing_total, currency)],
    )


def _category_tables(words, inside, kind, currencies):
    rows = (
        inside.filter(kind=kind).order_by()
        .values('currency', name=alive_name('category', 'name'))
        .annotate(total=Sum('amount_minor'), n=Count('id'))
    )
    expense = kind == 'expense'
    title = words.t('table.spending_by_category' if expense else 'table.income_by_category')
    found = by_currency(rows, lambda row: row['currency'])
    return [
        share_table(
            words, titled(title, currency, len(currencies) > 1),
            [(row['name'] or words.t('row.uncategorised'), row['n'], abs(row['total'])) for row in found.get(currency, [])],
            currency, TONE_OUT if expense else TONE_IN,
        )
        for currency in currencies
    ]


def _income_and_spending(words, inside, currencies):
    lines = (
        inside.exclude(kind='transfer').select_related('account')
        .annotate(category_name=alive_name('category', 'name'))
        .order_by('occurred_on', 'created_at', 'id')
    )
    rows, income, spending = [], 0, 0
    for line in lines:
        income += max(line.amount_minor, 0)
        spending += max(-line.amount_minor, 0)
        rows.append([
            line.occurred_on, line.account.name, line.category_name or words.t('row.uncategorised'), line.note,
            money(max(line.amount_minor, 0), line.currency), money(max(-line.amount_minor, 0), line.currency),
        ])
    one = currencies[0] if len(currencies) == 1 else ''
    return Table(
        title=words.t('table.income_spending'), currency=one, is_detail=True, empty=words.t('empty.transactions'),
        columns=[
            Column(words.t('col.date'), DATE, width=1.5),
            Column(words.t('col.account'), width=2),
            Column(words.t('col.category'), width=2.2),
            Column(words.t('col.note'), width=3),
            Column(words.t('col.income'), MONEY, TONE_IN, width=1.7),
            Column(words.t('col.spending'), MONEY, TONE_OUT, width=1.7),
        ],
        rows=rows,
        # Amounts in several currencies cannot be added up in one line
        total=[None, words.t('row.total'), '', '', Money(income, one), Money(spending, one)] if one and rows else None,
    )


def _transfers(words, inside):
    """Each transfer once, however its two legs sit in the table."""
    legs = (
        inside.filter(kind='transfer').select_related('account').order_by('occurred_on', 'created_at', 'id')
    )
    pairs = {}
    for leg in legs:
        pairs.setdefault(leg.transfer_group_id, {})['in' if leg.amount_minor > 0 else 'out'] = leg
    rows = []
    for pair in pairs.values():
        sent, received = pair.get('out'), pair.get('in')
        shown = sent or received
        rows.append([
            shown.occurred_on,
            sent.account.name if sent else '',
            received.account.name if received else '',
            shown.note,
            Money(abs(shown.amount_minor), shown.currency),
        ])
    return Table(
        title=words.t('table.transfers'), is_detail=True, empty=words.t('empty.transfers'),
        columns=[
            Column(words.t('col.date'), DATE, width=1.5),
            Column(words.t('col.from'), width=2.2),
            Column(words.t('col.to'), width=2.2),
            Column(words.t('col.note'), width=3),
            Column(words.t('col.amount'), MONEY, width=1.8),
        ],
        rows=rows,
    )


def _net_totals(inside, currencies):
    """Income and spending per currency over the lines a narrowed report keeps."""
    rows = inside.order_by().values('currency').annotate(
        income=Sum('amount_minor', filter=Q(kind='income')),
        expense=Sum('amount_minor', filter=Q(kind='expense')),
    )
    found = {row['currency']: row for row in rows}
    return [
        net(currency, found.get(currency, {}).get('income') or 0, -(found.get(currency, {}).get('expense') or 0))
        for currency in currencies
    ]


def build(workspace, context):
    words, period, narrow = context.words, context.period, context.narrow
    accounts = list(Account.objects.filter(workspace=workspace).order_by('is_archived', 'created_at'))
    transactions = Transaction.objects.filter(workspace=workspace, account__in=accounts)
    inside = narrowing.lines(
        transactions.filter(occurred_on__range=(period.date_from, period.date_to)), narrow, 'kind', PLAIN,
    )

    grouped = by_currency(accounts, lambda account: account.currency)
    currencies = list(grouped) or [workspace.default_currency]
    several = len(currencies) > 1

    totals, account_tables = [], []
    if narrow.active:
        totals = _net_totals(inside, currencies)
    else:
        figures = _figures(transactions, period)
        for currency in currencies:
            held = grouped.get(currency, [])
            moved = [figures.get(account.id, {}) for account in held]
            opening = sum(account.opening_balance_minor for account in held) + sum(m.get('before', 0) for m in moved)
            income = sum(m.get('income', 0) for m in moved)
            spending = -sum(m.get('expense', 0) for m in moved)
            # Transfers inside one currency cancel out; what is left crossed from another
            crossed = sum(m.get('transfers', 0) for m in moved)
            totals.append(Totals(
                currency, opening, income, spending, opening + income - spending + crossed,
                other_minor=crossed, other_label=words.t('tile.transfers_other'),
            ))
            account_tables.append(_accounts_table(words, held, figures, currency, several))

    plain = narrow.shows('income') or narrow.shows('expense')
    report = context.report(
        SCOPE_PERSONAL, workspace.name,
        totals=totals,
        notes=[
            *context.notes(
                words.t('note.equation_personal'),
                words.t('note.personal_transfers'),
                words.t('note.opening', date=words.date(period.date_from)),
                net='note.net_personal',
            ),
            *([words.t('note.currencies')] if several else []),
        ],
        tables=[
            *account_tables,
            *(_category_tables(words, inside, 'expense', currencies) if narrow.shows('expense') else []),
            *(_category_tables(words, inside, 'income', currencies) if narrow.shows('income') else []),
            *([_income_and_spending(words, inside, currencies)] if plain else []),
            # A transfer has no category: choosing categories leaves transfers out
            *([_transfers(words, inside)] if narrow.shows('transfer') and not narrow.by_category else []),
        ],
    )
    report.in_label, report.out_label = words.t('tile.income'), words.t('tile.spending')
    return report
