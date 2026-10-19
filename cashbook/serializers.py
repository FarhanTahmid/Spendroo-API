from rest_framework import serializers
from django.db import transaction
from workspaces.models import Membership, Workspace
from workspaces.services import get_personal_workspace, workspaces_for
from spendroo.money import validate_currency
from .models import (
    CashBook, CashBookAdditionalMember, EntryCategory,
    PaymentMethod, Entry, EntryBills, EntryExtraFields
)


class CashBookAdditionalMemberSerializer(serializers.ModelSerializer):
    member_email = serializers.EmailField(source='member.email', read_only=True)
    member_username = serializers.CharField(source='member.username', read_only=True)
    added_by_email = serializers.EmailField(source='added_by.email', read_only=True)

    class Meta:
        model = CashBookAdditionalMember
        fields = ['id', 'member', 'member_email', 'member_username', 'role', 'created_at', 'added_by', 'added_by_email']
        read_only_fields = ['id', 'created_at', 'added_by']


class CashBookGrantSerializer(serializers.Serializer):
    """The staff member to let into a book, and how far."""
    member = serializers.UUIDField()
    role = serializers.ChoiceField(choices=CashBookAdditionalMember.ROLE_CHOICES)


class CashBookGrantRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=CashBookAdditionalMember.ROLE_CHOICES)


class CashBookAccessSerializer(serializers.Serializer):
    """A staff member of the book's business, and their grant to it if they have one."""
    membership_id = serializers.UUIDField(source='membership.id')
    user = serializers.UUIDField(source='membership.user_id')
    user_email = serializers.EmailField(source='membership.user.email')
    grant_id = serializers.UUIDField(source='grant.id', default=None)
    role = serializers.CharField(source='grant.role', default=None)


class EntryCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = EntryCategory
        fields = ['id', 'category_name', 'color', 'is_default', 'created_at']
        read_only_fields = ['id', 'is_default', 'created_at']


class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = ['id', 'payment_method_name', 'is_default', 'created_at']
        read_only_fields = ['id', 'is_default', 'created_at']


class EntryBillsSerializer(serializers.ModelSerializer):
    """What the app shows of a bill. Never the file's path: the file is fetched through a signed link."""
    class Meta:
        model = EntryBills
        fields = [
            'id', 'workspace_id', 'entry_id', 'cashbook_id', 'created_by_id', 'content_type', 'original_name',
            'size_bytes', 'page_count', 'width', 'height', 'version', 'server_seq', 'created_at', 'updated_at',
            'deleted_at',
        ]
        read_only_fields = fields


class EntryExtraFieldsSerializer(serializers.ModelSerializer):
    class Meta:
        model = EntryExtraFields
        fields = ['id', 'field_name', 'field_value']
        read_only_fields = ['id']


class EntryListSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.category_name', read_only=True)
    payment_method_name = serializers.CharField(source='payment_method.payment_method_name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    bills_count = serializers.SerializerMethodField()

    class Meta:
        model = Entry
        fields = [
            'id', 'entry_type', 'amount_minor', 'currency', 'source', 'title', 'entry_date',
            'category', 'category_name', 'payment_method', 'payment_method_name',
            'created_by', 'created_by_email', 'bills_count', 'version', 'created_at', 'updated_at'
        ]
        read_only_fields = fields

    def get_bills_count(self, obj) -> int:
        return len(obj.bills.all())


class EntryDetailSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.category_name', read_only=True)
    payment_method_name = serializers.CharField(source='payment_method.payment_method_name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    bills = EntryBillsSerializer(many=True, read_only=True)
    extra_fields = EntryExtraFieldsSerializer(many=True, read_only=True)

    class Meta:
        model = Entry
        fields = [
            'id', 'entry_type', 'amount_minor', 'currency', 'source', 'title', 'remarks', 'entry_date',
            'category', 'category_name', 'payment_method', 'payment_method_name',
            'created_by', 'created_by_email', 'bills', 'extra_fields',
            'version', 'created_at', 'updated_at'
        ]
        read_only_fields = fields


class EntryCreateUpdateSerializer(serializers.ModelSerializer):
    extra_fields_data = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False
    )

    class Meta:
        model = Entry
        fields = [
            'id', 'entry_type', 'amount_minor', 'source', 'title', 'remarks', 'entry_date',
            'category', 'payment_method', 'extra_fields_data'
        ]
        read_only_fields = ['id']

    def validate_amount_minor(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than 0")
        return value

    def validate(self, attrs):
        # Category and payment method must belong to the entry's own cashbook
        cashbook = self.instance.cashbook if self.instance else self.context['cashbook']
        for field in ('category', 'payment_method'):
            value = attrs.get(field)
            if value is not None and value.cashbook_id != cashbook.id:
                raise serializers.ValidationError({field: 'Does not belong to this cashbook.'})
        return attrs

    def create(self, validated_data):
        extra_fields_data = validated_data.pop('extra_fields_data', [])

        with transaction.atomic():
            entry = Entry.objects.create(**validated_data)

            # Create extra fields
            for field_data in extra_fields_data:
                EntryExtraFields.objects.create(
                    entry=entry,
                    field_name=field_data.get('field_name'),
                    field_value=field_data.get('field_value')
                )

        return entry

    def update(self, instance, validated_data):
        extra_fields_data = validated_data.pop('extra_fields_data', None)

        with transaction.atomic():
            # Update entry
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            # Update extra fields if provided
            if extra_fields_data is not None:
                for extra_field in instance.extra_fields.all():
                    extra_field.soft_delete()
                for field_data in extra_fields_data:
                    EntryExtraFields.objects.create(
                        entry=instance,
                        field_name=field_data.get('field_name'),
                        field_value=field_data.get('field_value')
                    )

        return instance


class CashBookListSerializer(serializers.ModelSerializer):
    owner = serializers.UUIDField(source='workspace.owner_id', read_only=True)
    owner_email = serializers.EmailField(source='workspace.owner.email', read_only=True)
    balance_minor = serializers.SerializerMethodField()
    members_count = serializers.SerializerMethodField()
    entries_count = serializers.SerializerMethodField()

    class Meta:
        model = CashBook
        fields = [
            'id', 'workspace', 'book_name', 'description', 'currency', 'owner', 'owner_email',
            'balance_minor', 'members_count', 'entries_count',
            'version', 'created_at', 'updated_at'
        ]
        read_only_fields = fields

    def get_balance_minor(self, obj) -> int:
        return obj.get_balance()

    def get_members_count(self, obj) -> int:
        return obj.cashbookadditionalmember_set.count()

    def get_entries_count(self, obj) -> int:
        return obj.entry_set.count()


class CashBookDetailSerializer(serializers.ModelSerializer):
    owner = serializers.UUIDField(source='workspace.owner_id', read_only=True)
    owner_email = serializers.EmailField(source='workspace.owner.email', read_only=True)
    balance_minor = serializers.SerializerMethodField()
    members = CashBookAdditionalMemberSerializer(source='cashbookadditionalmember_set', many=True, read_only=True)
    categories = EntryCategorySerializer(source='entrycategory_set', many=True, read_only=True)
    payment_methods = PaymentMethodSerializer(source='paymentmethod_set', many=True, read_only=True)

    class Meta:
        model = CashBook
        fields = [
            'id', 'workspace', 'book_name', 'description', 'currency', 'owner', 'owner_email',
            'balance_minor', 'members', 'categories', 'payment_methods',
            'version', 'created_at', 'updated_at'
        ]
        read_only_fields = fields

    def get_balance_minor(self, obj) -> int:
        return obj.get_balance()


class CashBookCreateUpdateSerializer(serializers.ModelSerializer):
    # Optional on create: defaults to the user's personal workspace
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.none(), required=False)
    balance_minor = serializers.SerializerMethodField()

    class Meta:
        model = CashBook
        fields = [
            'id', 'workspace', 'book_name', 'description', 'currency',
            'version', 'created_at', 'updated_at', 'balance_minor'
        ]
        read_only_fields = ['id', 'version', 'created_at', 'updated_at']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        if request is not None and request.user.is_authenticated:
            self.fields['workspace'].queryset = workspaces_for(request.user)

    def validate_currency(self, value):
        return validate_currency(value)

    def validate(self, attrs):
        user = self.context['request'].user
        if self.instance is not None:
            # A book cannot move between workspaces or change currency once it exists
            attrs.pop('workspace', None)
            if 'currency' in attrs and attrs['currency'] != self.instance.currency:
                raise serializers.ValidationError({'currency': 'Currency cannot be changed after creation.'})
            workspace = self.instance.workspace
        else:
            workspace = attrs.get('workspace') or get_personal_workspace(user)
            attrs['workspace'] = workspace
            attrs.setdefault('currency', workspace.default_currency)
            if workspace.role_of(user) not in Membership.MANAGER_ROLES:
                raise serializers.ValidationError(
                    {'workspace': 'Only the owner or an admin can create cashbooks in this workspace.'}
                )

        # Book names are unique within a workspace
        book_name = attrs.get('book_name')
        if book_name is not None:
            duplicates = CashBook.objects.filter(workspace=workspace, book_name__iexact=book_name)
            if self.instance is not None:
                duplicates = duplicates.exclude(id=self.instance.id)
            if duplicates.exists():
                raise serializers.ValidationError({'book_name': ['You already have a cashbook with this name.']})
        return attrs

    def create(self, validated_data):
        with transaction.atomic():
            cashbook = CashBook.objects.create(**validated_data)

            # Create default categories
            for category_name in EntryCategory.GENERAL_CATEGORIES:
                EntryCategory.objects.create(
                    cashbook=cashbook,
                    category_name=category_name,
                    is_default=True
                )

            # Create default payment methods
            for method_name in PaymentMethod.GENERAL_PAYMENT_METHODS:
                PaymentMethod.objects.create(
                    cashbook=cashbook,
                    payment_method_name=method_name,
                    is_default=True
                )

        return cashbook

    def get_balance_minor(self, obj) -> int:
        return obj.get_balance()
