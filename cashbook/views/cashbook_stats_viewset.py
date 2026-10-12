from rest_framework import viewsets,status
from rest_framework.decorators import action
from rest_framework.response import Response

from django.shortcuts import get_object_or_404
from django.db.models import Sum, Count, Q

from datetime import datetime

from workspaces.tenancy import TenantScopedMixin

from ..models import CashBook
from ..permissions import CashBookAccessPermission
from ..utility_funcs.reporting_utils import *

class CashBookStatsViewSet(TenantScopedMixin, viewsets.ViewSet):
    """
    ViewSet for cashbook statistics and analytics.
    Provides summary and analytics endpoints. Files to download are in reports/.
    """
    permission_classes = [CashBookAccessPermission]
    
    def get_cashbook(self, cashbook_id):
        """Helper method to get cashbook and check permissions"""
        cashbook = get_object_or_404(CashBook, id=cashbook_id)
        if not cashbook.has_permission(self.request.user, 'view'):
            return None
        return cashbook
    
    @action(detail=False, methods=['get'], url_path='summary')
    def summary(self, request, cashbook_pk=None):
        """
        Get total cash in, cash out, and balance for a cashbook.
        Optional query params: start_date, end_date (YYYY-MM-DD format)
        """
        cashbook = self.get_cashbook(cashbook_pk)
        if not cashbook:
            return Response(
                {'error': 'You do not have permission to view this cashbook'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get date range from query params
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        # Filter entries
        entries = cashbook.entry_set.all()
        if start_date and end_date:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
                entries = entries.filter(entry_date__range=[start_date, end_date])
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Calculate totals
        cash_in, cash_out = cashbook.get_totals(entries)

        return Response({
            'cashbook_id': str(cashbook.id),
            'cashbook_name': cashbook.book_name,
            'currency': cashbook.currency,
            'total_cash_in_minor': cash_in,
            'total_cash_out_minor': cash_out,
            'balance_minor': cash_in - cash_out,
            'total_entries': entries.count(),
            'date_range': {
                'start': start_date.strftime('%Y-%m-%d') if start_date else None,
                'end': end_date.strftime('%Y-%m-%d') if end_date else None,
            } if start_date and end_date else None
        })
    
    @action(detail=False, methods=['get'], url_path='analytics')
    def analytics(self, request, cashbook_pk=None):
        """
        Get analytical data for cashbook entries.
        Returns category-wise spending, payment method analysis, and trends.
        
        Query params:
        - period: 'daily', 'weekly', 'monthly', 'custom' (default: last 30 days)
        - start_date: For custom period (YYYY-MM-DD)
        - end_date: For custom period (YYYY-MM-DD)
        """
        cashbook = self.get_cashbook(cashbook_pk)
        if not cashbook:
            return Response(
                {'error': 'You do not have permission to view this cashbook'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get period parameters
        period = request.query_params.get('period', 'last_30_days')
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        # Parse dates
        try:
            if period == 'custom':
                if not start_date or not end_date:
                    return Response(
                        {'error': 'start_date and end_date required for custom period'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
            elif period in ['daily', 'weekly', 'monthly']:
                start_date, end_date = get_date_range(period)
            else:
                # Default: last 30 days
                start_date = None
                end_date = None
        except ValueError as e:
            return Response(
                {'error': f'Invalid date format: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get analytics data
        analytics_data = get_analytics_data(cashbook, start_date, end_date)
        
        return Response({
            'cashbook_id': str(cashbook.id),
            'cashbook_name': cashbook.book_name,
            'currency': cashbook.currency,
            'period': period,
            **analytics_data
        })
    
    @action(detail=False, methods=['get'], url_path='category-breakdown')
    def category_breakdown(self, request, cashbook_pk=None):
        """
        Get detailed breakdown by category for both income and expenses.
        
        Query params:
        - start_date: Optional (YYYY-MM-DD)
        - end_date: Optional (YYYY-MM-DD)
        - type: 'cash_in', 'cash_out', or 'both' (default: 'both')
        """
        cashbook = self.get_cashbook(cashbook_pk)
        if not cashbook:
            return Response(
                {'error': 'You do not have permission to view this cashbook'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get parameters
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        entry_type = request.query_params.get('type', 'both')
        
        # Filter entries
        entries = cashbook.entry_set.filter(
            category__isnull=False,
            category__deleted_at__isnull=True
        )
        
        # Apply date filter
        if start_date and end_date:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
                entries = entries.filter(entry_date__range=[start_date, end_date])
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Apply type filter
        if entry_type in ['cash_in', 'cash_out']:
            entries = entries.filter(entry_type=entry_type)
        
        # Group by category
        breakdown = entries.values(
            'category__category_name',
            'category__color',
            'entry_type'
        ).annotate(
            total=Sum('amount_minor'),
            count=Count('id')
        ).order_by('category__category_name', 'entry_type')
        
        # Organize data
        result = {}
        for item in breakdown:
            category = item['category__category_name']
            if category not in result:
                result[category] = {
                    'category_name': category,
                    'color': item['category__color'],
                    'cash_in_minor': 0,
                    'cash_out_minor': 0,
                    'cash_in_count': 0,
                    'cash_out_count': 0,
                }
            
            if item['entry_type'] == 'cash_in':
                result[category]['cash_in_minor'] = item['total']
                result[category]['cash_in_count'] = item['count']
            else:
                result[category]['cash_out_minor'] = item['total']
                result[category]['cash_out_count'] = item['count']
        
        return Response({
            'cashbook_id': str(cashbook.id),
            'cashbook_name': cashbook.book_name,
            'currency': cashbook.currency,
            'categories': list(result.values()),
            'date_range': {
                'start': start_date.strftime('%Y-%m-%d') if start_date else None,
                'end': end_date.strftime('%Y-%m-%d') if end_date else None,
            } if start_date and end_date else None
        })
    
    @action(detail=False, methods=['get'], url_path='payment-method-breakdown')
    def payment_method_breakdown(self, request, cashbook_pk=None):
        """
        Get detailed breakdown by payment method for both income and expenses.
        
        Query params:
        - start_date: Optional (YYYY-MM-DD)
        - end_date: Optional (YYYY-MM-DD)
        - type: 'cash_in', 'cash_out', or 'both' (default: 'both')
        """
        cashbook = self.get_cashbook(cashbook_pk)
        if not cashbook:
            return Response(
                {'error': 'You do not have permission to view this cashbook'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get parameters
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        entry_type = request.query_params.get('type', 'both')
        
        # Filter entries
        entries = cashbook.entry_set.filter(
            payment_method__isnull=False,
            payment_method__deleted_at__isnull=True
        )
        
        # Apply date filter
        if start_date and end_date:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
                entries = entries.filter(entry_date__range=[start_date, end_date])
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Apply type filter
        if entry_type in ['cash_in', 'cash_out']:
            entries = entries.filter(entry_type=entry_type)
        
        # Group by payment method
        breakdown = entries.values(
            'payment_method__payment_method_name',
            'entry_type'
        ).annotate(
            total=Sum('amount_minor'),
            count=Count('id')
        ).order_by('payment_method__payment_method_name', 'entry_type')
        
        # Organize data
        result = {}
        for item in breakdown:
            method = item['payment_method__payment_method_name']
            if method not in result:
                result[method] = {
                    'payment_method_name': method,
                    'cash_in_minor': 0,
                    'cash_out_minor': 0,
                    'cash_in_count': 0,
                    'cash_out_count': 0,
                }
            
            if item['entry_type'] == 'cash_in':
                result[method]['cash_in_minor'] = item['total']
                result[method]['cash_in_count'] = item['count']
            else:
                result[method]['cash_out_minor'] = item['total']
                result[method]['cash_out_count'] = item['count']
        
        return Response({
            'cashbook_id': str(cashbook.id),
            'cashbook_name': cashbook.book_name,
            'currency': cashbook.currency,
            'payment_methods': list(result.values()),
            'date_range': {
                'start': start_date.strftime('%Y-%m-%d') if start_date else None,
                'end': end_date.strftime('%Y-%m-%d') if end_date else None,
            } if start_date and end_date else None
        })