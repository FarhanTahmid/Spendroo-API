from django.contrib import admin
from unfold.admin import ModelAdmin

from sync.admin import TombstoneOnDeleteAdmin
from .models import Account, Budget, Category, SavingsGoal, Transaction

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


@admin.register(Transaction)
class TransactionAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['occurred_on', 'kind', 'amount_minor', 'currency', 'account', 'category', 'source']
    list_filter = ['kind', 'source', 'occurred_on']
    search_fields = ['note', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY + ['currency']
    date_hierarchy = 'occurred_on'


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
