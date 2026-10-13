from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from billing import gates
from identity.models import UserSettings

from . import delivery, push
from .models import Device, NotificationPreference
from .serializers import (
    DeviceSerializer,
    ForgetDeviceSerializer,
    NotificationPreferenceSerializer,
    PushTestSerializer,
)

# Being reminded is for every plan; what there is to be reminded of is limited where it is made
OWN_NOTIFICATIONS = gates.exempt('own devices and notification preferences')


class DeviceView(APIView):
    """
    The phone says where push notifications for the signed-in user go. Called
    after sign-in, and again whenever Firebase gives the phone a new token or
    the app's language changes.
    """
    permission_classes = [permissions.IsAuthenticated]
    billing_gate = OWN_NOTIFICATIONS

    @extend_schema(request=DeviceSerializer, responses={204: None})
    def put(self, request):
        serializer = DeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        # A phone has one row: whoever signs in on it last is the one it rings for
        Device.objects.update_or_create(
            fcm_token=data['token'],
            defaults={
                'user': request.user, 'platform': data['platform'], 'locale': data['locale'],
                'timezone': data['timezone'], 'app_version': data['app_version'], 'last_seen_at': timezone.now(),
            },
        )
        if data['timezone']:
            # "Today", and the hour of a reminder, are read where the phone is
            UserSettings.objects.update_or_create(user=request.user, defaults={'timezone': data['timezone']})
        return Response(status=status.HTTP_204_NO_CONTENT)


class ForgetDeviceView(APIView):
    """Before signing out: this phone is no longer to ring for the user."""
    permission_classes = [permissions.IsAuthenticated]
    billing_gate = OWN_NOTIFICATIONS

    @extend_schema(request=ForgetDeviceSerializer, responses={204: None})
    def post(self, request):
        serializer = ForgetDeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        Device.objects.filter(user=request.user, fcm_token=serializer.validated_data['token']).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationSettingsView(generics.RetrieveUpdateAPIView):
    """What the signed-in user wants to be told by push, and at what hours."""
    serializer_class = NotificationPreferenceSerializer
    permission_classes = [permissions.IsAuthenticated]
    billing_gate = OWN_NOTIFICATIONS
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_object(self):
        # Unsaved until the user changes something: saving the serializer then makes the row
        return NotificationPreference.of(self.request.user.pk)


class PushTestView(APIView):
    """Sends one notification to the user's own phones, to see that it arrives."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'push_test'
    billing_gate = OWN_NOTIFICATIONS

    @extend_schema(request=None, responses=PushTestSerializer)
    def post(self, request):
        devices = Device.objects.filter(user=request.user).count()
        try:
            sent = delivery.send_test(request.user.pk)
        except push.PushError:
            sent = 0
        return Response({'configured': push.configured(), 'devices': devices, 'sent': sent})
