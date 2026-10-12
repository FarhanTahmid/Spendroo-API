"""
URL configuration for spendroo project.
"""
from django.contrib import admin
from django.urls import path,include
from django.conf.urls.static import static
from django.conf import settings
from django.db import DatabaseError, connection
from django.http import JsonResponse
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


def health(request):
    """Liveness; with ?db=1 also checks the database answers."""
    if request.GET.get('db'):
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
        except DatabaseError:
            return JsonResponse({'status': 'error', 'database': 'unavailable'}, status=503)
        return JsonResponse({'status': 'ok', 'database': 'ok'})
    return JsonResponse({'status': 'ok'})


api_v1_patterns = [
    path('',include('identity.urls')),
    path('workspaces/',include('workspaces.urls')),
    path('',include('cashbook.urls')),
    path('ledger/',include('ledger_personal.urls')),
    path('sync/',include('sync.urls')),
    path('billing/',include('billing.api.urls')),
    path('reports/',include('reports.urls')),
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='docs'),
]

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path('health/', health, name='health'),
    path('api/v1/', include(api_v1_patterns)),
    # Authentication (django-allauth). The app talks to /_allauth/app/v1/;
    # accounts/ only serves the provider callbacks allauth needs internally.
    path('_allauth/', include('allauth.headless.urls')),
    path('accounts/', include('allauth.urls')),
]
if settings.DEBUG:
    urlpatterns+=static(settings.MEDIA_URL,document_root=settings.MEDIA_ROOT)
