"""What the four builders share."""
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Case, CharField, F, When

from .. import narrow as narrowing
from ..document import COUNT, MONEY, SHARE, TONE_IN, TONE_OUT, Column, Money, Report, Table
from ..labels import Words
from ..periods import Period


@dataclass(frozen=True)
class Context:
    """Who asked for the report, for when, and how it is to be worded."""
    user: object
    period: Period
    words: Words
    reference: str
    generated_at: datetime
    #: What a custom report is narrowed to, beside its dates
    narrow: narrowing.Narrow = narrowing.EVERYTHING

    def notes(self, *full, net='note.net'):
        """How to read the report: `full` when it shows everything, else what a filtered one means."""
        if not self.narrow.active:
            return list(full)
        return [self.words.t('note.filtered'), self.words.t(net), self.words.t('note.no_balances')]

    def report(self, scope, subject, **parts):
        words = self.words
        return Report(
            scope=scope, title=words.t(f'title.{scope}'), subject=subject, covers=words.t(f'covers.{scope}'),
            date_from=self.period.date_from, date_to=self.period.date_to,
            prepared_for=self.user.full_name or self.user.username, generated_at=self.generated_at,
            reference=self.reference, language=words.language,
            in_label=words.t('tile.in'), out_label=words.t('tile.out'),
            filters=narrowing.describe(self.narrow, words),
            **parts,
        )


def alive_name(relation, field):
    """The name of a related row, or nothing once that row is deleted, as the app shows it."""
    return Case(
        When(**{f'{relation}__deleted_at__isnull': True}, then=F(f'{relation}__{field}')),
        output_field=CharField(),
    )


def money(amount_minor, currency):
    """A cell; an amount of nothing is left blank so the eye finds the ones that matter."""
    return Money(amount_minor, currency) if amount_minor else None


def by_currency(items, currency_of):
    """Groups in the order each currency first appears."""
    groups = {}
    for item in items:
        groups.setdefault(currency_of(item), []).append(item)
    return groups


def titled(title, currency, several):
    """A table per currency says which one it is."""
    return f'{title} ({currency})' if several else title


def share_table(words, title, rows, currency, tone, empty_key='empty.categories'):
    """
    Name, how many, amount and share of the whole, largest first.
    `rows` is (name, count, amount_minor) with amounts already positive.
    """
    rows = sorted(rows, key=lambda row: -row[2])
    whole = sum(row[2] for row in rows)
    return Table(
        title=title, currency=currency, empty=words.t(empty_key),
        columns=[
            Column(words.t('col.category'), width=3),
            Column(words.t('col.count'), COUNT),
            Column(words.t('col.amount'), MONEY, tone, width=1.6),
            Column(words.t('col.share'), SHARE),
        ],
        rows=[[name, count, Money(amount, currency), amount / whole if whole else 0] for name, count, amount in rows],
        total=[words.t('row.total'), sum(row[1] for row in rows), Money(whole, currency), None] if rows else None,
    )


def in_out_table(words, title, first_column, rows, currency):
    """
    Name, how many, money in and money out, biggest mover first.
    `rows` is (name, count, in_minor, out_minor).
    """
    rows = sorted(rows, key=lambda row: -(row[2] + row[3]))
    return Table(
        title=title, currency=currency, empty=words.t('empty.categories'),
        columns=[
            Column(first_column, width=3),
            Column(words.t('col.count'), COUNT),
            Column(words.t('col.in'), MONEY, TONE_IN, width=1.6),
            Column(words.t('col.out'), MONEY, TONE_OUT, width=1.6),
        ],
        rows=[[name, count, money(cash_in, currency), money(cash_out, currency)] for name, count, cash_in, cash_out in rows],
        total=[
            words.t('row.total'), sum(row[1] for row in rows),
            Money(sum(row[2] for row in rows), currency), Money(sum(row[3] for row in rows), currency),
        ] if rows else None,
    )
