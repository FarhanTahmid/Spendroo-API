"""
What a report says, before it becomes a file.

Every builder returns a Report and every renderer reads one, so a PDF and an
Excel workbook of the same report cannot disagree. Cells hold raw values
(dates, Money, numbers); a renderer decides how they look.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import NamedTuple

TEXT = 'text'
DATE = 'date'
MONEY = 'money'
COUNT = 'count'
SHARE = 'share'

# How a money column is coloured
TONE_PLAIN = 'plain'
TONE_IN = 'in'
TONE_OUT = 'out'
TONE_BALANCE = 'balance'


class Money(NamedTuple):
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class Column:
    label: str
    kind: str = TEXT
    tone: str = TONE_PLAIN
    #: Share of the table's width, relative to the other columns
    width: float = 1.0


@dataclass
class Table:
    title: str
    columns: list
    rows: list = field(default_factory=list)
    #: A row printed before the first / after the last, in bold (same shape as a row)
    first: list | None = None
    total: list | None = None
    #: Said under the title: what the table shows, in a sentence
    note: str = ''
    #: Said instead of the rows when there are none
    empty: str = ''
    #: When every amount is in one currency it is named once, in the title
    currency: str = ''
    #: The line-by-line part of the report, as opposed to a breakdown
    is_detail: bool = False


@dataclass(frozen=True)
class Totals:
    """The four figures every report opens with, for one currency."""
    currency: str
    opening_minor: int
    in_minor: int
    out_minor: int
    closing_minor: int
    #: What the four do not explain, with its label; shown only when not zero
    other_minor: int = 0
    other_label: str = ''
    #: A narrowed report has no balances: closing_minor is then in less out
    narrowed: bool = False


def net(currency, in_minor, out_minor):
    """The figures of a narrowed report: what came in, what went out, the difference."""
    return Totals(currency, 0, in_minor, out_minor, in_minor - out_minor, narrowed=True)


@dataclass
class Report:
    scope: str
    #: "Account statement"
    title: str
    #: "Brac Bank"
    subject: str
    #: One line saying what the report covers
    covers: str
    date_from: date
    date_to: date
    prepared_for: str
    generated_at: datetime
    reference: str
    language: str
    #: What the two middle figures are called here: "Money in" or "Income"
    in_label: str = ''
    out_label: str = ''
    #: What a custom report was narrowed to, as (label, choice); empty when it shows everything
    filters: list = field(default_factory=list)
    totals: list = field(default_factory=list)
    #: "How to read this report", a sentence each
    notes: list = field(default_factory=list)
    tables: list = field(default_factory=list)

    @property
    def row_count(self):
        return sum(len(table.rows) for table in self.tables if table.is_detail)

    @property
    def breakdowns(self):
        return [table for table in self.tables if not table.is_detail]

    @property
    def details(self):
        return [table for table in self.tables if table.is_detail]
