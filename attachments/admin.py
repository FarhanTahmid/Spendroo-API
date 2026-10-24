"""Admin bases for attachment models. Each app registers its own model with these."""
from unfold.admin import ModelAdmin, TabularInline

from sync.admin import TombstoneOnDeleteAdmin

from . import service

FILE_COLUMNS = ['content_type', 'original_name', 'size_bytes', 'page_count', 'width', 'height']


class AttachmentInline(TabularInline):
    """Shown, never added here: a file comes in through the app, where it is checked and counted."""
    extra = 0
    can_delete = False
    fields = ['original_name', 'content_type', 'size_bytes', 'created_by', 'created_at']
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class AttachmentAdmin(TombstoneOnDeleteAdmin, ModelAdmin):
    """Read-only. Deleting gives the owner's storage back and removes the file, as the app does."""
    #: the attachments.kinds.Kind this admin shows
    kind = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        service.remove(self.kind, obj)

    def delete_queryset(self, request, queryset):
        service.discard(self.kind, queryset)
