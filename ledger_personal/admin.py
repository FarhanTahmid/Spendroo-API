from django.contrib import admin
from unfold.admin import ModelAdmin

from attachments.admin import FILE_COLUMNS, AttachmentAdmin, AttachmentInline
from sync.admin import TombstoneOnDeleteAdmin
from .models import Account, Budget, Category, SavingsGoal, Transaction, TransactionReceipt
from .receipts import RECEIPT

SYNC_READONLY = ['id', 'workspace', 'version', 'server_seq', 'created_at', 'updated_at', 'deleted_at']


@admin.register(Account)
class AccountAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['name', 'kind', 'currency', 'workspace', 'is_archived']
    list_filter = ['kind', 'currency', 'is_archived']
    search_fields = ['name', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY


@admin.register(Category)
class CategoryAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['name', 'kind', 'color', 'is_default', 'workspace']
    list_filter = ['kind', 'is_default']
    search_fields = ['name', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY + ['is_default']


class TransactionReceiptInline(AttachmentInline):
    model = TransactionReceipt


@admin.register(Transaction)
class TransactionAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['occurred_on', 'kind', 'amount_minor', 'currency', 'account', 'category', 'source']
    list_filter = ['kind', 'source', 'occurred_on']
    search_fields = ['note', 'merchant', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY + ['currency']
    date_hierarchy = 'occurred_on'
    inlines = [TransactionReceiptInline]


@admin.register(TransactionReceipt)
class TransactionReceiptAdmin(AttachmentAdmin):
    kind = RECEIPT
    list_display = ['id', 'transaction', 'original_name', 'content_type', 'size_bytes', 'created_by', 'created_at', 'deleted_at']
    list_filter = ['content_type', 'created_at']
    search_fields = ['original_name', 'transaction__note', 'transaction__merchant', 'workspace__owner__email']
    readonly_fields = ['id', 'transaction', 'created_by', 'receipt_file', *FILE_COLUMNS, 'created_at', 'deleted_at']
    fields = readonly_fields

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('transaction', 'created_by')


@admin.register(Budget)
class BudgetAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['category', 'amount_minor', 'currency', 'workspace']
    readonly_fields = SYNC_READONLY


@admin.register(SavingsGoal)
class SavingsGoalAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['name', 'target_minor', 'currency', 'target_on', 'status', 'workspace']
    list_filter = ['status', 'currency']
    search_fields = ['name', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY + ['account', 'currency', 'reached_at', 'milestones_hit']
