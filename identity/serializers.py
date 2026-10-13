import hashlib

from django.urls import reverse
from rest_framework import serializers

from billing import payload as billing_payload
from .models import DEFAULT_TIMEZONE, SATURDAY, AppUser, UserSettings, validate_timezone


class UserProfileSerializer(serializers.ModelSerializer):
    full_name = serializers.ReadOnlyField()
    has_password = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    # Kept on UserSettings, shown here so the app needs one call after login
    onboarded_at = serializers.DateTimeField(required=False, allow_null=True)
    primary_mode = serializers.ChoiceField(choices=UserSettings.MODE_CHOICES, required=False, allow_blank=True)
    # The phone's time zone (an IANA name) and the day the user's week starts on, Monday as 0
    timezone = serializers.CharField(required=False, max_length=64, validators=[validate_timezone])
    week_start = serializers.IntegerField(required=False, min_value=0, max_value=6)
    # The plan and what it gives, so the app knows after login without a second call.
    # Read-only: nothing a client sends can change it.
    entitlements = serializers.SerializerMethodField()

    SETTINGS_FIELDS = ('onboarded_at', 'primary_mode', 'timezone', 'week_start')

    class Meta:
        model = AppUser
        fields = ('id', 'email', 'username', 'first_name', 'last_name',
                 'full_name', 'bio', 'phone', 'avatar_url', 'has_password', 'date_joined',
                 'onboarded_at', 'primary_mode', 'timezone', 'week_start', 'entitlements')
        # The username changes through /me/username/ (password-checked), the picture through /me/avatar/
        read_only_fields = ('id', 'email', 'username', 'date_joined')

    def get_entitlements(self, user) -> dict:
        return billing_payload.build(user)

    def get_has_password(self, user) -> bool:
        # Accounts made with Google have none until the user sets one
        return user.has_usable_password()

    def get_avatar_url(self, user) -> str | None:
        if not user.profile_picture:
            return None
        # Changes with every new picture, so the app knows to fetch it again
        version = hashlib.sha256(user.profile_picture.name.encode()).hexdigest()[:12]
        return f"{reverse('identity:avatar')}?v={version}"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        user_settings = UserSettings.objects.filter(user=instance).first()
        data['onboarded_at'] = (
            serializers.DateTimeField().to_representation(user_settings.onboarded_at)
            if user_settings and user_settings.onboarded_at else None
        )
        data['primary_mode'] = user_settings.primary_mode if user_settings else ''
        data['timezone'] = user_settings.timezone if user_settings else DEFAULT_TIMEZONE
        data['week_start'] = user_settings.week_start if user_settings else SATURDAY
        return data

    def update(self, instance, validated_data):
        changes = {field: validated_data.pop(field) for field in self.SETTINGS_FIELDS if field in validated_data}
        if changes:
            UserSettings.objects.update_or_create(user=instance, defaults=changes)
        return super().update(instance, validated_data)


class UsernameCheckSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    current = serializers.BooleanField()
    reason = serializers.ChoiceField(choices=('invalid', 'taken'), allow_null=True)
    message = serializers.CharField(allow_null=True)
    suggestions = serializers.ListField(child=serializers.CharField())


class ChangeUsernameSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, trim_whitespace=True)
    password = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False, write_only=True)

    def validate(self, attrs):
        user = self.context['request'].user
        # Google-only accounts have no password to confirm with
        if user.has_usable_password():
            password = attrs.get('password')
            if not password:
                raise serializers.ValidationError({'password': ['Enter your password to confirm.']})
            if not user.check_password(password):
                raise serializers.ValidationError({'password': ['Incorrect password.']})
        return attrs


class AvatarUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
