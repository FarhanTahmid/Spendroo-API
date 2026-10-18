from rest_framework import permissions


def _get_cashbook(obj):
    """Objects are either a cashbook or hang off one"""
    return obj.cashbook if hasattr(obj, 'cashbook') else obj


class CashBookAccessPermission(permissions.BasePermission):
    """
    Custom permission to check if user has access to a cashbook.
    - Workspace owner and admins have full access
    - Workspace viewers can read
    - Staff have access based on their per-book role (viewer, contributor, editor, admin)
    """

    def has_permission(self, request, view):
        # User must be authenticated
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        cashbook = _get_cashbook(obj)

        # Safe methods (GET, HEAD, OPTIONS) require view permission
        if request.method in permissions.SAFE_METHODS:
            return cashbook.has_permission(request.user, 'view')

        # Write methods require edit permission
        if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            return cashbook.has_permission(request.user, 'edit')

        return False


class CashBookAdminPermission(permissions.BasePermission):
    """
    Permission for admin-only actions like managing categories, payment methods, and members.
    """

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        return _get_cashbook(obj).has_permission(request.user, 'admin')


class IsCashBookOwnerOrMember(CashBookAccessPermission):
    """
    Permission to check if user is cashbook owner or has appropriate member role
    """


class IsCashBookOwner(permissions.BasePermission):
    """
    Only the workspace owner can delete the cashbook
    """

    def has_object_permission(self, request, view, obj):
        return _get_cashbook(obj).owner == request.user
