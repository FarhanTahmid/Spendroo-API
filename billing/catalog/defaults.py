"""
The tiers Spendroo starts with. `manage.py billing_seed` loads this into the
database; after that the admin is the source of truth and this file is only
the fallback for a fresh database and the reference the tests check against.
"""
from .keys import F

UNLIMITED = 'unlimited'

FREE = 'free'
PLUS = 'plus'
BUSINESS = 'business'

PLANS = [
    {
        'code': FREE, 'name': 'Free', 'name_bn': 'ফ্রি', 'rank': 0, 'sort': 0,
        'is_default': True, 'is_public': True,
        'tagline': 'Track your money, with a few ads.',
        'tagline_bn': 'কিছু বিজ্ঞাপনসহ আপনার টাকার হিসাব রাখুন।',
    },
    {
        'code': PLUS, 'name': 'Plus', 'name_bn': 'প্লাস', 'rank': 10, 'sort': 10,
        'is_default': False, 'is_public': True,
        'tagline': 'No ads, no limits on your own money, and AI that does the typing.',
        'tagline_bn': 'বিজ্ঞাপন নেই, নিজের হিসাবে কোনো সীমা নেই, আর লেখার কাজ করবে এআই।',
    },
    {
        'code': BUSINESS, 'name': 'Business', 'name_bn': 'বিজনেস', 'rank': 20, 'sort': 20,
        'is_default': False, 'is_public': True,
        'tagline': 'Run your shop with your team.',
        'tagline_bn': 'টিম নিয়ে আপনার ব্যবসা চালান।',
    },
]

PLAN_CODES = [plan['code'] for plan in PLANS]

# feature -> what (Free, Plus, Business) get
VALUES = {
    F.ADS: (True, False, False),

    F.PERSONAL_ACCOUNTS: (4, UNLIMITED, UNLIMITED),
    F.PERSONAL_CUSTOM_CATEGORIES: (10, UNLIMITED, UNLIMITED),
    F.BUDGET_MONTH_OVERRIDE: (False, True, True),
    F.INSIGHTS_CUSTOM_RANGE: (False, True, True),
    F.INSIGHTS_YEAR_REVIEW: (False, True, True),
    F.INSIGHTS_TREND_MONTHS: (6, 24, 24),
    F.RECURRING_TRANSACTIONS: (False, True, True),
    F.SAVINGS_GOALS: (False, True, True),
    F.HOME_WIDGET: (False, True, True),

    F.BUSINESS_WORKSPACES: (1, 2, 5),
    F.BUSINESS_CASHBOOKS: (2, UNLIMITED, UNLIMITED),
    F.TEAM_SEATS: (0, 0, 5),
    F.AUDIT_TRAIL: (False, False, True),
    F.ENTRY_CUSTOM_FIELDS: (False, False, True),
    F.STORAGE_ATTACHMENTS_MB: (0, 1024, 10240),

    F.REPORTS_EXCEL: (False, True, True),
    F.REPORTS_CUSTOM_RANGE: (False, True, True),

    F.AI_CREDITS: (15, 300, 1000),
    F.STATEMENT_PDF_PAGES: (5, 30, 150),

    F.DEVICES_MAX: (2, 5, 10),
}


def value_for(plan_code, feature):
    """What the plan gets by default: a bool for a flag, an int or UNLIMITED otherwise."""
    return VALUES[feature][PLAN_CODES.index(plan_code)]
