from rest_framework import serializers

from .models import Transaction


class AccountTransactionSerializer(serializers.ModelSerializer):
    """A transaction as sync sends it, with the names a row shows beside it."""
    workspace_id = serializers.UUIDField()
    account_id = serializers.UUIDField()
    category_id = serializers.UUIDField(allow_null=True)
    category_name = serializers.CharField(allow_null=True)
    category_color = serializers.CharField(allow_null=True)
    # For a transfer: the account the money went to or came from
    other_account_id = serializers.UUIDField(allow_null=True)
    other_account_name = serializers.CharField(allow_null=True)

    class Meta:
        model = Transaction
        fields = [
            'id', 'workspace_id', 'account_id', 'category_id', 'kind', 'amount_minor', 'currency',
            'transfer_group_id', 'occurred_on', 'note', 'source', 'version', 'created_at', 'updated_at',
            'category_name', 'category_color', 'other_account_id', 'other_account_name',
        ]
        read_only_fields = fields


class AccountTransactionPageSerializer(serializers.Serializer):
    results = AccountTransactionSerializer(many=True)
    next_cursor = serializers.CharField(allow_null=True, help_text='Pass as ?cursor= for the next page.')
    has_more = serializers.BooleanField()


class CategoryTotalSerializer(serializers.Serializer):
    category_id = serializers.UUIDField(allow_null=True, help_text='Null: uncategorised.')
    name = serializers.CharField(allow_null=True)
    color = serializers.CharField(allow_null=True)
    amount_minor = serializers.IntegerField()
    count = serializers.IntegerField()
    largest_minor = serializers.IntegerField()


class SeriesPointSerializer(serializers.Serializer):
    date = serializers.DateField(help_text='The day, or the first day of the month.')
    in_minor = serializers.IntegerField()
    out_minor = serializers.IntegerField()
    balance_minor = serializers.IntegerField(help_text='The balance at the end of the step.')


class SeriesSerializer(serializers.Serializer):
    monthly = serializers.BooleanField()
    opening_minor = serializers.IntegerField(help_text='The balance before the first point.')
    points = SeriesPointSerializer(many=True)


class AccountSummarySerializer(serializers.Serializer):
    """Amounts are positive, in minor units of `currency`."""
    currency = serializers.CharField()
    balance_minor = serializers.IntegerField(help_text="Today's balance, whatever the filter.")
    opening_balance_minor = serializers.IntegerField()
    is_locked = serializers.BooleanField(help_text='Read-only since a downgrade.')
    income_minor = serializers.IntegerField()
    expense_minor = serializers.IntegerField()
    transfer_in_minor = serializers.IntegerField()
    transfer_out_minor = serializers.IntegerField()
    cash_in_minor = serializers.IntegerField(help_text='Income plus transfers in.')
    cash_out_minor = serializers.IntegerField(help_text='Expenses plus transfers out.')
    net_minor = serializers.IntegerField()
    largest_expense_minor = serializers.IntegerField()
    count = serializers.IntegerField()
    first_day = serializers.DateField(allow_null=True)
    last_day = serializers.DateField(allow_null=True)
    period_opening_minor = serializers.IntegerField(help_text='The balance before `from`; ignores kind, category and q.')
    period_closing_minor = serializers.IntegerField(help_text='The balance after `to`; ignores kind, category and q.')
    expense_categories = CategoryTotalSerializer(many=True, required=False)
    income_categories = CategoryTotalSerializer(many=True, required=False)
    series = SeriesSerializer(required=False)
