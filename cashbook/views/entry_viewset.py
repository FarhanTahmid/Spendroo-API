from rest_framework import viewsets,status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from django.db import transaction
from django.shortcuts import get_object_or_404

from billing import gates
from billing.api.guards import CashbookPlanGuard
from billing.catalog.keys import F
from workspaces.tenancy import TenantScopedMixin

from ..models import (
    CashBook, Entry, EntryBills, EntryExtraFields
)
from ..permissions import *
from ..serializers import (
    EntryListSerializer, EntryDetailSerializer, EntryCreateUpdateSerializer,
    EntryBillsSerializer, EntryExtraFieldsSerializer
)

MAX_BILL_BYTES = 5 * 1024 * 1024


def bill_storage_key(bill):
    return f'bill:{bill.id}'


class EntryViewSet(CashbookPlanGuard, TenantScopedMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing cashbook entries.
    Supports CRUD operations with role-based access control.
    """
    permission_classes = [CashBookAccessPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def attach_bills(self, entry, files):
        """
        Store the files as bills of the entry, counted against the storage of
        the workspace owner's plan. Call inside a transaction: a file that
        does not fit raises, and the bills stored before it must go with it.
        """
        bills = [EntryBills(entry=entry, bill_file=file, size_bytes=file.size) for file in files]
        # Count them all before writing any, so a refusal leaves no file behind
        for bill in bills:
            gates.store(entry.workspace, bill.size_bytes, bill_storage_key(bill), actor=self.request.user)
        for bill in bills:
            bill.save()
        return bills

    def require_custom_fields(self, cashbook, request):
        if request.data.get('extra_fields_data'):
            gates.require(cashbook.workspace, F.ENTRY_CUSTOM_FIELDS)

    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'list':
            return EntryListSerializer
        elif self.action == 'retrieve':
            return EntryDetailSerializer
        else:
            return EntryCreateUpdateSerializer
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        cashbook_id = self.kwargs.get('cashbook_pk')
        if cashbook_id:
            context['cashbook'] = CashBook.objects.filter(id=cashbook_id).first()
        return context

    def get_queryset(self):
        """Filter entries by cashbook and user access"""
        cashbook_id = self.kwargs.get('cashbook_pk')
        if not cashbook_id:
            return Entry.objects.none()
        
        cashbook = get_object_or_404(CashBook, id=cashbook_id)
        
        # Check if user has view permission
        if not cashbook.has_permission(self.request.user, 'view'):
            return Entry.objects.none()
        
        return Entry.objects.filter(cashbook=cashbook).select_related(
            'category', 'payment_method', 'created_by'
        ).prefetch_related('bills', 'extra_fields')
    
    def list(self, request, *args, **kwargs):
        """
        Get all entries for a cashbook with current balance.
        Returns entries list and cashbook balance.
        """
        cashbook_id = self.kwargs.get('cashbook_pk')
        cashbook = get_object_or_404(CashBook, id=cashbook_id)
        
        # Check permission
        if not cashbook.has_permission(request.user, 'view'):
            return Response(
                {'error': 'You do not have permission to view this cashbook'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        queryset = self.filter_queryset(self.get_queryset())
        
        # Pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            response = self.get_paginated_response(serializer.data)
            response.data['balance_minor'] = cashbook.get_balance()
            response.data['currency'] = cashbook.currency
            return response
        
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'results': serializer.data,
            'balance_minor': cashbook.get_balance(),
            'currency': cashbook.currency
        })
    
    def retrieve(self, request, *args, **kwargs):
        """
        Get detailed information of a single entry.
        Includes bills and extra fields.
        """
        instance = self.get_object()
        
        # Check permission
        if not instance.cashbook.has_permission(request.user, 'view'):
            return Response(
                {'error': 'You do not have permission to view this entry'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = self.get_serializer(instance)
        return Response(serializer.data)
    
    def create(self, request, *args, **kwargs):
        """
        Create a new entry with optional file uploads.
        Supports multipart form data for file uploads.
        """
        cashbook_id = self.kwargs.get('cashbook_pk')
        cashbook = get_object_or_404(CashBook, id=cashbook_id)
        
        # Adding needs less than changing: a contributor may add but not edit
        if not cashbook.has_permission(request.user, 'add'):
            return Response(
                {'error': 'You do not have permission to create entries in this cashbook'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.require_custom_fields(cashbook, request)

        files = request.FILES.getlist('bills')
        for file in files:
            if file.size > MAX_BILL_BYTES:
                return Response(
                    {'error': f'File {file.name} exceeds 5MB limit'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # The entry and its bills are saved together or not at all
        with transaction.atomic():
            entry = serializer.save(
                cashbook=cashbook,
                created_by=request.user
            )
            self.attach_bills(entry, files)

        # Return detailed response
        response_serializer = EntryDetailSerializer(entry)
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED
        )
    
    def update(self, request, *args, **kwargs):
        """
        Update an existing entry.
        Can update all fields including extra fields and bills.
        """
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        
        # Check edit permission
        if not instance.cashbook.has_permission(request.user, 'edit'):
            return Response(
                {'error': 'You do not have permission to edit this entry'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.require_custom_fields(instance.cashbook, request)

        files = request.FILES.getlist('bills')
        for file in files:
            if file.size > MAX_BILL_BYTES:
                return Response(
                    {'error': f'File {file.name} exceeds 5MB limit'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        with transaction.atomic():
            entry = serializer.save()
            self.attach_bills(entry, files)

        # Return detailed response
        response_serializer = EntryDetailSerializer(entry)
        return Response(response_serializer.data)
    
    def destroy(self, request, *args, **kwargs):
        """
        Delete an entry.
        Updates cashbook balance automatically.
        """
        instance = self.get_object()
        
        # Check edit permission
        if not instance.cashbook.has_permission(request.user, 'edit'):
            return Response(
                {'error': 'You do not have permission to delete this entry'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        cashbook = instance.cashbook
        instance.soft_delete()

        return Response({
            'message': 'Entry deleted successfully',
            'updated_balance_minor': cashbook.get_balance()
        }, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'], url_path='add-bill')
    def add_bill(self, request, cashbook_pk=None, pk=None):
        """
        Add a bill/file to an existing entry.
        """
        entry = self.get_object()
        
        # Check edit permission
        if not entry.cashbook.has_permission(request.user, 'edit'):
            return Response(
                {'error': 'You do not have permission to add bills to this entry'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        files = request.FILES.getlist('bill_file')
        if not files:
            return Response(
                {'error': 'No files provided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        for file in files:
            if file.size > MAX_BILL_BYTES:
                return Response(
                    {'error': f'File {file.name} exceeds 5MB limit'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        with transaction.atomic():
            created_bills = self.attach_bills(entry, files)

        serializer = EntryBillsSerializer(created_bills, many=True)
        return Response(
            {
                'message': f'{len(created_bills)} bill(s) added successfully',
                'bills': serializer.data
            },
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=True, methods=['delete'], url_path='remove-bill/(?P<bill_id>[^/.]+)')
    def remove_bill(self, request, cashbook_pk=None, pk=None, bill_id=None):
        """
        Remove a bill/file from an entry.
        """
        entry = self.get_object()
        
        # Check edit permission
        if not entry.cashbook.has_permission(request.user, 'edit'):
            return Response(
                {'error': 'You do not have permission to remove bills from this entry'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        bill = get_object_or_404(EntryBills, id=bill_id, entry=entry)
        with transaction.atomic():
            bill.soft_delete()
            # The owner gets the space back
            gates.release_storage(bill_storage_key(bill))

        return Response(
            {'message': 'Bill removed successfully'},
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'], url_path='add-extra-field')
    def add_extra_field(self, request, cashbook_pk=None, pk=None):
        """
        Add an extra field to an existing entry.
        """
        entry = self.get_object()
        
        # Check edit permission
        if not entry.cashbook.has_permission(request.user, 'edit'):
            return Response(
                {'error': 'You do not have permission to add extra fields to this entry'},
                status=status.HTTP_403_FORBIDDEN
            )
        gates.require(entry.workspace, F.ENTRY_CUSTOM_FIELDS)

        serializer = EntryExtraFieldsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        extra_field = serializer.save(entry=entry)
        
        return Response(
            {
                'message': 'Extra field added successfully',
                'extra_field': EntryExtraFieldsSerializer(extra_field).data
            },
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=True, methods=['put', 'patch'], url_path='update-extra-field/(?P<field_id>[^/.]+)')
    def update_extra_field(self, request, cashbook_pk=None, pk=None, field_id=None):
        """
        Update an extra field of an entry.
        """
        entry = self.get_object()
        
        # Check edit permission
        if not entry.cashbook.has_permission(request.user, 'edit'):
            return Response(
                {'error': 'You do not have permission to update extra fields'},
                status=status.HTTP_403_FORBIDDEN
            )
        gates.require(entry.workspace, F.ENTRY_CUSTOM_FIELDS)

        extra_field = get_object_or_404(EntryExtraFields, id=field_id, entry=entry)

        serializer = EntryExtraFieldsSerializer(
            extra_field,
            data=request.data, 
            partial=request.method == 'PATCH'
        )
        serializer.is_valid(raise_exception=True)
        updated_field = serializer.save()
        
        return Response(
            {
                'message': 'Extra field updated successfully',
                'extra_field': EntryExtraFieldsSerializer(updated_field).data
            },
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['delete'], url_path='remove-extra-field/(?P<field_id>[^/.]+)')
    def remove_extra_field(self, request, cashbook_pk=None, pk=None, field_id=None):
        """
        Remove an extra field from an entry.
        """
        entry = self.get_object()
        
        # Check edit permission
        if not entry.cashbook.has_permission(request.user, 'edit'):
            return Response(
                {'error': 'You do not have permission to remove extra fields'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        extra_field = get_object_or_404(EntryExtraFields, id=field_id, entry=entry)
        extra_field.soft_delete()
        
        return Response(
            {'message': 'Extra field removed successfully'},
            status=status.HTTP_200_OK
        )