from rest_framework import serializers

from identity.models import validate_timezone

from .models import NotificationPreference


class DeviceSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=512)
    platform = serializers.ChoiceField(choices=['android', 'ios'], default='android')
    locale = serializers.CharField(max_length=10, required=False, default='en')
    # The phone's time zone, an IANA name: also becomes the user's own
    timezone = serializers.CharField(max_length=64, required=False, allow_blank=True, default='',
                                     validators=[validate_timezone])
    app_version = serializers.CharField(max_length=40, required=False, allow_blank=True, default='')


class ForgetDeviceSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=512)


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = (
            'push_enabled', 'bills', 'income', 'recorded', 'budgets', 'daily_nudge', 'nudge_time', 'reminder_time',
            'quiet_from', 'quiet_to',
        )

    def validate(self, attrs):
        current = self.instance
        quiet = [attrs.get(name, getattr(current, name, None)) for name in ('quiet_from', 'quiet_to')]
        if (quiet[0] is None) != (quiet[1] is None):
            raise serializers.ValidationError({'quiet_to': 'Quiet hours need both a start and an end.'})
        return attrs


class PushTestSerializer(serializers.Serializer):
    configured = serializers.BooleanField()
    devices = serializers.IntegerField()
    sent = serializers.IntegerField()
