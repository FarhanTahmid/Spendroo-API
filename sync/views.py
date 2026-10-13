import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from billing import gates
from billing.entitlements import forget_counts
from billing.models import Stamp
from workspaces.models import Workspace
from reminders.tasks import budgets_changed, goals_changed
from workspaces.tenancy import TenantScopedMixin
from .models import SyncMutation
from .registry import TABLES, accessible_cashbook_ids

DEFAULT_PULL_LIMIT = 500
MAX_PULL_LIMIT = 1000
MAX_PUSH_MUTATIONS = 200
BUDGET_TABLES = ('transactions', 'budgets')
GOAL_TABLES = ('transactions', 'savings_goals')


# Shapes for the API schema only; the views validate by hand so that one bad
# mutation is reported on its own instead of failing the whole batch
_row_schema = serializers.DictField(help_text='A row as the server holds it: columns by name, ids as strings.')
_pull_response = inline_serializer('SyncPullResponse', {
    'changes': serializers.DictField(child=serializers.ListField(child=_row_schema),
                                     help_text='Changed rows (tombstones included) by table name.'),
    'next_since': serializers.IntegerField(),
    'has_more': serializers.BooleanField(),
    'accessible_cashbook_ids': serializers.ListField(child=serializers.UUIDField()),
    'billing_stamp': serializers.CharField(
        help_text='Changes when the plan or what it gives may have changed: fetch /billing/entitlements/ again.',
    ),
})
_mutation = inline_serializer('SyncMutation', {
    'id': serializers.UUIDField(help_text='Made on the device; resending it returns the first result.'),
    'table': serializers.ChoiceField(choices=list(TABLES)),
    'op': serializers.ChoiceField(choices=['upsert', 'delete']),
    'row_id': serializers.UUIDField(),
    'data': serializers.DictField(required=False, help_text='Changed columns only.'),
})
_push_request = inline_serializer('SyncPushRequest', {
    'workspace': serializers.UUIDField(),
    'mutations': serializers.ListField(child=_mutation, max_length=MAX_PUSH_MUTATIONS),
})
_push_response = inline_serializer('SyncPushResponse', {
    'results': serializers.ListField(child=inline_serializer('SyncMutationResult', {
        'id': serializers.UUIDField(),
        'status': serializers.ChoiceField(choices=['applied', 'rejected']),
        'error': inline_serializer('SyncMutationError', {
            'code': serializers.CharField(
                help_text='malformed, forbidden, deleted, immutable, invalid, conflict or plan_limit',
            ),
            'detail': serializers.CharField(),
            'meta': serializers.DictField(help_text='For plan_limit: feature, reason, limit, current, plan, upgrade_to.'),
        }, required=False),
        'row': serializers.DictField(allow_null=True),
    })),
})


class Rejected(Exception):
    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail


def get_workspace_and_role(user, workspace_id):
    """Returns (workspace, role), or (None, None) when the user is not a member."""
    try:
        workspace = Workspace.objects.filter(id=workspace_id).first()
    except (ValidationError, ValueError):
        workspace = None
    role = workspace.role_of(user) if workspace else None
    return (workspace, role) if role else (None, None)


