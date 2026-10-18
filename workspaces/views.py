from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from audit.utils.audit_utils import AuditLogMixin
from billing import gates
from billing.catalog.keys import F
from . import invitations, members
from .models import Workspace, Membership
from .serializers import (
    InvitationCodeSerializer,
    InvitationCreateSerializer,
    InvitationPreviewSerializer,
    InvitationSerializer,
    MembershipRoleSerializer,
    MembershipSerializer,
    OwnershipTransferSerializer,
    SeatsSerializer,
    WorkspaceSerializer,
)
from .services import create_demo_business, create_workspace, workspaces_for
from .tenancy import TenantScopedMixin


class WorkspaceViewSet(TenantScopedMixin,
                       AuditLogMixin,
                       mixins.ListModelMixin,
                       mixins.RetrieveModelMixin,
                       mixins.CreateModelMixin,
                       mixins.UpdateModelMixin,
                       mixins.DestroyModelMixin,
                       viewsets.GenericViewSet):
    """
    Workspaces the user belongs to. Creating one always makes a business
    workspace; the personal workspace is created automatically on sign-up.
    """
    serializer_class = WorkspaceSerializer
    permission_classes = [IsAuthenticated]
    # create() checks how many businesses the plan allows. Renaming and
    # deleting are never refused, and the demo business does not count.
    billing_gate = gates.GATED
    # Set per action: the invitation code actions are held to a low rate
    throttle_scope = None

    def get_queryset(self):
        return workspaces_for(self.request.user)

    def create(self, request, *args, **kwargs):
        """
        The app may send its own `id`. Sending the same id again returns the
        workspace already made (200), so a retry after a lost response cannot
        create a second business.
        """
        workspace_id = None
        if request.data.get('id') is not None:
            workspace_id = serializers.UUIDField().run_validation(request.data['id'])
            existing = Workspace.all_objects.filter(id=workspace_id).first()
            if existing is not None:
                if existing.owner_id != request.user.id or existing.deleted_at is not None:
                    raise ValidationError({'id': 'This id is already in use.'})
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # One transaction, so two requests at once cannot both take the last place
        with transaction.atomic():
            gates.check_limit(request.user, F.BUSINESS_WORKSPACES)
            workspace = create_workspace(
                owner=request.user,
                name=serializer.validated_data['name'],
                kind=Workspace.KIND_BUSINESS,
                default_currency=serializer.validated_data.get('default_currency', 'BDT'),
                workspace_id=workspace_id,
            )
        return Response(self.get_serializer(workspace).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        if serializer.instance.role_of(self.request.user) not in Membership.MANAGER_ROLES:
            raise PermissionDenied('Only the owner or an admin can edit this workspace.')
        serializer.save()

    def perform_destroy(self, instance):
        if instance.kind == Workspace.KIND_PERSONAL:
            raise PermissionDenied('The personal workspace cannot be deleted.')
        if instance.owner_id != self.request.user.id:
            raise PermissionDenied('Only the owner can delete this workspace.')
        instance.soft_delete()

    @action(detail=False, methods=['post'])
    def demo(self, request):
        """A sample business with a few weeks of entries, for trying the app out."""
        workspace = create_demo_business(request.user)
        return Response(self.get_serializer(workspace).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def members(self, request, pk=None):
        workspace = self.get_object()
        memberships = workspace.memberships.select_related('user')
        return Response(MembershipSerializer(memberships, many=True).data)

    @extend_schema(responses=SeatsSerializer)
    @action(detail=True, methods=['get'])
    def seats(self, request, pk=None):
        """
        Team seats on the owner's plan: members other than the owner plus
        invitations waiting, out of the limit. Any member may ask.
        """
        return Response(SeatsSerializer(invitations.seats(self.get_object())).data)

    @extend_schema(
        methods=['patch'], request=MembershipRoleSerializer, responses=MembershipSerializer,
        parameters=[OpenApiParameter('membership_id', OpenApiTypes.UUID, OpenApiParameter.PATH)],
    )
    @extend_schema(
        methods=['delete'], request=None, responses={204: None},
        parameters=[OpenApiParameter('membership_id', OpenApiTypes.UUID, OpenApiParameter.PATH)],
    )
    @action(detail=True, methods=['patch', 'delete'], url_path=r'members/(?P<membership_id>[^/.]+)')
    def manage_member(self, request, pk=None, membership_id=None):
        """
        PATCH gives a member another role; DELETE takes them out of the
        business, or lets a member leave it. The owner can't be either.
        """
        workspace = self.get_object()
        membership_id = serializers.UUIDField().run_validation(membership_id)
        membership = get_object_or_404(workspace.memberships.select_related('user'), pk=membership_id)
        if request.method == 'DELETE':
            members.remove(workspace, request.user, membership)
            leaving = membership.user_id == request.user.id
            self.log_action(
                request, 'DELETE', 'Membership', membership.id,
                changes={'workspace': workspace.id, 'user': membership.user.email, 'role': membership.role},
                operation='leave_business' if leaving else 'remove_member',
            )
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = MembershipRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = serializer.validated_data['role']
        old = members.change_role(workspace, request.user, membership, role)
        if old != role:
            self.log_action(
                request, 'UPDATE', 'Membership', membership.id,
                changes={'workspace': workspace.id, 'user': membership.user.email, 'role': {'old': old, 'new': role}},
                operation='change_member_role',
            )
        membership.refresh_from_db()
        return Response(MembershipSerializer(membership).data)

    @extend_schema(request=OwnershipTransferSerializer, responses=WorkspaceSerializer)
    @action(detail=True, methods=['post'], url_path='transfer-ownership')
    def transfer_ownership(self, request, pk=None):
        """Hand the business to one of its admins. The owner stays on as an admin."""
        workspace = self.get_object()
        serializer = OwnershipTransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = get_object_or_404(
            workspace.memberships.select_related('user'), pk=serializer.validated_data['membership_id'],
        )
        old_owner = workspace.owner
        workspace = members.transfer_ownership(workspace, request.user, membership)
        self.log_action(
            request, 'UPDATE', 'Workspace', workspace.id,
            changes={'owner': {'old': old_owner.email, 'new': membership.user.email}},
            operation='transfer_ownership',
        )
        return Response(self.get_serializer(workspace).data)

    @extend_schema(methods=['get'], responses=InvitationSerializer(many=True))
    @extend_schema(methods=['post'], request=InvitationCreateSerializer, responses={201: InvitationSerializer})
    @action(detail=True, methods=['get', 'post'], url_path='invitations')
    def manage_invitations(self, request, pk=None):
        """
        Invitations waiting to be taken up (owner and admins), or a new one.
        A new invitation's `code` is in the response this once; an invitation
        by email is also emailed. Each one waiting holds a team seat.
        """
        workspace = self.get_object()
        if request.method == 'GET':
            if workspace.role_of(request.user) not in Membership.MANAGER_ROLES:
                raise PermissionDenied('Only the owner or an admin can see invitations.')
            waiting = invitations.pending(workspace).select_related('invited_by')
            return Response(InvitationSerializer(waiting, many=True).data)
        serializer = InvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation, code = invitations.invite(workspace, request.user, **serializer.validated_data)
        data = {**InvitationSerializer(invitation).data, 'code': code, 'emailed': bool(invitation.email)}
        return Response(data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=None, responses={204: None},
        parameters=[OpenApiParameter('invitation_id', OpenApiTypes.UUID, OpenApiParameter.PATH)],
    )
    @action(detail=True, methods=['delete'], url_path=r'invitations/(?P<invitation_id>[^/.]+)')
    def cancel_invitation(self, request, pk=None, invitation_id=None):
        workspace = self.get_object()
        invitation_id = serializers.UUIDField().run_validation(invitation_id)
        invitations.revoke(get_object_or_404(workspace.invitations, pk=invitation_id), request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        parameters=[OpenApiParameter('code', str, required=True)], responses=InvitationPreviewSerializer,
    )
    @action(
        detail=False, methods=['get'], url_path='invitations/preview',
        throttle_classes=[ScopedRateThrottle], throttle_scope='invitation_code',
    )
    def preview_invitation(self, request):
        """What a code invites to, so the app can ask "Join Shop as staff?" before accepting."""
        serializer = InvitationCodeSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return Response(InvitationPreviewSerializer(invitations.find(serializer.validated_data['code'])).data)

    @extend_schema(request=InvitationCodeSerializer, responses=WorkspaceSerializer)
    @action(
        detail=False, methods=['post'], url_path='invitations/accept',
        throttle_classes=[ScopedRateThrottle], throttle_scope='invitation_code',
    )
    def accept_invitation(self, request):
        """Join the business the code invites to. Returns it, with the role given."""
        serializer = InvitationCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workspace = invitations.accept(serializer.validated_data['code'], request.user)
        return Response(self.get_serializer(workspace).data)
