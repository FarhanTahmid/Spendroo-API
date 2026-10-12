from datetime import timedelta
from django.db.models import Sum, Count
from django.utils import timezone


def get_date_range(period_type, custom_start=None, custom_end=None):
    """
    Get start and end dates based on period type.
    period_type: 'daily', 'weekly', 'monthly', 'custom'
    """
    today = timezone.now().date()
    
    if period_type == 'daily':
        return today, today
    elif period_type == 'weekly':
        start_date = today - timedelta(days=today.weekday())  # Monday
        end_date = start_date + timedelta(days=6)  # Sunday
        return start_date, end_date
    elif period_type == 'monthly':
        start_date = today.replace(day=1)
        # Get last day of month
        if today.month == 12:
            end_date = today.replace(day=31)
        else:
            end_date = (today.replace(month=today.month + 1, day=1) - timedelta(days=1))
        return start_date, end_date
    elif period_type == 'custom':
        if not custom_start or not custom_end:
            raise ValueError("Custom date range requires start_date and end_date")
        return custom_start, custom_end
    else:
        raise ValueError("Invalid period_type")


def get_analytics_data(cashbook, start_date=None, end_date=None):
    """
    Get analytics data for cashbook entries.
    Returns data suitable for Flutter charts (pie charts, bar charts, etc.)
    """
    # Filter entries by date range if provided
    entries = cashbook.entry_set.all()
    if start_date and end_date:
        entries = entries.filter(entry_date__range=[start_date, end_date])
    
    # Category-wise spending (Cash Out only)
    category_data = entries.filter(
        entry_type='cash_out',
        category__isnull=False,
        category__deleted_at__isnull=True
    ).values(
        'category__category_name'
    ).annotate(
        total_amount=Sum('amount_minor'),
        count=Count('id')
    ).order_by('-total_amount')
    
    category_analytics = {
        'labels': [item['category__category_name'] for item in category_data],
        'values': [item['total_amount'] for item in category_data],
        'counts': [item['count'] for item in category_data],
    }
    
    # Category-wise income (Cash In only)
    category_income_data = entries.filter(
        entry_type='cash_in',
        category__isnull=False,
        category__deleted_at__isnull=True
    ).values(
        'category__category_name'
    ).annotate(
        total_amount=Sum('amount_minor'),
        count=Count('id')
    ).order_by('-total_amount')
    
    category_income_analytics = {
        'labels': [item['category__category_name'] for item in category_income_data],
        'values': [item['total_amount'] for item in category_income_data],
        'counts': [item['count'] for item in category_income_data],
    }
    
    # Payment method-wise analysis (both cash in and out)
    payment_method_data = entries.filter(
        payment_method__isnull=False,
        payment_method__deleted_at__isnull=True
    ).values(
        'payment_method__payment_method_name',
        'entry_type'
    ).annotate(
        total_amount=Sum('amount_minor'),
        count=Count('id')
    ).order_by('payment_method__payment_method_name', 'entry_type')
    
    # Organize payment method data by type
    payment_analytics = {}
    for item in payment_method_data:
        method = item['payment_method__payment_method_name']
        entry_type = item['entry_type']
        
        if method not in payment_analytics:
            payment_analytics[method] = {
                'cash_in_minor': 0,
                'cash_out_minor': 0,
                'cash_in_count': 0,
                'cash_out_count': 0,
            }
        
        if entry_type == 'cash_in':
            payment_analytics[method]['cash_in_minor'] = item['total_amount']
            payment_analytics[method]['cash_in_count'] = item['count']
        else:
            payment_analytics[method]['cash_out_minor'] = item['total_amount']
            payment_analytics[method]['cash_out_count'] = item['count']
    
    # Daily trend (last 30 days or specified range)
    if not start_date or not end_date:
        end_date = timezone.now().date()
        start_date = end_date - timedelta(days=30)
    
    daily_data = entries.filter(
        entry_date__range=[start_date, end_date]
    ).values('entry_date', 'entry_type').annotate(
        total_amount=Sum('amount_minor')
    ).order_by('entry_date')
    
    # Organize daily data
    daily_analytics = {}
    for item in daily_data:
        date_str = item['entry_date'].strftime('%Y-%m-%d')
        if date_str not in daily_analytics:
            daily_analytics[date_str] = {'cash_in_minor': 0, 'cash_out_minor': 0}
        
        if item['entry_type'] == 'cash_in':
            daily_analytics[date_str]['cash_in_minor'] = item['total_amount']
        else:
            daily_analytics[date_str]['cash_out_minor'] = item['total_amount']
    
    # Calculate totals
    cash_in_total, cash_out_total = cashbook.get_totals(entries)

    return {
        'summary': {
            'total_cash_in_minor': cash_in_total,
            'total_cash_out_minor': cash_out_total,
            'net_balance_minor': cash_in_total - cash_out_total,
            'total_transactions': entries.count(),
        },
        'category_spending': category_analytics,
        'category_income': category_income_analytics,
        'payment_methods': payment_analytics,
        'daily_trend': daily_analytics,
        'date_range': {
            'start': start_date.strftime('%Y-%m-%d'),
            'end': end_date.strftime('%Y-%m-%d'),
        }
    }