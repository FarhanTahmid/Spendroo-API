from django.urls import path, include
from rest_framework_nested import routers

from .views.cashbook_home_views import CashBookHomeViewSet
from .views.entry_viewset import EntryViewSet
from .views.entry_category_viewset import EntryCategoryViewSet
from .views.payment_method_viewset import PaymentMethodViewSet
from .views.cashbook_stats_viewset import CashBookStatsViewSet
from attachments.views import AttachmentFileView

# Main router for cashbook-level resources
router = routers.DefaultRouter()
router.register(r'cashbooks', CashBookHomeViewSet, basename='cashbook')

# Register nested routers under the MAIN router (not a separate cashbook_router)
# URL pattern: /cashbooks/{cashbook_pk}/entries/

entries_router = routers.NestedDefaultRouter(
    router,  # ✅ Use 'router' instead of 'cashbook_router'
    r'cashbooks', 
    lookup='cashbook'
)
entries_router.register(
    r'entries', 
    EntryViewSet, 
    basename='cashbook-entries'
)

# Categories router under cashbook
categories_router = routers.NestedDefaultRouter(
    router,  # ✅ Use 'router' instead of 'cashbook_router'
    r'cashbooks',
    lookup='cashbook'
)
categories_router.register(
    r'categories',
    EntryCategoryViewSet,
    basename='cashbook-categories'
)

# Payment methods router under cashbook
payment_methods_router = routers.NestedDefaultRouter(
    router,  # ✅ Use 'router' instead of 'cashbook_router'
    r'cashbooks',
    lookup='cashbook'
)
payment_methods_router.register(
    r'payment-methods',
    PaymentMethodViewSet,
    basename='cashbook-payment-methods'
)

# Stats router under cashbook
stats_router = routers.NestedDefaultRouter(
    router,  # ✅ Use 'router' instead of 'cashbook_router'
    r'cashbooks',
    lookup='cashbook'
)
stats_router.register(
    r'stats',
    CashBookStatsViewSet,
    basename='cashbook-stats'
)

# URL patterns
urlpatterns = [
    # Include main router (for /cashbooks/)
    path('', include(router.urls)),
    
    # Include nested routers
    path('', include(entries_router.urls)),
    path('', include(categories_router.urls)),
    path('', include(payment_methods_router.urls)),
    path('', include(stats_router.urls)),

    # A bill's or receipt's file, through a signed link the app asked for (attachments/storage.py)
    path('bills/file/<str:token>/', AttachmentFileView.as_view(), name='bill-file'),
]