from django.urls import path

from .views import AccountSummaryView, AccountTransactionsView, GoalSummaryView, WorkspaceTrendsView, WorkspaceYearReviewView

app_name = 'ledger'

urlpatterns = [
    path('accounts/<uuid:account_id>/transactions/', AccountTransactionsView.as_view(), name='account-transactions'),
    path('accounts/<uuid:account_id>/summary/', AccountSummaryView.as_view(), name='account-summary'),
    path('workspaces/<uuid:workspace_id>/trends/', WorkspaceTrendsView.as_view(), name='workspace-trends'),
    path('workspaces/<uuid:workspace_id>/year-review/', WorkspaceYearReviewView.as_view(), name='workspace-year-review'),
    path('goals/<uuid:goal_id>/summary/', GoalSummaryView.as_view(), name='goal-summary'),
]
