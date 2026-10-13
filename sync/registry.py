"""
The tables a client can sync, and who may write to each.

Rows travel as flat dicts keyed by database column name (`cashbook_id`, not
`cashbook`). The same names are used by the client's local database.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from billing.catalog.keys import F
from billing.sync_gates import NO_GATES, Flag, Limit, Lock
from cashbook.models import CashBook, CashBookAdditionalMember, Entry, EntryCategory, PaymentMethod
from ledger_personal import allocations
from ledger_personal.models import Account, Budget, Category, Transaction
from reminders import engine
from reminders.models import InboxItem, Occurrence, Rule
from workspaces.models import Membership

SYNC_COLUMNS = ['id', 'workspace_id', 'version', 'server_seq', 'created_at', 'updated_at', 'deleted_at']


class SyncTable:
    #: columns a client may set
    writable = []
    #: columns that cannot change once the row exists
    immutable = []
    #: extra columns sent on pull but never accepted on push
    read_only = []
    #: foreign key column -> (model, must share this other column with the row or None)
    references = {}
    #: what the owner's plan limits here (billing/sync_gates.py), checked on
    #: every create and update. Each table says so itself, NO_GATES included:
    #: a test fails for a table that leaves this unset.
    plan_gates = None
    #: False for rows only the server makes or removes; a client may still edit `writable`
    client_creates = True
    client_deletes = True

    def __init__(self, name, model):
        self.name = name
        self.model = model

    def to_row(self, obj):
        columns = SYNC_COLUMNS + self.writable + self.read_only
        return {column: getattr(obj, column) for column in columns}

    def visible(self, queryset, user, role):
        """Narrow a workspace's rows to what this member may see."""
        return queryset

    def can_write(self, user, role, obj, op):
        """op is 'create', 'update' or 'delete'. obj already carries the new values."""
        return False

    def on_write(self, workspace, user, obj, creating, before):
        """
        Last look at a create or update before it is saved, for rules that
        depend on what changed. `before` is as the plan gates get it. Raise
        ValidationError to refuse.
        """


class WorkspaceLevelTable(SyncTable):
    """Personal-ledger tables: only the people who run the workspace."""

    def visible(self, queryset, user, role):
        return queryset if role in Membership.MANAGER_ROLES else queryset.none()

    def can_write(self, user, role, obj, op):
        return role in Membership.MANAGER_ROLES


class CashBookTable(SyncTable):
    writable = ['book_name', 'description', 'currency']
    immutable = ['currency']
    plan_gates = [Limit(F.BUSINESS_CASHBOOKS), Lock(F.BUSINESS_CASHBOOKS)]

    def visible(self, queryset, user, role):
        if role in (*Membership.MANAGER_ROLES, Membership.ROLE_VIEWER):
            return queryset
        return queryset.filter(id__in=accessible_cashbook_ids(user, role))

    def can_write(self, user, role, obj, op):
        if op == 'create':
            return role in Membership.MANAGER_ROLES
        if op == 'delete':
            return role == Membership.ROLE_OWNER
        return obj.has_permission(user, 'edit')


class CashBookChildTable(SyncTable):
    """Rows that hang off a cashbook and follow its per-book permissions."""
    required_permission = 'edit'
    # Read-only together with the book they belong to
    plan_gates = [Lock(F.BUSINESS_CASHBOOKS, target=lambda row: row.cashbook_id)]

    def visible(self, queryset, user, role):
        if role in (*Membership.MANAGER_ROLES, Membership.ROLE_VIEWER):
            return queryset
        return queryset.filter(cashbook__cashbookadditionalmember__member=user,
                               cashbook__cashbookadditionalmember__deleted_at__isnull=True)

    def can_write(self, user, role, obj, op):
        return obj.cashbook.has_permission(user, self.required_permission)


def accessible_cashbook_ids(user, role):
    if role in (*Membership.MANAGER_ROLES, Membership.ROLE_VIEWER):
        return CashBook.all_objects.values('id')
    return CashBookAdditionalMember.objects.filter(member=user).values('cashbook_id')


class EntryCategoryTable(CashBookChildTable):
    writable = ['cashbook_id', 'category_name', 'is_default', 'color']
    immutable = ['cashbook_id']
    references = {'cashbook_id': (CashBook, None)}
    required_permission = 'admin'


class PaymentMethodTable(CashBookChildTable):
    writable = ['cashbook_id', 'payment_method_name', 'is_default']
    immutable = ['cashbook_id']
    references = {'cashbook_id': (CashBook, None)}
    required_permission = 'admin'


class EntryTable(CashBookChildTable):
    writable = [
        'cashbook_id', 'category_id', 'payment_method_id', 'entry_type', 'amount_minor',
        'title', 'remarks', 'entry_date', 'source',
    ]
    immutable = ['cashbook_id']
    read_only = ['currency', 'created_by_id']
    references = {
        'cashbook_id': (CashBook, None),
        'category_id': (EntryCategory, 'cashbook_id'),
        'payment_method_id': (PaymentMethod, 'cashbook_id'),
    }


class CashBookMemberTable(CashBookChildTable):
    """Per-book grants. Managed online through the REST API; clients only read them."""
    read_only = ['cashbook_id', 'member_id', 'role']
    # Never written through sync; the REST endpoint that adds a member checks seats
    plan_gates = NO_GATES

    def visible(self, queryset, user, role):
        if role in Membership.MANAGER_ROLES:
            return queryset
        return queryset.filter(member=user)

    def can_write(self, user, role, obj, op):
        return False


