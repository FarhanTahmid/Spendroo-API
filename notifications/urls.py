from django.urls import path

from . import views

app_name = 'notifications'

urlpatterns = [
    path('me/devices/', views.DeviceView.as_view(), name='device'),
    path('me/devices/forget/', views.ForgetDeviceView.as_view(), name='forget-device'),
    path('me/notification-settings/', views.NotificationSettingsView.as_view(), name='settings'),
    path('me/notification-settings/test/', views.PushTestView.as_view(), name='test'),
]
