from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import gates
from billing.catalog.keys import F
from workspaces.models import Membership, Workspace
from workspaces.tenancy import TenantScopedMixin
from reminders.inbox import today_in
from reports.periods import is_preset
from . import goals, pagination, queries, trends, year_review
from .models import Account, SavingsGoal
from .serializers import (
    AccountSummarySerializer, AccountTransactionPageSerializer, AccountTransactionSerializer, GoalSummarySerializer,
    TrendsSerializer, YearReviewSerializer,
)

INCLUDES = ('categories', 'series')

FILTER_PARAMETERS = [
    OpenApiParameter(
        'from', OpenApiTypes.DATE,
        description='First day, inclusive. Dates other than a preset (a whole month, the last three months, '
                    'this year) are a plan feature: 402 `insights.custom_range` without it.',
    ),
    OpenApiParameter('to', OpenApiTypes.DATE, description='Last day, inclusive.'),
    OpenApiParameter('kind', OpenApiTypes.STR, enum=list(queries.KINDS)),
    OpenApiParameter(
        'category', OpenApiTypes.STR, many=True,
        description="Category ids; 'none' for income and expenses with no category.",
    ),
    OpenApiParameter(
        'receipt', OpenApiTypes.STR, enum=list(queries.RECEIPT_CHOICES),
        description='1: only transactions with a receipt; 0: only those without.',
    ),
    OpenApiParameter(
        'q', OpenApiTypes.STR,
        description="Matches the note, the category, the other account of a transfer, or the amount's whole units.",
    ),
]


class AccountView(TenantScopedMixin, APIView):
    """
    Reads of one personal account. Nothing here writes: the app changes the
    ledger through sync, and an account locked by a downgrade stays readable.
    """
    permission_classes = [IsAuthenticated]

    def get_account(self, request, account_id):
        account = Account.objects.select_related('workspace').filter(id=account_id).first()
        # The personal ledger is for those who run the workspace, as it is in sync
        if account is None or account.workspace.role_of(request.user) not in Membership.MANAGER_ROLES:
            raise NotFound('Account not found.')
        return account

    def get_filters(self, request, account):
        """The filter asked for. Chosen dates need the plan; the presets do not."""
        filters = queries.parse_filters(request.query_params)
        if not is_preset(filters.date_from, filters.date_to, today_in(account.workspace)):
            gates.require(account.workspace, F.INSIGHTS_CUSTOM_RANGE)
        return filters


class AccountTransactionsView(AccountView):
    """
    GET ?from&to&kind&category&q&cursor&limit

    The account's transactions, newest first. Call again with `next_cursor`
    while `has_more` is true.
    """

    @extend_schema(
        parameters=[
            *FILTER_PARAMETERS,
            OpenApiParameter('cursor', OpenApiTypes.STR, description='`next_cursor` of the page before.'),
            OpenApiParameter(
                'limit', OpenApiTypes.INT,
                description=f'{pagination.DEFAULT_LIMIT} by default, at most {pagination.MAX_LIMIT}.',
            ),
        ],
        responses=AccountTransactionPageSerializer,
    )
    def get(self, request, account_id):
        account = self.get_account(request, account_id)
        filters = self.get_filters(request, account)
        rows, next_cursor = pagination.page(
            queries.filtered(account, filters),
            request.query_params.get('cursor'),
            pagination.parse_limit(request.query_params),
        )
        return Response({
            'results': AccountTransactionSerializer(rows, many=True).data,
            'next_cursor': next_cursor,
            'has_more': next_cursor is not None,
        })


class AccountSummaryView(AccountView):
    """
    GET ?from&to&kind&category&q&include=categories,series

    What the same filter adds up to: cash in, cash out and their make-up, with
    the balance before and after the period. `balance_minor` is today's.
    """

    @extend_schema(
        parameters=[
            *FILTER_PARAMETERS,
            OpenApiParameter(
                'include', OpenApiTypes.STR,
                description="Comma-separated extras: 'categories' (totals per category), 'series' (the charts).",
            ),
        ],
        responses=AccountSummarySerializer,
    )
    def get(self, request, account_id):
        account = self.get_account(request, account_id)
        filters = self.get_filters(request, account)
        include = {part.strip() for part in request.query_params.get('include', '').split(',') if part.strip()}
        if include - set(INCLUDES):
            raise ParseError(f"include takes: {', '.join(INCLUDES)}.")

        data = {
            'currency': account.currency,
            'balance_minor': queries.balance_of(account),
            'opening_balance_minor': account.opening_balance_minor,
            'is_locked': (
                not account.is_archived
                and account.id in gates.keep_state(account.workspace, F.PERSONAL_ACCOUNTS).locked
            ),
            **queries.summarize(account, filters),
        }
        if 'categories' in include:
            data['expense_categories'] = queries.by_category(account, filters, 'expense')
            data['income_categories'] = queries.by_category(account, filters, 'income')
        if 'series' in include:
            # All time is free, so a long series is shortened to the plan rather than refused
            data['series'] = queries.series(
                account, filters, gates.limit_in_force(account.workspace, F.INSIGHTS_TREND_MONTHS),
            )
        return Response(AccountSummarySerializer(data).data)


class WorkspaceTrendsView(TenantScopedMixin, APIView):
    """
    GET ?months&to&currency

    Income and expense for each of the `months` months ending with `to`, as the
    app's cash-flow chart draws them. More months than the plan's
    insights.trend_months is a 402.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                'months', OpenApiTypes.INT,
                description=f'{trends.DEFAULT_MONTHS} by default, at most {trends.MAX_MONTHS}. More than the plan '
                            'allows: 402 `insights.trend_months`, reason `limit`.',
            ),
            OpenApiParameter('to', OpenApiTypes.STR, description='The last month, YYYY-MM. This month by default.'),
            OpenApiParameter(
                'currency', OpenApiTypes.STR, description="Three-letter code. The workspace's own by default.",
            ),
        ],
        responses=TrendsSerializer,
    )
    def get(self, request, workspace_id):
        workspace = Workspace.objects.filter(id=workspace_id).first()
        # Who may look comes before what the plan allows, so a stranger learns nothing of it
        if workspace is None or workspace.role_of(request.user) not in Membership.MANAGER_ROLES:
            raise NotFound('Workspace not found.')
        asked = trends.parse_trend_params(request.query_params, today_in(workspace), workspace.default_currency)
        gates.check_within(workspace, F.INSIGHTS_TREND_MONTHS, asked.months)
        first, last = asked.window
        return Response(TrendsSerializer({
            'currency': asked.currency,
            'months': asked.months,
            'date_from': first,
            'date_to': last,
            'points': trends.monthly_totals(workspace, asked.currency, first, last),
        }).data)


class WorkspaceYearReviewView(TenantScopedMixin, APIView):
    """
    GET ?year&currency

    A year looked back on: totals, month by month, the top categories and the
    year before beside it. A plan feature: 402 `insights.year_review` without it.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                'year', OpenApiTypes.INT,
                description=f'From {year_review.FIRST_YEAR} to this year. This year by default; last year in January.',
            ),
            OpenApiParameter(
                'currency', OpenApiTypes.STR, description="Three-letter code. The workspace's own by default.",
            ),
        ],
        responses=YearReviewSerializer,
    )
    def get(self, request, workspace_id):
        workspace = Workspace.objects.filter(id=workspace_id).first()
        # Who may look comes before what the plan allows, so a stranger learns nothing of it
        if workspace is None or workspace.role_of(request.user) not in Membership.MANAGER_ROLES:
            raise NotFound('Workspace not found.')
        gates.require(workspace, F.INSIGHTS_YEAR_REVIEW)
        today = today_in(workspace)
        asked = year_review.parse_year_params(request.query_params, today, workspace.default_currency)
        return Response(YearReviewSerializer(year_review.review(workspace, asked.currency, asked.year, today)).data)


class GoalSummaryView(TenantScopedMixin, APIView):
    """
    GET: how far a savings goal has come and whether it keeps pace with its
    date. Its transactions are those of its account (accounts/<id>/transactions/).
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=GoalSummarySerializer)
    def get(self, request, goal_id):
        goal = SavingsGoal.objects.select_related('account', 'workspace').filter(id=goal_id).first()
        if goal is None or goal.workspace.role_of(request.user) not in Membership.MANAGER_ROLES:
            raise NotFound('Goal not found.')
        progress = goals.progress(goal, today_in(goal.workspace))
        return Response(GoalSummarySerializer({
            'id': goal.id, 'name': goal.name, 'currency': goal.currency, 'status': goal.status,
            'target_on': goal.target_on, **progress.as_dict(),
            'is_locked': (
                goal.status != SavingsGoal.ARCHIVED
                and goal.id in gates.keep_state(goal.workspace, F.SAVINGS_GOALS).locked
            ),
        }).data)
