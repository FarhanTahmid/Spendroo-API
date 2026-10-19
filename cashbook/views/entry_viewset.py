from rest_framework import viewsets,status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

import uuid

from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import APIException

from rest_framework.throttling import ScopedRateThrottle

from billing import gates
from billing.api.guards import CashbookPlanGuard
from billing.catalog.keys import F
from workspaces.tenancy import TenantScopedMixin

from .. import bill_files, bill_storage, bills, custom_fields
from ..models import CashBook, Entry, EntryBills
from ..permissions import *
from ..serializers import (
    EntryListSerializer, EntryDetailSerializer, EntryCreateUpdateSerializer,
    EntryBillsSerializer,
)

# Room for the form fields around the largest file the server reads
MULTIPART_SLACK = 1024 * 1024


class BillRefused(APIException):
    """A file that is not a whole, harmless photo or PDF. `code` says which way, for the app to word."""
    status_code = 400

    def __init__(self, code, message):
        super().__init__({'code': code, 'detail': message})


class BillIdTaken(APIException):
    status_code = 409
    default_detail = {'code': 'bill_id_taken', 'detail': 'That bill id belongs to another bill.'}


def _uuid_or_404(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise Http404


class BillUploadThrottle(ScopedRateThrottle):
    """Only requests that carry files: every photo is decoded and written out again."""
    scope_attr = 'bill_throttle_scope'

    def allow_request(self, request, view):
        if 'multipart/form-data' not in (request.content_type or ''):
            return True
        return super().allow_request(request, view)


class EntryViewSet(CashbookPlanGuard, TenantScopedMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing cashbook entries.
    Supports CRUD operations with role-based access control.
    """
    permission_classes = [CashBookAccessPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    throttle_classes = [*viewsets.ModelViewSet.throttle_classes, BillUploadThrottle]
    bill_throttle_scope = 'bill_upload'

    def initial(self, request, *args, **kwargs):
        # Before the body is read: a file bigger than any plan takes is refused unread
        length = request.META.get('CONTENT_LENGTH') or '0'
        if length.isdigit() and int(length) > bills.HARD_CAP_MB * bills.MB + MULTIPART_SLACK:
            raise BillRefused(bill_files.TOO_LARGE, f'A bill can be up to {bills.HARD_CAP_MB} MB.')
        super().initial(request, *args, **kwargs)

    def attach_bills(self, entry, uploads):
        """
        Store files as bills of the entry (see cashbook/bills.py). Call inside
        a transaction when the entry is saved in the same request: a refused
        file must take the entry with it.
        """
        try:
            return bills.attach(entry, uploads, actor=self.request.user)
        except bill_files.BillRejected as error:
            raise BillRefused(error.code, error.message)
        except bills.BillConflict:
            raise BillIdTaken()

    def require_custom_values(self, cashbook, serializer):
        """Filling in the book's own fields is the plan feature; the rest of an entry is not."""
        if 'custom_values' not in serializer.validated_data:
            return
        before = serializer.instance.custom_values if serializer.instance else {}
        if custom_fields.values_changed(before, serializer.validated_data['custom_values']):
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
        ).prefetch_related('bills')
    
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
        Includes bills and the book's field values.
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
        self.require_custom_values(cashbook, serializer)

        files = request.FILES.getlist('bills')

        # The entry and its bills are saved together or not at all
        with transaction.atomic():
            entry = serializer.save(
                cashbook=cashbook,
                created_by=request.user
            )
            self.attach_bills(entry, [(file, None) for file in files])

        # Return detailed response
        response_serializer = EntryDetailSerializer(entry)
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED
        )
    
    def update(self, request, *args, **kwargs):
        """
        Update an existing entry.
        Can update all fields including the book's field values and bills.
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
        self.require_custom_values(instance.cashbook, serializer)

        files = request.FILES.getlist('bills')

        with transaction.atomic():
            entry = serializer.save()
            self.attach_bills(entry, [(file, None) for file in files])

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
    
    def entry_for_bills(self, pk):
        """The entry, checked for viewing only: what a bill change needs is checked by the action."""
        return get_object_or_404(self.get_queryset().select_related('cashbook__workspace'), pk=pk)

    @action(detail=True, methods=['post'], url_path='add-bill')
    def add_bill(self, request, cashbook_pk=None, pk=None):
        """
        Attach files (`bill_file`, one or more) to an entry. A phone sends
        `bill_id`, an id it made, with a single file: sending it again after a
        lost answer gives back the bill stored the first time (200).
        """
        entry = self.entry_for_bills(pk)
        if not bills.can_attach(entry, request.user):
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
        bill_id = request.data.get('bill_id') or None
        if bill_id is not None:
            if len(files) != 1:
                return Response({'error': 'Send one file with a bill_id'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                bill_id = uuid.UUID(str(bill_id))
            except ValueError:
                return Response({'error': 'bill_id is not an id'}, status=status.HTTP_400_BAD_REQUEST)

        results = self.attach_bills(entry, [(file, bill_id) for file in files])
        stored = [bill for bill, _ in results]
        created = any(fresh for _, fresh in results)
        return Response(
            {
                'message': f'{len(stored)} bill(s) added successfully',
                'bills': EntryBillsSerializer(stored, many=True).data
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )

    @action(detail=True, methods=['delete'], url_path='remove-bill/(?P<bill_id>[^/.]+)')
    def remove_bill(self, request, cashbook_pk=None, pk=None, bill_id=None):
        """
        Remove a bill from an entry. The owner gets the storage back at once.
        """
        entry = self.entry_for_bills(pk)
        bill = get_object_or_404(EntryBills, id=_uuid_or_404(bill_id), entry=entry)
        if not bills.can_remove(bill, request.user):
            return Response(
                {'error': 'You do not have permission to remove bills from this entry'},
                status=status.HTTP_403_FORBIDDEN
            )
        bills.remove(bill)
        return Response(
            {'message': 'Bill removed successfully'},
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['get'], url_path='bills/(?P<bill_id>[^/.]+)/link')
    def bill_link(self, request, cashbook_pk=None, pk=None, bill_id=None):
        """A link to the bill's file, good for a few minutes and only for the one who asked."""
        entry = self.entry_for_bills(pk)
        bill = get_object_or_404(EntryBills, id=_uuid_or_404(bill_id), entry=entry)
        url, expires_at = bill_storage.link_for(bill, request.user)
        return Response({'url': url, 'expires_at': expires_at})
