from django.contrib import admin
from unfold.admin import ModelAdmin

from ledger_personal.admin import SYNC_READONLY
from sync.admin import TombstoneOnDeleteAdmin
from .models import Tag


@admin.register(Tag)
class TagAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    list_display = ['name', 'color', 'workspace']
    search_fields = ['name', 'workspace__owner__email']
    readonly_fields = SYNC_READONLY
