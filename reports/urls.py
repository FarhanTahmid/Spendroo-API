from django.urls import path

from .views import AccountReportView, BusinessReportView, CashbookReportView, PersonalReportView

app_name = 'reports'

urlpatterns = [
    path('personal/', PersonalReportView.as_view(), name='personal'),
    path('accounts/<uuid:account_id>/', AccountReportView.as_view(), name='account'),
    path('cashbooks/<uuid:cashbook_id>/', CashbookReportView.as_view(), name='cashbook'),
    path('businesses/<uuid:workspace_id>/', BusinessReportView.as_view(), name='business'),
]
