"""Admin behaviour for synced models and for the things that own them (see sync/guard.py)."""
from .guard import allow_hard_delete


class TombstoneOnDeleteAdmin:
    """
    For a synced model: "delete" in the admin tombstones the rows, as the app
    does, so phones drop them on their next sync. A hard delete would leave
    them on every phone for good, and the database refuses it anyway.
    """

    def delete_model(self, request, obj):
        obj.soft_delete()

    def delete_queryset(self, request, queryset):
        # One by one, so a model's own soft_delete() (a cashbook's, say) still runs
        for obj in queryset:
            obj.soft_delete()

    def get_deleted_objects(self, objs, request):
        # Only the chosen rows are tombstoned; what hangs off them stays
        objs = list(objs)
        return [str(obj) for obj in objs], {self.model._meta.verbose_name_plural: len(objs)}, set(), []


class WholeRemovalAdmin:
    """For a workspace: deleting it takes everything of it, synced rows included. Phones drop a workspace that leaves their list."""

    def delete_model(self, request, obj):
        with allow_hard_delete():
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        with allow_hard_delete():
            super().delete_queryset(request, queryset)
