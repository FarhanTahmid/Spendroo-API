"""What the four builders share."""
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Case, CharField, F, When

from ledger_personal import allocations

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


def _kept(row, narrow, alive):
    return allocations.kept(
        allocations.parts(row['amount_minor'], row['category_id'], row['splits']),
        narrow.category_ids, narrow.uncategorised, alive,
    )


def filed(line, narrow, alive, words):
    """
    (category label, amount) of a personal transaction as a report shows it.
    Under a choice of categories a split counts for its chosen parts only.
    """
    if not line.splits:
        return line.category_name or words.t('row.uncategorised'), line.amount_minor
    kept = _kept({'amount_minor': line.amount_minor, 'category_id': None, 'splits': line.splits}, narrow, alive)
    names = dict.fromkeys(
        alive[category_id][0] if category_id in alive else words.t('row.uncategorised') for category_id, _, _ in kept
    )
    return words.t('row.split', names=', '.join(names)), sum(amount for _, amount, _ in kept)


def split_sums(queryset, narrow, alive):
    """kind -> what the split transactions of `queryset` count for under the choice of categories, signed."""
    sums = {}
    for row in allocations.split_only(queryset).order_by().values('kind', 'currency', 'amount_minor', 'splits'):
        row['category_id'] = None
        key = (row['currency'], row['kind'])
        sums[key] = sums.get(key, 0) + sum(amount for _, amount, _ in _kept(row, narrow, alive))
    return sums


def category_shares(rows, narrow, alive, words):
    """
    (name, count, amount) per category for share_table, each part of a split
    under its own. `rows` hold amount_minor, category_id and splits.
    """
    found = {}
    for row in rows:
        for category_id, amount, _ in _kept(row, narrow, alive):
            # A deleted category reads as none, as the app shows it
            name = alive[category_id][0] if category_id in alive else words.t('row.uncategorised')
            total = found.setdefault(name, [0, 0])
            total[0] += 1
            total[1] += abs(amount)
    return [(name, count, amount) for name, (count, amount) in found.items()]


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
