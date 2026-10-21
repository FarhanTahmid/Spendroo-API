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
            'transfer_group_id', 'occurred_on', 'note', 'merchant', 'tag_ids', 'source', 'splits', 'version', 'created_at', 'updated_at',
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
    hidden_months = serializers.IntegerField(
        help_text='Months of history before the first point that the plan does not chart (insights.trend_months).',
    )


class TrendPointSerializer(serializers.Serializer):
    month = serializers.DateField(help_text='The first day of the month.')
    income_minor = serializers.IntegerField()
    expense_minor = serializers.IntegerField()


class TrendsSerializer(serializers.Serializer):
    """Amounts are positive, in minor units of `currency`. Transfers are neither income nor expense."""
    currency = serializers.CharField()
    months = serializers.IntegerField()
    date_from = serializers.DateField(help_text='First day of the first month.')
    date_to = serializers.DateField(help_text='Last day of the last month.')
    points = TrendPointSerializer(many=True, help_text='One per month, oldest first, empty months included.')


class YearTotalsSerializer(serializers.Serializer):
    income_minor = serializers.IntegerField()
    expense_minor = serializers.IntegerField()
    net_minor = serializers.IntegerField()


class YearMonthSerializer(serializers.Serializer):
    month = serializers.DateField(help_text='The first day of the month.')
    amount_minor = serializers.IntegerField()


class YearCategorySerializer(CategoryTotalSerializer):
    previous_minor = serializers.IntegerField(help_text='Spent in the category the year before.')


class YearReviewSerializer(serializers.Serializer):
    """
    Amounts are positive (net may not be), in minor units of `currency`. Worked
    out as the app does (lib/features/insights/year_review.dart). Months are
    ranked and averaged only once they are over.
    """
    year = serializers.IntegerField()
    currency = serializers.CharField()
    is_partial = serializers.BooleanField(help_text='This year, still under way.')
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    income_minor = serializers.IntegerField()
    expense_minor = serializers.IntegerField()
    net_minor = serializers.IntegerField(help_text='Income less expense; negative when more went out.')
    savings_rate = serializers.IntegerField(allow_null=True, help_text='Net as a whole percent of income.')
    previous = YearTotalsSerializer(allow_null=True, help_text='The year before; null when it has nothing.')
    months = TrendPointSerializer(many=True, help_text='January to December, empty months included.')
    biggest_month = YearMonthSerializer(allow_null=True, help_text='The closed month that spent most.')
    smallest_month = YearMonthSerializer(
        allow_null=True, help_text='The closed month that spent least, of two or more that spent anything.',
    )
    best_month = YearMonthSerializer(allow_null=True, help_text='The closed month that kept most of its income.')
    months_in_green = serializers.IntegerField(help_text='Tracked months with more in than out.')
    tracked_months = serializers.IntegerField(
        help_text='Closed months from the month of the first transaction on: what the average and the months '
                  'in the green are out of.',
    )
    average_expense_minor = serializers.IntegerField(allow_null=True, help_text='Spent per tracked month.')
    largest_expense_minor = serializers.IntegerField(help_text='The biggest single expense (or part of a split).')
    top_categories = YearCategorySerializer(many=True, help_text='The five that took most, largest first.')
    goals_reached = serializers.IntegerField(help_text='Savings goals that reached their target this year.')


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


class GoalSummarySerializer(serializers.Serializer):
    """Amounts in minor units of `currency`. Worked out as the app does (lib/features/goals/goal_math.dart)."""
    id = serializers.UUIDField()
    name = serializers.CharField()
    currency = serializers.CharField()
    status = serializers.CharField()
    target_on = serializers.DateField(allow_null=True)
    saved_minor = serializers.IntegerField()
    target_minor = serializers.IntegerField()
    percent = serializers.IntegerField(help_text='Whole percent saved, floored; may pass 100.')
    remaining_minor = serializers.IntegerField()
    expected_minor = serializers.IntegerField(
        allow_null=True, help_text='Where a steady saver would be today. Empty without a date.',
    )
    needed_per_month_minor = serializers.IntegerField(
        allow_null=True, help_text='Still to put in each month to make the date.',
    )
    pace = serializers.ChoiceField(choices=['on_track', 'ahead', 'behind', 'no_date', 'reached'])
    is_locked = serializers.BooleanField(help_text='Nothing more goes in since a downgrade; taking money out works.')
