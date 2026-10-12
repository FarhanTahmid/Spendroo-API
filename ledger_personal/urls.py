from django.urls import path

from .views import AccountSummaryView, AccountTransactionsView

app_name = 'ledger'

urlpatterns = [
    path('accounts/<uuid:account_id>/transactions/', AccountTransactionsView.as_view(), name='account-transactions'),
    path('accounts/<uuid:account_id>/summary/', AccountSummaryView.as_view(), name='account-summary'),
]
