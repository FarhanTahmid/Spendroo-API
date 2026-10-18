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

from billing import gates
from billing.catalog.keys import F
from . import invitations
from .models import Workspace, Membership
from .serializers import (
    InvitationCodeSerializer,
    InvitationCreateSerializer,
    InvitationPreviewSerializer,
    InvitationSerializer,
    MembershipSerializer,
    WorkspaceSerializer,
)
from .services import create_demo_business, create_workspace, workspaces_for
from .tenancy import TenantScopedMixin


class WorkspaceViewSet(TenantScopedMixin,
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
