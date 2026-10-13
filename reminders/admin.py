from django.contrib import admin
from unfold.admin import ModelAdmin

from ledger_personal.admin import SYNC_READONLY

from .models import InboxItem, Occurrence, Rule


@admin.register(Rule)
class RuleAdmin(ModelAdmin):
    list_display = ['title', 'kind', 'freq', 'interval', 'mode', 'next_due_on', 'is_paused', 'ended', 'workspace']
    list_filter = ['kind', 'freq', 'mode', 'is_paused', 'ended']
    search_fields = ['title', 'note', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY + ['currency', 'next_due_on', 'done_count', 'ended', 'needs_refresh']


@admin.register(Occurrence)
class OccurrenceAdmin(ModelAdmin):
    list_display = ['rule', 'due_on', 'status', 'snoozed_until', 'workspace']
    list_filter = ['status', 'due_on']
    search_fields = ['rule__title', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY
    date_hierarchy = 'due_on'


@admin.register(InboxItem)
class InboxItemAdmin(ModelAdmin):
    list_display = ['type', 'dedupe_key', 'notify_at', 'pushed_at', 'read_at', 'resolved_at', 'workspace']
    list_filter = ['type']
    search_fields = ['dedupe_key', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY
