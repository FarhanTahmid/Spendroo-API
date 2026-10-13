"""
Every feature the server knows how to gate.

A key is declared here because code has to enforce it; what each plan gets for
it is data (PlanFeature rows, edited in the admin). To gate something new:
add a key here, give it values in catalog/defaults.py, call a gate from
billing/gates.py where the feature is used.

Kinds:
- flag:  on or off
- limit: how many may exist at one time (or unlimited)
- quota: how much may be used per calendar month (or unlimited)
"""
from dataclasses import dataclass

FLAG = 'flag'
LIMIT = 'limit'
QUOTA = 'quota'
KINDS = (FLAG, LIMIT, QUOTA)


@dataclass(frozen=True)
class FeatureKey:
    key: str
    kind: str
    name: str
    description: str = ''
    unit: str = ''

    def __str__(self):
        return self.key


class F:
    # Personal ledger
    PERSONAL_ACCOUNTS = FeatureKey(
        'personal.accounts', LIMIT, 'Personal accounts',
        'Accounts in use at one time. Archived accounts do not count.',
    )
    PERSONAL_CUSTOM_CATEGORIES = FeatureKey(
        'personal.custom_categories', LIMIT, 'Custom categories',
        'Categories the user added. The ones every account starts with do not count.',
    )
    BUDGET_MONTH_OVERRIDE = FeatureKey(
        'budgets.month_override', FLAG, 'One-month budget overrides',
        'A different limit for one month without changing the recurring one.',
    )
    INSIGHTS_CUSTOM_RANGE = FeatureKey('insights.custom_range', FLAG, 'Insights: custom date ranges')
    INSIGHTS_YEAR_REVIEW = FeatureKey('insights.year_review', FLAG, 'Insights: year in review')
    INSIGHTS_TREND_MONTHS = FeatureKey(
        'insights.trend_months', LIMIT, 'Insights: trend length', 'Longest trend the app may draw.', unit='months',
    )
    RECURRING_TRANSACTIONS = FeatureKey(
        'recurring.transactions', FLAG, 'Recurring transactions and reminders',
        'Transactions recorded or asked about on a schedule. Plain reminders are counted on their own.',
    )
    REMINDERS_STANDALONE = FeatureKey(
        'reminders.standalone', LIMIT, 'Reminders',
        'Reminders with nothing to record, in use at one time. Paused ones do not count.',
    )
    SAVINGS_GOALS = FeatureKey('goals.savings', FLAG, 'Savings goals')
    HOME_WIDGET = FeatureKey('widget.home', FLAG, 'Home-screen widget')

    # Business
    BUSINESS_WORKSPACES = FeatureKey(
        'business.workspaces', LIMIT, 'Business workspaces',
        'Businesses the user owns. The demo business does not count.',
    )
    BUSINESS_CASHBOOKS = FeatureKey('business.cashbooks', LIMIT, 'Cashbooks per workspace')
    TEAM_SEATS = FeatureKey(
        'team.seats', LIMIT, 'Team members per workspace', 'People other than the owner.',
    )
    AUDIT_TRAIL = FeatureKey('history.audit_trail', FLAG, 'Owner-visible edit history')
    ENTRY_CUSTOM_FIELDS = FeatureKey('entries.custom_fields', FLAG, 'Custom fields on entries')
    STORAGE_ATTACHMENTS_MB = FeatureKey(
        'storage.attachments_mb', LIMIT, 'Bill attachments', 'Total size of attached files.', unit='MB',
    )

    # Reports to download (reports/). A PDF of a preset period is for every plan.
    REPORTS_EXCEL = FeatureKey(
        'reports.excel', FLAG, 'Reports as Excel', 'Download any report as an Excel workbook. PDF is always included.',
    )
    REPORTS_CUSTOM_RANGE = FeatureKey(
        'reports.custom_range', FLAG, 'Custom reports',
        'A report between two chosen dates, narrowed to chosen kinds of transaction and categories.',
    )

    # AI
    AI_CREDITS = FeatureKey('ai.credits', QUOTA, 'AI credits', 'Pooled across every AI action.', unit='credits')
    STATEMENT_PDF_PAGES = FeatureKey('statements.pdf_pages', QUOTA, 'Statement upload', unit='PDF pages')

    # Account
    DEVICES_MAX = FeatureKey('devices.max', LIMIT, 'Signed-in devices', 'Shown to the user; not enforced yet.')
    ADS = FeatureKey('ads.enabled', FLAG, 'Shows ads', 'On for plans that see ads.')


ALL = [value for value in vars(F).values() if isinstance(value, FeatureKey)]
BY_KEY = {feature.key: feature for feature in ALL}
