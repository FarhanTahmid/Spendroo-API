"""A whole business: its cashbooks side by side, then each one in full."""
from cashbook.models import CashBook

from ..document import MONEY, TONE_BALANCE, TONE_IN, TONE_OUT, Column, Money, Table
from ..models import SCOPE_BUSINESS
from .cashbook import breakdowns, entries_of, entries_table, figures, totals_of
from .common import by_currency, money, titled


def _sums(books, numbers):
    return [sum(numbers[book.id][i] for book in books) for i in range(4)]


def _books_table(words, books, numbers, currency, several, narrowed):
    def cells(opening, cash_in, cash_out, closing):
        moved = [money(cash_in, currency), money(cash_out, currency)]
        # Narrowed there are no balances: what came in, what went out, the difference
        return [*moved, Money(closing, currency)] if narrowed else [Money(opening, currency), *moved, Money(closing, currency)]

    held = Column(words.t('col.opening'), MONEY, TONE_BALANCE, width=1.6)
    last = Column(words.t('col.net' if narrowed else 'col.closing'), MONEY, TONE_BALANCE, width=1.6)
    total = _sums(books, numbers)
    return Table(
        title=titled(words.t('table.cashbooks'), currency, several), currency=currency,
        columns=[
            Column(words.t('col.cashbook'), width=3),
            *([] if narrowed else [held]),
            Column(words.t('col.in'), MONEY, TONE_IN, width=1.6),
            Column(words.t('col.out'), MONEY, TONE_OUT, width=1.6),
            last,
        ],
        rows=[[book.book_name, *cells(*numbers[book.id])] for book in books],
        total=[
            words.t('row.total'),
            *(Money(amount, currency) for amount in (total[1:] if narrowed else total)),
        ],
    )


def build(workspace, context):
    words, period, narrow = context.words, context.period, context.narrow
    books = list(CashBook.objects.filter(workspace=workspace).order_by('created_at'))
    numbers = figures(books, period, narrow)
    grouped = by_currency(books, lambda book: book.currency)
    currencies = list(grouped) or [workspace.default_currency]
    several = len(currencies) > 1

    totals, book_tables, breakdown_tables = [], [], []
    for currency in currencies:
        held = grouped.get(currency, [])
        totals.append(totals_of(currency, _sums(held, numbers), narrow))
        book_tables.append(_books_table(words, held, numbers, currency, several, narrow.active))
        breakdown_tables += breakdowns(
            words, entries_of(held, period, narrow), currency, f' ({currency})' if several else '',
        )

    return context.report(
        SCOPE_BUSINESS, workspace.name,
        totals=totals,
        notes=[
            *context.notes(
                words.t('note.equation'),
                words.t('note.business_all'),
                words.t('note.opening', date=words.date(period.date_from)),
            ),
            *([words.t('note.currencies')] if several else []),
        ],
        tables=[
            *book_tables,
            *breakdown_tables,
            *(entries_table(words, book, period, numbers[book.id], narrow, title=book.book_name) for book in books),
        ],
    )
