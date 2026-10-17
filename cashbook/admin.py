from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from sync.admin import TombstoneOnDeleteAdmin
from django.utils.html import format_html
from spendroo.money import format_money
from .models import (
    CashBook, CashBookAdditionalMember, EntryCategory, 
    PaymentMethod, Entry, EntryBills, EntryExtraFields
)


@admin.register(CashBook)
class CashBookAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['id','book_name', 'workspace', 'currency', 'get_balance_display', 'created_at', 'updated_at']
    list_filter = ['currency', 'created_at', 'updated_at']
    search_fields = ['book_name', 'workspace__name', 'workspace__owner__email']
    readonly_fields = ['id', 'workspace', 'created_at', 'updated_at', 'get_balance_display']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('id', 'workspace', 'created_by', 'book_name', 'description', 'currency')
        }),
        ('Balance', {
            'fields': ('get_balance_display',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_balance_display(self, obj):
        balance = obj.get_balance()
        color = 'green' if balance >= 0 else 'red'

        formatted_balance = format_money(balance, obj.currency)
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            formatted_balance
        )

    get_balance_display.short_description = 'Current Balance'



@admin.register(CashBookAdditionalMember)
class CashBookAdditionalMemberAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['member', 'cashbook', 'role', 'added_by', 'created_at']
    list_filter = ['role', 'created_at']
    search_fields = ['member__email', 'member__username', 'cashbook__book_name']
    readonly_fields = ['id', 'created_at']
    
    fieldsets = (
        ('Member Information', {
            'fields': ('id', 'cashbook', 'member', 'role')
        }),
        ('Metadata', {
            'fields': ('added_by', 'created_at')
        }),
    )


@admin.register(EntryCategory)
class EntryCategoryAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['category_name', 'cashbook', 'color', 'is_default', 'created_at']
    list_filter = ['is_default', 'created_at']
    search_fields = ['category_name', 'cashbook__book_name']
    readonly_fields = ['id', 'created_at']
    
    fieldsets = (
        ('Category Information', {
            'fields': ('id', 'cashbook', 'category_name', 'color', 'is_default')
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )


@admin.register(PaymentMethod)
class PaymentMethodAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['payment_method_name', 'cashbook', 'is_default', 'created_at']
    list_filter = ['is_default', 'created_at']
    search_fields = ['payment_method_name', 'cashbook__book_name']
    readonly_fields = ['id', 'created_at']
    
    fieldsets = (
        ('Payment Method Information', {
            'fields': ('id', 'cashbook', 'payment_method_name', 'is_default')
        }),
        ('Metadata', {
            'fields': ('created_at',)
        }),
    )


class EntryBillsInline(TabularInline):
    model = EntryBills
    extra = 0
    readonly_fields = ['id', 'created_at']
    fields = ['bill_file', 'created_at']


class EntryExtraFieldsInline(TabularInline):
    model = EntryExtraFields
    extra = 0
    readonly_fields = ['id']
    fields = ['field_name', 'field_value']


@admin.register(Entry)
class EntryAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['title', 'cashbook', 'entry_type', 'amount_minor', 'currency', 'category', 'payment_method', 'entry_date', 'created_by']
    list_filter = ['entry_type', 'source', 'entry_date', 'created_at']
    search_fields = ['title', 'remarks', 'cashbook__book_name', 'created_by__email']
    readonly_fields = ['id', 'currency', 'created_at', 'updated_at']
    date_hierarchy = 'entry_date'
    inlines = [EntryBillsInline, EntryExtraFieldsInline]
    
    fieldsets = (
        ('Entry Information', {
            'fields': ('id', 'cashbook', 'title', 'entry_type', 'amount_minor', 'currency', 'source')
        }),
        ('Classification', {
            'fields': ('category', 'payment_method', 'created_by')
        }),
        ('Details', {
            'fields': ('remarks', 'entry_date')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('cashbook', 'category', 'payment_method', 'created_by')


@admin.register(EntryBills)
class EntryBillsAdmin(ModelAdmin):
    list_display = ['id', 'entry', 'bill_file', 'created_at']
    list_filter = ['created_at']
    search_fields = ['entry__title', 'entry__cashbook__book_name']
    readonly_fields = ['id', 'created_at']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('entry', 'entry__cashbook')


@admin.register(EntryExtraFields)
class EntryExtraFieldsAdmin(ModelAdmin):
    list_display = ['field_name', 'field_value', 'entry']
    search_fields = ['field_name', 'field_value', 'entry__title']
    readonly_fields = ['id']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('entry', 'entry__cashbook')