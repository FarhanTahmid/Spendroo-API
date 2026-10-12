from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import gates
from billing.catalog.keys import F
from workspaces.models import Membership
from workspaces.tenancy import TenantScopedMixin
from . import pagination, queries
from .models import Account
from .serializers import AccountSummarySerializer, AccountTransactionPageSerializer, AccountTransactionSerializer

INCLUDES = ('categories', 'series')

FILTER_PARAMETERS = [
    OpenApiParameter('from', OpenApiTypes.DATE, description='First day, inclusive.'),
    OpenApiParameter('to', OpenApiTypes.DATE, description='Last day, inclusive.'),
    OpenApiParameter('kind', OpenApiTypes.STR, enum=list(queries.KINDS)),
    OpenApiParameter(
        'category', OpenApiTypes.STR, many=True,
        description="Category ids; 'none' for income and expenses with no category.",
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
        filters = queries.parse_filters(request.query_params)
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
        filters = queries.parse_filters(request.query_params)
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
            data['series'] = queries.series(account, filters)
        return Response(AccountSummarySerializer(data).data)
