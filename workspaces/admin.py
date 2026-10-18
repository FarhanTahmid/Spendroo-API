from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from sync.admin import WholeRemovalAdmin

from .models import Invitation, Workspace, Membership


class MembershipInline(TabularInline):
    model = Membership
    extra = 0
    fields = ['user', 'role', 'added_by']


@admin.register(Workspace)
class WorkspaceAdmin(WholeRemovalAdmin, ModelAdmin):
    list_display = ['name', 'kind', 'owner', 'default_currency', 'created_at']
    list_filter = ['kind', 'created_at']
    search_fields = ['name', 'owner__email']
    readonly_fields = ['id', 'version', 'created_at', 'updated_at', 'deleted_at']
    inlines = [MembershipInline]


@admin.register(Membership)
class MembershipAdmin(ModelAdmin):
    list_display = ['user', 'workspace', 'role', 'created_at']
    list_filter = ['role']
    search_fields = ['user__email', 'workspace__name']
    readonly_fields = ['id', 'version', 'created_at', 'updated_at', 'deleted_at']


@admin.register(Invitation)
class InvitationAdmin(ModelAdmin):
    """Read-only: invitations are made and taken up in the app."""
    list_display = ['__str__', 'workspace', 'role', 'status', 'invited_by', 'expires_at', 'created_at']
    list_filter = ['role']
    search_fields = ['email', 'phone', 'workspace__name']
    exclude = ['code_hash']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