class AccountTable(WorkspaceLevelTable):
    writable = ['name', 'kind', 'currency', 'opening_balance_minor', 'is_archived']
    immutable = ['currency']
    plan_gates = [
        # Archived accounts do not count, so bringing one back needs room like a new one
        Limit(F.PERSONAL_ACCOUNTS, counts=lambda account: not account.is_archived),
        # A locked account can still be archived
        Lock(F.PERSONAL_ACCOUNTS, unless=lambda account: account.is_archived),
    ]


class CategoryTable(WorkspaceLevelTable):
    writable = ['name', 'kind', 'color']
    # Set by the server on the categories an account starts with
    read_only = ['is_default']
    plan_gates = [
        Limit(F.PERSONAL_CUSTOM_CATEGORIES, counts=lambda category: not category.is_default),
        Lock(F.PERSONAL_CUSTOM_CATEGORIES),
    ]


class TransactionTable(WorkspaceLevelTable):
    writable = [
        'account_id', 'category_id', 'kind', 'amount_minor', 'transfer_group_id',
        'occurred_on', 'note', 'source', 'splits', 'occurrence_id',
    ]
    # Which date of a recurring rule it settles is said once, when it is recorded
    immutable = ['occurrence_id']
    read_only = ['currency']
    references = {
        'account_id': (Account, None), 'category_id': (Category, None), 'occurrence_id': (Occurrence, None),
    }
    # Read-only together with the account they are on
    plan_gates = [Lock(F.PERSONAL_ACCOUNTS, target=lambda transaction: transaction.account_id)]

    def on_write(self, workspace, user, obj, creating, before):
        # Only when the split itself is sent: a category deleted since must not block other edits
        if obj.splits and (creating or 'splits' in before):
            allocations.check_categories(workspace, obj.splits)


class BudgetTable(WorkspaceLevelTable):
    writable = ['category_id', 'amount_minor', 'currency', 'month']
    references = {'category_id': (Category, None)}
    # The recurring budget is for everyone; a different limit for one month is a plan feature
    plan_gates = [Flag(F.BUDGET_MONTH_OVERRIDE, when=lambda budget: budget.month is not None)]


class RuleTable(WorkspaceLevelTable):
    writable = [
        'kind', 'title', 'account_id', 'to_account_id', 'category_id', 'amount_minor', 'note', 'splits',
        'freq', 'interval', 'weekday_mask', 'month_day', 'set_pos', 'week_start', 'start_on', 'end_on', 'max_count',
        'mode', 'remind_days_before', 'remind_at', 'is_paused',
    ]
    # A reminder does not turn into a transaction, nor the other way
    immutable = ['kind']
    read_only = ['currency', 'next_due_on', 'done_count']
    references = {'account_id': (Account, None), 'to_account_id': (Account, None), 'category_id': (Category, None)}
    plan_gates = [
        # Recording transactions on a schedule is the plan feature; plain reminders are for everyone, a few of them
        Flag(F.RECURRING_TRANSACTIONS, when=lambda rule: not rule.is_reminder),
        Limit(F.REMINDERS_STANDALONE, counts=lambda rule: rule.is_reminder and not rule.is_paused),
        # A locked reminder can still be paused, which is one way back under the limit
        Lock(F.REMINDERS_STANDALONE, unless=lambda rule: not rule.is_reminder or rule.is_paused),
        Lock(F.PERSONAL_ACCOUNTS, target=lambda rule: rule.account_id),
    ]

    def on_write(self, workspace, user, obj, creating, before):
        if obj.splits and (creating or 'splits' in before):
            allocations.check_categories(workspace, obj.splits)
        if creating or any(column.removesuffix('_id') in Rule.TIMING_FIELDS for column in before):
            # The dates to come are worked out again, as soon as this is saved
            obj.needs_refresh = True
            obj.ended = False
            transaction.on_commit(lambda: engine.refresh(obj.pk), robust=True)


class OccurrenceTable(WorkspaceLevelTable):
    """Made by the server. The app skips one, puts one off, or ticks off a plain reminder."""
    writable = ['status', 'snoozed_until']
    read_only = ['rule_id', 'due_on']
    plan_gates = NO_GATES
    client_creates = False
    client_deletes = False

    def on_write(self, workspace, user, obj, creating, before):
        was = before.get('status', obj.status)
        if was == obj.status:
            return
        allowed = {(Occurrence.UPCOMING, Occurrence.SKIPPED), (Occurrence.DUE, Occurrence.SKIPPED)}
        if obj.rule.is_reminder:
            # Nothing is recorded for a reminder, so the app says when it is done
            allowed.add((Occurrence.DUE, Occurrence.DONE))
        if (was, obj.status) not in allowed:
            raise ValidationError(f'An occurrence cannot go from {was} to {obj.status}.')
        transaction.on_commit(lambda: engine.settled(obj.pk), robust=True)


class InboxTable(WorkspaceLevelTable):
    """Made by the server. The app marks them read."""
    writable = ['read_at']
    read_only = ['type', 'params', 'subject_table', 'subject_id', 'dedupe_key', 'resolved_at', 'notify_at']
    plan_gates = NO_GATES
    client_creates = False
    client_deletes = False


# Parents before children: a push batch is applied in the order sent, and a
# pull is applied by the client in this order.
TABLES = {
    table.name: table for table in [
        CashBookTable('cashbooks', CashBook),
        EntryCategoryTable('entry_categories', EntryCategory),
        PaymentMethodTable('payment_methods', PaymentMethod),
        EntryTable('entries', Entry),
        CashBookMemberTable('cashbook_members', CashBookAdditionalMember),
        AccountTable('accounts', Account),
        CategoryTable('categories', Category),
        RuleTable('recurring_rules', Rule),
        OccurrenceTable('recurring_occurrences', Occurrence),
        TransactionTable('transactions', Transaction),
        BudgetTable('budgets', Budget),
        InboxTable('inbox_items', InboxItem),
    ]
}
