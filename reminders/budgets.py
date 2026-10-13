"""
Tells the user when a budget is nearly spent, and when it is gone. Looked at
after transactions or budgets change; each budget says each thing once a month.
"""
import calendar

from django.utils import timezone

from ledger_personal import allocations
from ledger_personal.models import Budget, Transaction
from workspaces.models import Workspace

from . import inbox

#: Share of a budget, in percent, at which the user is warned
WARN_AT = 80
BUDGETS = 'budgets'


def in_force(workspace, month):
    """The budget of each category for the month: its override if there is one, else the every-month one."""
    budgets = {}
    for budget in Budget.objects.filter(workspace=workspace, month__isnull=True).select_related('category'):
        budgets[budget.category_id] = budget
    for budget in Budget.objects.filter(workspace=workspace, month=month).select_related('category'):
        budgets[budget.category_id] = budget
    return {category_id: budget for category_id, budget in budgets.items() if budget.category.deleted_at is None}


def spent_by_category(workspace, first, last):
    """
    Expenses of the period per category, positive, each part of a split under
    its own. Added up as the app's budgets screen does (ledger_repository.dart
    watchBudgets), so the alert and the screen never disagree.
    """
    spent = {}
    rows = Transaction.objects.filter(
        workspace=workspace, kind='expense', occurred_on__range=(first, last),
    ).values('amount_minor', 'category_id', 'splits')
    for row in rows:
        for category_id, amount, _ in allocations.parts(row['amount_minor'], row['category_id'], row['splits']):
            if category_id is not None:
                spent[category_id] = spent.get(category_id, 0) - amount
    return spent


def evaluate(workspace_id, now=None):
    now = now or timezone.now()
    workspace = Workspace.objects.filter(pk=workspace_id).first()
    if workspace is None:
        return
    today = inbox.today_in(workspace, now)
    month = today.replace(day=1)
    budgets = in_force(workspace, month)
    if not budgets:
        return
    spent = spent_by_category(workspace, month, month.replace(day=calendar.monthrange(month.year, month.month)[1]))
    for category_id, budget in budgets.items():
        used = spent.get(category_id, 0)
        if used > budget.amount_minor:
            kind, mark = 'budget_over', 100
        elif used * 100 >= budget.amount_minor * WARN_AT:
            kind, mark = 'budget_80', WARN_AT
        else:
            continue
        inbox.tell(
            workspace, kind, f'budget:{category_id}:{month:%Y-%m}:{mark}',
            {
                'category': budget.category.name, 'category_id': str(category_id), 'amount_minor': budget.amount_minor,
                'spent_minor': used, 'currency': budget.currency, 'month': f'{month:%Y-%m}',
                'percent': used * 100 // budget.amount_minor,
            },
            BUDGETS, budget.id, now,
            # Something to know, nothing to answer
            resolved=True,
        )