class PullView(TenantScopedMixin, APIView):
    """
    GET ?workspace=<id>&since=<cursor>&limit=<n>

    Rows of the workspace changed after the cursor, tombstones included,
    grouped by table. Call again with `next_since` while `has_more` is true.
    """
    permission_classes = [IsAuthenticated]
    # Background sync runs often; its own budget keeps it from using up the user's
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'sync'

    @extend_schema(
        parameters=[
            OpenApiParameter('workspace', OpenApiTypes.UUID, required=True),
            OpenApiParameter('since', OpenApiTypes.INT, description='Cursor from the last pull; 0 for everything.'),
            OpenApiParameter('limit', OpenApiTypes.INT, description=f'At most {MAX_PULL_LIMIT}.'),
        ],
        responses=_pull_response,
    )
    def get(self, request):
        workspace, role = get_workspace_and_role(request.user, request.query_params.get('workspace'))
        if workspace is None:
            return Response({'detail': 'Workspace not found.'}, status=status.HTTP_404_NOT_FOUND)
        try:
            since = int(request.query_params.get('since', 0))
            limit = min(int(request.query_params.get('limit', DEFAULT_PULL_LIMIT)), MAX_PULL_LIMIT)
        except ValueError:
            return Response({'detail': 'since and limit must be integers.'}, status=status.HTTP_400_BAD_REQUEST)
        if since < 0 or limit < 1:
            return Response({'detail': 'since and limit must be positive.'}, status=status.HTTP_400_BAD_REQUEST)

        fetched = {}
        # A table that hit the limit has more rows after its last one, so nothing
        # beyond that point may be handed out from any table in this page.
        cutoff = None
        for name, table in TABLES.items():
            queryset = table.model.all_objects.filter(workspace=workspace, server_seq__gt=since)
            rows = list(table.visible(queryset, request.user, role).order_by('server_seq')[:limit + 1])
            if len(rows) > limit:
                rows = rows[:limit]
                cutoff = rows[-1].server_seq if cutoff is None else min(cutoff, rows[-1].server_seq)
            fetched[name] = rows

        changes = {}
        next_since = since
        for name, rows in fetched.items():
            if cutoff is not None:
                rows = [row for row in rows if row.server_seq <= cutoff]
            changes[name] = [TABLES[name].to_row(row) for row in rows]
            if rows:
                next_since = max(next_since, rows[-1].server_seq)

        return Response({
            'changes': changes,
            'next_since': next_since,
            'has_more': cutoff is not None,
            # A book that appears here but is unknown to the client was granted
            # after the cursor moved past its rows: the client re-pulls from 0.
            # A book the client holds that is missing here must be dropped.
            'accessible_cashbook_ids': list(
                workspace.cashbook_cashbook_set.filter(
                    deleted_at__isnull=True, id__in=accessible_cashbook_ids(request.user, role)
                ).values_list('id', flat=True)
            ),
            'billing_stamp': Stamp.current(request.user.pk),
        })


