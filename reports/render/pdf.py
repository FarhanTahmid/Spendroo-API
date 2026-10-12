"""
A Report as a PDF.

WeasyPrint lays out HTML with Pango, which shapes Bangla correctly. The page
furniture every report carries (the watermark, the footer with the reference
and the page number) lives in templates/reports/report.css.
"""
from pathlib import Path

from django.template.loader import render_to_string

from ..labels import Words
from . import cells

ASSETS = Path(__file__).resolve().parent.parent / 'assets'
# The three boxes of the page footer share one look; CSS cannot group them in one rule
FOOTER_STYLE = (
    'font-family: Anek; font-size: 7.5pt; color: #5B6B84; '
    'border-top: 0.6pt solid #E2E8F1; padding-top: 2.5mm; vertical-align: top; white-space: pre;'
)


def css_string(value):
    """Text as a CSS string literal, for `content:` in a page margin."""
    cleaned = ' '.join(str(value).split())
    return '"' + cleaned.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _row(words, table, values, kind=''):
    return {
        'kind': kind,
        'cells': [
            {'text': cells.text(words, column, value, table), 'kind': column.kind, 'tone': column.tone}
            for column, value in zip(table.columns, values)
        ],
    }


def _table(words, table):
    width = sum(column.width for column in table.columns)
    return {
        'title': table.title,
        'note': table.note,
        'empty': table.empty,
        'amounts_in': words.t('table.amounts_in', currency=table.currency) if table.currency else '',
        'columns': [
            {'label': column.label, 'kind': column.kind, 'width': f'{column.width / width * 100:.2f}'}
            for column in table.columns
        ],
        'first': _row(words, table, table.first, 'edge') if table.first else None,
        'rows': [_row(words, table, values) for values in table.rows],
        'total': _row(words, table, table.total, 'edge total') if table.total else None,
    }


def context(report):
    words = Words(report.language)
    several = len(report.totals) > 1
    return {
        'report': report,
        # Dots do not survive a template lookup: t.head_period
        't': {key.replace('.', '_'): text for key, text in words.all().items()},
        'assets': ASSETS.as_uri(),
        'period': words.period(report.date_from, report.date_to),
        'generated': words.moment(report.generated_at),
        'filters': [{'label': label, 'chosen': chosen} for label, chosen in report.filters],
        'glance': [
            {
                'currency': totals.currency if several else '',
                'tiles': [
                    {'kind': kind, 'label': label, 'value': words.money(amount, totals.currency)}
                    for kind, label, amount in cells.tiles(words, report, totals)
                ],
            }
            for totals in report.totals
        ],
        'breakdowns': [_table(words, table) for table in report.breakdowns],
        'details': [_table(words, table) for table in report.details],
        'footer_left': css_string(f"{words.t('footer.by')}  ·  {report.reference}"),
        'footer_center': css_string(f'{report.title}: {report.subject}'),
        # Our own words, with the spaces around them that css_string would trim
        'page_word': f'"{words.t("footer.page")} "',
        'of_word': f'" {words.t("footer.of")} "',
        'footer_style': FOOTER_STYLE,
        'counter_style': 'bengali' if report.language == 'bn' else 'decimal',
    }


def render(report):
    """The report as PDF bytes."""
    # Imported here: WeasyPrint loads Pango, which only this function needs
    from weasyprint import HTML

    html = render_to_string('reports/report.html', context(report))
    return HTML(string=html, base_url=str(ASSETS)).write_pdf()
