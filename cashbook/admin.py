from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from sync.admin import TombstoneOnDeleteAdmin
from django.utils.html import format_html
from spendroo.money import format_money
from . import bills
from .models import (
    CashBook, CashBookAdditionalMember, EntryCategory, 
    PaymentMethod, Entry, EntryBills, EntryField
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
    """Shown, never added here: a bill comes in through the app, where its file is checked and counted."""
    model = EntryBills
    extra = 0
    can_delete = False
    fields = ['original_name', 'content_type', 'size_bytes', 'created_by', 'created_at']
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Entry)
class EntryAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['title', 'cashbook', 'entry_type', 'amount_minor', 'currency', 'category', 'payment_method', 'entry_date', 'created_by']
    list_filter = ['entry_type', 'source', 'entry_date', 'created_at']
    search_fields = ['title', 'remarks', 'merchant', 'cashbook__book_name', 'created_by__email']
    readonly_fields = ['id', 'currency', 'created_at', 'updated_at']
    date_hierarchy = 'entry_date'
    inlines = [EntryBillsInline]
    
    fieldsets = (
        ('Entry Information', {
            'fields': ('id', 'cashbook', 'title', 'entry_type', 'amount_minor', 'currency', 'source')
        }),
        ('Classification', {
            'fields': ('category', 'payment_method', 'created_by')
        }),
        ('Details', {
            'fields': ('remarks', 'entry_date', 'custom_values')
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
class EntryBillsAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    """Read-only. Deleting gives the owner's storage back and removes the file, as the app does."""
    list_display = ['id', 'entry', 'original_name', 'content_type', 'size_bytes', 'created_by', 'created_at', 'deleted_at']
    list_filter = ['content_type', 'created_at']
    search_fields = ['original_name', 'entry__title', 'entry__cashbook__book_name']
    readonly_fields = [
        'id', 'entry', 'cashbook', 'created_by', 'bill_file', 'content_type', 'original_name', 'size_bytes',
        'page_count', 'width', 'height', 'created_at', 'deleted_at',
    ]
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        bills.remove(obj)

    def delete_queryset(self, request, queryset):
        bills.discard(queryset)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('entry', 'entry__cashbook', 'created_by')


@admin.register(EntryField)
class EntryFieldAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['name', 'cashbook', 'kind', 'required', 'show_in_list', 'position', 'deleted_at']
    list_filter = ['kind', 'required', 'show_in_list']
    search_fields = ['name', 'cashbook__book_name']
    readonly_fields = ['id', 'workspace', 'kind', 'created_at', 'updated_at']
    fields = ['id', 'workspace', 'cashbook', 'name', 'kind', 'options', 'required', 'show_in_list', 'position',
              'created_at', 'updated_at']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('cashbook')