class PushView(TenantScopedMixin, APIView):
    """
    POST {"workspace": <id>, "mutations": [
        {"id": <uuid made on the device>, "table": "entries", "op": "upsert" | "delete",
         "row_id": <uuid>, "data": {<changed columns>}}
    ]}

    Mutations are applied in order, each on its own: one rejection does not
    stop the rest. Sending a mutation again returns its first result. For an
    update the client sends only the columns it changed, so two people editing
    different fields of one row both keep their change; on the same field the
    later arrival wins. Deletes win over edits.
    """
    permission_classes = [IsAuthenticated]
    # Background sync runs often; its own budget keeps it from using up the user's
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'sync'
    # apply() runs every table's plan_gates
    billing_gate = gates.GATED

    @extend_schema(request=_push_request, responses=_push_response)
    def post(self, request):
        workspace, role = get_workspace_and_role(request.user, request.data.get('workspace'))
        if workspace is None:
            return Response({'detail': 'Workspace not found.'}, status=status.HTTP_404_NOT_FOUND)
        mutations = request.data.get('mutations')
        if not isinstance(mutations, list) or len(mutations) > MAX_PUSH_MUTATIONS:
            return Response(
                {'detail': f'mutations must be a list of at most {MAX_PUSH_MUTATIONS}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        results = [self.process(request.user, workspace, role, m) for m in mutations]
        # What was spent or what may be spent changed: the budget alerts are looked at shortly
        if any(isinstance(m, dict) and m.get('table') in BUDGET_TABLES for m in mutations):
            budgets_changed(workspace.id)
        # Money moved into or out of a goal, or a target changed: milestones are looked at shortly
        if any(isinstance(m, dict) and m.get('table') in GOAL_TABLES for m in mutations):
            goals_changed(workspace.id)
        return Response({'results': results})

    def process(self, user, workspace, role, mutation):
        try:
            mutation_id = uuid.UUID(str(mutation['id']))
            row_id = uuid.UUID(str(mutation['row_id']))
            table = TABLES[mutation['table']]
            op = mutation['op']
            data = mutation.get('data') or {}
            if op not in ('upsert', 'delete') or not isinstance(data, dict):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            mutation_id = mutation.get('id') if isinstance(mutation, dict) else None
            return {'id': mutation_id, 'status': SyncMutation.STATUS_REJECTED,
                    'error': {'code': 'malformed', 'detail': 'Mutation is malformed.'}, 'row': None}

        earlier = SyncMutation.objects.filter(id=mutation_id).first()
        if earlier is not None and (earlier.user_id != user.id or earlier.row_id != row_id):
            return {'id': str(mutation_id), 'status': SyncMutation.STATUS_REJECTED,
                    'error': {'code': 'malformed', 'detail': 'Mutation id was already used.'}, 'row': None}
        if earlier is None:
            code, detail, meta = '', '', {}
            try:
                with transaction.atomic():
                    self.apply(user, workspace, role, table, op, row_id, data)
                # The next mutation in the batch must see what this one added or removed
                forget_counts()
            except Rejected as rejection:
                code, detail = rejection.code, rejection.detail
            except gates.PlanLimit as limit:
                code, detail, meta = limit.default_code, limit.message, limit.meta
                # Here, not in the gate: the block above has rolled back by now
                gates.record_refusal(limit, user)
            except ValidationError as error:
                code, detail = 'invalid', '; '.join(error.messages)
            except IntegrityError as error:
                code, detail = 'conflict', str(error).split('\n')[0]
            earlier = SyncMutation.objects.create(
                id=mutation_id, user=user, workspace=workspace, table=table.name, row_id=row_id,
                status=SyncMutation.STATUS_REJECTED if code else SyncMutation.STATUS_APPLIED,
                error_code=code, error_detail=detail, error_meta=meta,
            )

        # The row as the server now holds it, so the client can match it exactly
        current = table.model.all_objects.filter(id=row_id, workspace=workspace).first()
        visible = current is not None and table.visible(
            table.model.all_objects.filter(id=row_id), user, role
        ).exists()
        result = {'id': str(mutation_id), 'status': earlier.status, 'row': table.to_row(current) if visible else None}
        if earlier.status == SyncMutation.STATUS_REJECTED:
            result['error'] = {
                'code': earlier.error_code, 'detail': earlier.error_detail, 'meta': earlier.error_meta,
            }
        return result

    def apply(self, user, workspace, role, table, op, row_id, data):
        obj = table.model.all_objects.select_for_update().filter(id=row_id).first()
        if obj is not None and obj.workspace_id != workspace.id:
            raise Rejected('forbidden', 'Row belongs to another workspace.')

        if op == 'delete':
            if obj is None or obj.deleted_at is not None:
                return  # already gone: deleting twice is not an error
            if not table.client_deletes or not table.can_write(user, role, obj, 'delete'):
                raise Rejected('forbidden', 'You may not delete this.')
            obj.soft_delete()
            return

        creating = obj is None
        if not creating and obj.deleted_at is not None:
            raise Rejected('deleted', 'The row was deleted.')
        if creating:
            if not table.client_creates:
                raise Rejected('forbidden', 'Only the server makes these.')
            obj = table.model(id=row_id, workspace=workspace)

        # Old values of what this push changes, for the plan gates
        before = None if creating else {}
        for column, value in data.items():
            if column not in table.writable:
                continue  # unknown or server-owned columns are ignored
            if not creating and column in table.immutable and str(getattr(obj, column)) != str(value):
                raise Rejected('immutable', f'{column} cannot change.')
            field = table.model._meta.get_field(column)
            if isinstance(field, models.IntegerField) and not (value is None and field.null):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise Rejected('invalid', f'{column} must be an integer.')
            if before is not None:
                before[column] = getattr(obj, column)
            setattr(obj, column, value)

        for column, (model, shared_column) in table.references.items():
            target_id = getattr(obj, column)
            if target_id is None:
                continue
            try:
                target = model.objects.filter(id=target_id, workspace=workspace).first()
            except (ValidationError, ValueError):
                target = None
            if target is None:
                raise Rejected('invalid', f'{column} does not exist in this workspace.')
            if shared_column and getattr(target, shared_column) != getattr(obj, shared_column):
                raise Rejected('invalid', f'{column} belongs to a different cashbook.')

        # Children copy their currency from the parent in save(); give
        # validation the same value so it does not trip on the blank column
        if hasattr(obj, 'currency') and not obj.currency:
            parent = getattr(obj, 'cashbook', None) or getattr(obj, 'account', None)
            obj.currency = parent.currency if parent is not None else workspace.default_currency
        # Validate first: the permission check below reads the row's parent,
        # which a row missing its required columns does not have
        obj.full_clean(exclude=['workspace'])
        if not table.can_write(user, role, obj, 'create' if creating else 'update'):
            raise Rejected('forbidden', 'You may not change this.')
        # What the owner's plan allows. Deletes return above and are never
        # refused: removing things is how a user gets back under a limit.
        gates.assert_workspace_writable(workspace, user)
        for gate in table.plan_gates:
            gate.check(workspace, obj, creating, before)
        table.on_write(workspace, user, obj, creating, before)
        if creating and hasattr(obj, 'created_by_id'):
            obj.created_by = user
        obj.save(force_insert=creating)
