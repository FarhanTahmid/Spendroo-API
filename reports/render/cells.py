"""How a raw cell reads as text. The PDF prints this; Excel keeps real values where it can."""
from ..document import COUNT, DATE, MONEY, SHARE


def text(words, column, value, table):
    if value is None or value == '':
        return ''
    if column.kind == DATE:
        return words.date(value, short=True)
    if column.kind == MONEY:
        # A table in one currency names it once, in its heading
        return words.amount(*value) if table.currency else words.money(*value)
    if column.kind == COUNT:
        return words.count(value)
    if column.kind == SHARE:
        return words.share(value)
    return str(value)


def tiles(words, report, totals):
    """The figures of one currency, in the order they are read."""
    if totals.narrowed:
        return [
            ('in', report.in_label, totals.in_minor),
            ('out', report.out_label, totals.out_minor),
            ('closing', words.t('tile.net'), totals.closing_minor),
        ]
    found = [
        ('opening', words.t('tile.opening'), totals.opening_minor),
        ('in', report.in_label, totals.in_minor),
        ('out', report.out_label, totals.out_minor),
    ]
    if totals.other_minor:
        found.append(('other', totals.other_label, totals.other_minor))
    found.append(('closing', words.t('tile.closing'), totals.closing_minor))
    return found
