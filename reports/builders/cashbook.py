"""One cashbook: what it held, where money came from and went, entry by entry."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Q, Sum

from cashbook.models import Entry, EntryField

from .. import narrow as narrowing
from ..document import DATE, MONEY, NUMBER, TEXT, TONE_BALANCE, TONE_IN, TONE_OUT, Column, Money, Table, Totals, net
from ..models import SCOPE_CASHBOOK
from .common import alive_name, in_out_table, money

CASH_IN = Q(entry_type='cash_in')
CASH_OUT = Q(entry_type='cash_out')


def entries_of(books, period, narrow):
    """The entries of these cashbooks a report of the period shows."""
    return narrowing.lines(
        Entry.objects.filter(cashbook__in=books, entry_date__range=(period.date_from, period.date_to)),
        narrow, 'entry_type',
    )


def totals_of(currency, numbers, narrow):
    """The figures at the top, from one cashbook's numbers or several added up."""
    return net(currency, numbers[1], numbers[2]) if narrow.active else Totals(currency, *numbers)


def figures(books, period, narrow=narrowing.EVERYTHING):
    """
    Per cashbook: (opening, in, out, closing) for the period. Narrowed, the
    opening is nothing and the last is in less out: lines are missing, so
    there is no balance to give.
    """
    before = Q(entry_date__lt=period.date_from)
    inside = Q(entry_date__range=(period.date_from, period.date_to))
    entries = narrowing.lines(Entry.objects.filter(cashbook__in=books), narrow, 'entry_type')
    if narrow.active:
        before = Q(pk__in=[])
    rows = entries.order_by().values('cashbook_id').annotate(
        before_in=Sum('amount_minor', filter=before & CASH_IN),
        before_out=Sum('amount_minor', filter=before & CASH_OUT),
        cash_in=Sum('amount_minor', filter=inside & CASH_IN),
        cash_out=Sum('amount_minor', filter=inside & CASH_OUT),
    )
    found = {row['cashbook_id']: row for row in rows}
    result = {}
    for book in books:
        row = found.get(book.id, {})
        opening = (row.get('before_in') or 0) - (row.get('before_out') or 0)
        cash_in, cash_out = row.get('cash_in') or 0, row.get('cash_out') or 0
        result[book.id] = (opening, cash_in, cash_out, opening + cash_in - cash_out)
    return result


def breakdown(words, entries, relation, field, title, first_column, missing, currency):
    """Money in and out per category, or per payment method, over these entries."""
    rows = (
        entries.order_by().values(name=alive_name(relation, field))
        .annotate(cash_in=Sum('amount_minor', filter=CASH_IN), cash_out=Sum('amount_minor', filter=CASH_OUT), n=Count('id'))
    )
    return in_out_table(
        words, title, first_column,
        [(row['name'] or missing, row['n'], row['cash_in'] or 0, row['cash_out'] or 0) for row in rows],
        currency,
    )


def breakdowns(words, entries, currency, suffix=''):
    return [
        breakdown(
            words, entries, 'category', 'category_name', words.t('table.by_category') + suffix,
            words.t('col.category'), words.t('row.uncategorised'), currency,
        ),
        breakdown(
            words, entries, 'payment_method', 'payment_method_name', words.t('table.by_method') + suffix,
            words.t('col.method'), words.t('row.no_method'), currency,
        ),
    ]


def _alive(row, field):
    return getattr(row, field) if row is not None and row.deleted_at is None else ''


FIELD_KINDS = {EntryField.NUMBER: NUMBER, EntryField.DATE: DATE}
# The Details column, which the PDF prints a book's own fields under
DETAILS = 1


def _typed(field, raw):
    """A field's value as the report shows it: a number or date where it reads as one, else as kept."""
    if not raw:
        return None
    try:
        if field.kind == EntryField.NUMBER:
            return Decimal(raw)
        if field.kind == EntryField.DATE:
            return date.fromisoformat(raw)
    except (InvalidOperation, ValueError):
        pass
    return raw


def entries_table(words, book, period, numbers, narrow=narrowing.EVERYTHING, title=None):
    """
    Entry by entry. Showing every entry, each carries the balance after it.
    The book's own fields come last, so a book without any keeps its shape.
    """
    currency = book.currency
    fields = list(EntryField.objects.filter(cashbook=book))
    opening, cash_in, cash_out, closing = numbers
    running = not narrow.active
    lines = (
        entries_of([book], period, narrow)
        .select_related('category', 'payment_method', 'created_by')
        .order_by('entry_date', 'created_at', 'id')
    )
    rows, balance = [], opening
    for line in lines:
        incoming = line.entry_type == 'cash_in'
        balance += line.amount_minor if incoming else -line.amount_minor
        author = line.created_by
        rows.append([
            line.entry_date, line.title or line.remarks or '',
            _alive(line.category, 'category_name'), _alive(line.payment_method, 'payment_method_name'),
            (author.full_name or author.username) if author else '',
            Money(line.amount_minor, currency) if incoming else None,
            None if incoming else Money(line.amount_minor, currency),
            *([Money(balance, currency)] if running else []),
            *[_typed(field, (line.custom_values or {}).get(str(field.id))) for field in fields],
        ])
    sums = [money(cash_in, currency), money(cash_out, currency)]
    blank = [None] * len(fields)
    return Table(
        title=title or words.t('table.entries'), currency=currency, is_detail=True, empty=words.t('empty.entries'),
        columns=[
            Column(words.t('col.date'), DATE, width=1.5),
            Column(words.t('col.details'), width=2.6),
            Column(words.t('col.category'), width=1.7),
            Column(words.t('col.method'), width=1.5),
            Column(words.t('col.entered_by'), width=1.9),
            Column(words.t('col.in'), MONEY, TONE_IN, width=1.45),
            Column(words.t('col.out'), MONEY, TONE_OUT, width=1.45),
            *([Column(words.t('col.balance'), MONEY, TONE_BALANCE, width=1.55)] if running else []),
            *[Column(field.name, FIELD_KINDS.get(field.kind, TEXT), width=1.6, under=DETAILS) for field in fields],
        ],
        first=(
            [period.date_from, words.t('row.opening'), '', '', '', None, None, Money(opening, currency), *blank]
            if running else None
        ),
        rows=rows,
        total=(
            [period.date_to, words.t('row.closing'), '', '', '', *sums, Money(closing, currency), *blank] if running
            else ([None, words.t('row.total'), '', '', '', *sums, *blank] if rows else None)
        ),
    )


def build(book, context):
    words, period, narrow = context.words, context.period, context.narrow
    numbers = figures([book], period, narrow)[book.id]
    return context.report(
        SCOPE_CASHBOOK, book.book_name,
        totals=[totals_of(book.currency, numbers, narrow)],
        notes=context.notes(
            words.t('note.equation'),
            words.t('note.opening', date=words.date(period.date_from)),
            words.t('note.running'),
        ),
        tables=[
            *breakdowns(words, entries_of([book], period, narrow), book.currency),
            entries_table(words, book, period, numbers, narrow),
        ],
    )
