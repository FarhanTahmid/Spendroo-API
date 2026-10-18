import re

from rest_framework import serializers

from spendroo.money import validate_currency
from .models import Invitation, Workspace, Membership


class WorkspaceSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = [
            'id', 'name', 'kind', 'owner', 'default_currency', 'is_demo', 'role',
            'version', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'kind', 'owner', 'is_demo', 'version', 'created_at', 'updated_at']

    def get_role(self, obj) -> str:
        return obj.role_of(self.context['request'].user)

    def validate_default_currency(self, value):
        return validate_currency(value)


class MembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    #: The member's own name, empty when they have not given one
    user_name = serializers.CharField(source='user.full_name', read_only=True)

    class Meta:
        model = Membership
        fields = ['id', 'user', 'user_email', 'user_name', 'role', 'created_at']
        read_only_fields = fields


class MembershipRoleSerializer(serializers.Serializer):
    """A member's new role. Ownership is handed over, not given as a role."""
    role = serializers.ChoiceField(choices=Invitation.ROLE_CHOICES)


class OwnershipTransferSerializer(serializers.Serializer):
    """The admin who takes over the business."""
    membership_id = serializers.UUIDField()


class InvitationSerializer(serializers.ModelSerializer):
    invited_by_email = serializers.EmailField(source='invited_by.email', read_only=True, default=None)
    status = serializers.CharField(read_only=True)

    class Meta:
        model = Invitation
        fields = ['id', 'email', 'phone', 'role', 'status', 'invited_by', 'invited_by_email', 'expires_at', 'created_at']
        read_only_fields = fields


class InvitationCreateSerializer(serializers.Serializer):
    """Who to invite, by email or by phone, and as what."""
    email = serializers.EmailField(required=False, allow_blank=True, default='')
    phone = serializers.CharField(required=False, allow_blank=True, default='', max_length=30)
    role = serializers.ChoiceField(choices=Invitation.ROLE_CHOICES, default=Membership.ROLE_STAFF)

    def validate_email(self, value):
        return value.strip().lower()

    def validate_phone(self, value):
        phone = re.sub(r'[\s\-().]', '', value)
        if phone and not re.fullmatch(r'\+?\d{7,15}', phone):
            raise serializers.ValidationError('Enter a phone number, such as +8801712345678.')
        return phone

    def validate(self, attrs):
        if bool(attrs['email']) == bool(attrs['phone']):
            raise serializers.ValidationError('Give an email address or a phone number, not both.')
        return attrs


class InvitationCodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=32)


class InvitationPreviewSerializer(serializers.Serializer):
    """What someone holding a code is about to join."""
    workspace_id = serializers.UUIDField(source='workspace.id')
    workspace_name = serializers.CharField(source='workspace.name')
    invited_by = serializers.SerializerMethodField()
    role = serializers.CharField()
    email = serializers.EmailField()
    expires_at = serializers.DateTimeField()

    def get_invited_by(self, obj) -> str:
        return obj.invited_by.full_name or obj.invited_by.email if obj.invited_by else ''


class SeatsSerializer(serializers.Serializer):
    """A business's team seats on its owner's plan. `limit` is null when unlimited."""
    limit = serializers.IntegerField(allow_null=True)
    unlimited = serializers.BooleanField()
    used = serializers.IntegerField()
    members = serializers.IntegerField()
    pending = serializers.IntegerField()
    enforced = serializers.BooleanField()
    upgrade_to = serializers.CharField(allow_null=True)
