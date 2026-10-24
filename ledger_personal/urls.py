from django.urls import path

from .receipt_views import TransactionReceiptLinkView, TransactionReceiptView, TransactionReceiptsView
from .views import AccountSummaryView, AccountTransactionsView, GoalSummaryView, WorkspaceTrendsView, WorkspaceYearReviewView

app_name = 'ledger'

urlpatterns = [
    path('accounts/<uuid:account_id>/transactions/', AccountTransactionsView.as_view(), name='account-transactions'),
    path('accounts/<uuid:account_id>/summary/', AccountSummaryView.as_view(), name='account-summary'),
    path('workspaces/<uuid:workspace_id>/trends/', WorkspaceTrendsView.as_view(), name='workspace-trends'),
    path('workspaces/<uuid:workspace_id>/year-review/', WorkspaceYearReviewView.as_view(), name='workspace-year-review'),
    path('goals/<uuid:goal_id>/summary/', GoalSummaryView.as_view(), name='goal-summary'),
    path('transactions/<uuid:transaction_id>/receipts/', TransactionReceiptsView.as_view(), name='transaction-receipts'),
    path(
        'transactions/<uuid:transaction_id>/receipts/<uuid:receipt_id>/',
        TransactionReceiptView.as_view(), name='transaction-receipt',
    ),
    path(
        'transactions/<uuid:transaction_id>/receipts/<uuid:receipt_id>/link/',
        TransactionReceiptLinkView.as_view(), name='transaction-receipt-link',
    ),
]
