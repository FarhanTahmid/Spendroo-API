"""
The words of a push notification, in English and Bangla. They say what the
app's reminders screen says for the same item (lib/l10n/app_*.arb, the inbox*
keys, and inboxTitle in lib/features/recurring/inbox.dart): change both together.
"""
from spendroo.money import format_money

LANGUAGES = ('en', 'bn')
DEFAULT_LANGUAGE = 'en'

_BANGLA_DIGITS = str.maketrans('0123456789', '০১২৩৪৫৬৭৮৯')

TEXT = {
    'en': {
        'untitled': 'Untitled',
        'upcoming': '{title} is due in {days} days',
        'upcoming.tomorrow': '{title} is due tomorrow',
        'due': '{title} is due today',
        'overdue': '{title} is still waiting',
        'posted': '{title} was recorded',
        'income': '{title} is expected in {days} days',
        'income.tomorrow': '{title} is expected tomorrow',
        'income.due': 'Has {title} come in?',
        'blocked': '{title} could not be recorded. Check its account.',
        'budget_80': '{title}: {percent}% of the budget is spent',
        'budget_over': '{title} is over its budget',
        'budget.detail': '{spent} of {limit}',
        'confirm': 'Tap to confirm or skip.',
        'test.title': 'Notifications are working',
        'test.body': 'This is how Spendroo will remind you.',
    },
    'bn': {
        'untitled': 'নাম নেই',
        'upcoming': '{title}: আর {days} দিন বাকি',
        'upcoming.tomorrow': '{title}: আগামীকাল',
        'due': '{title}: আজ দেওয়ার দিন',
        'overdue': '{title} এখনও বাকি আছে',
        'posted': '{title} লেখা হয়েছে',
        'income': '{title} আসার কথা {days} দিন পর',
        'income.tomorrow': '{title} আসার কথা আগামীকাল',
        'income.due': '{title} কি এসেছে?',
        'blocked': '{title} লেখা যায়নি। এর অ্যাকাউন্টটি দেখুন।',
        'budget_80': '{title}: বাজেটের {percent}% খরচ হয়েছে',
        'budget_over': '{title}: বাজেট পেরিয়ে গেছে',
        'budget.detail': '{limit}-এর মধ্যে {spent}',
        'confirm': 'নিশ্চিত করতে বা বাদ দিতে চাপুন।',
        'test.title': 'নোটিফিকেশন কাজ করছে',
        'test.body': 'Spendroo এভাবেই আপনাকে মনে করিয়ে দেবে।',
    },
}


def language_of(locale):
    """'bn_BD' and 'bn' are Bangla; anything unknown is English."""
    language = (locale or '').replace('-', '_').split('_')[0].lower()
    return language if language in LANGUAGES else DEFAULT_LANGUAGE


def number(value, language):
    text = str(value)
    return text.translate(_BANGLA_DIGITS) if language == 'bn' else text


def say(key, language, **values):
    return TEXT[language][key].format(**values)


def _money(amount_minor, currency, language):
    return number(format_money(amount_minor, currency), language)


def _title(kind, params, language):
    title = params.get('title') or TEXT[language]['untitled']
    days = params.get('days') or 0
    if kind == 'upcoming':
        key = 'upcoming.tomorrow' if days <= 1 else 'upcoming'
    elif kind == 'income_expected':
        key = 'income.due' if 'days' not in params else ('income.tomorrow' if days <= 1 else 'income')
    elif kind == 'rule_blocked':
        key = 'blocked'
    elif kind in ('budget_80', 'budget_over'):
        return say(kind, language, title=params.get('category') or '', percent=number(params.get('percent', 80), language))
    elif kind in ('due', 'overdue', 'posted'):
        key = kind
    else:
        # A reminder says what the user wrote
        return title
    return say(key, language, title=title, days=number(days, language))


def _body(kind, params, language):
    amount, currency = params.get('amount_minor'), params.get('currency') or ''
    if amount is None or not currency:
        return ''
    if kind.startswith('budget_'):
        return say(
            'budget.detail', language,
            spent=_money(params.get('spent_minor') or 0, currency, language), limit=_money(amount, currency, language),
        )
    money = _money(amount, currency, language)
    # Something the user is to answer
    if kind in ('due', 'overdue') or (kind == 'income_expected' and 'days' not in params):
        return f'{money} · {say("confirm", language)}'
    return money


def wording(kind, params, locale):
    """(title, body) of the push for an inbox item of this type."""
    language = language_of(locale)
    return _title(kind, params or {}, language), _body(kind, params or {}, language)
