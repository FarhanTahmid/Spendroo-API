from django.core.exceptions import ValidationError

from cashbook.models import CashBook

from .. import gates


class CashbookPlanGuard:
    """
    For viewsets that write to a cashbook or what hangs off it: refuses the
    write when the owner's plan has locked the book, its workspace or the
    caller's seat. Deletes pass, so a user can always get back under a limit.
    """
    billing_gate = gates.GATED
    #: URL kwarg that holds the cashbook id
    cashbook_kwarg = 'cashbook_pk'

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method not in ('POST', 'PUT', 'PATCH'):
            return
        cashbook_id = kwargs.get(self.cashbook_kwarg)
        if cashbook_id is None:
            return
        try:
            cashbook = CashBook.objects.select_related('workspace').filter(id=cashbook_id).first()
        except (ValidationError, ValueError, TypeError):
            cashbook = None  # not an id: the view answers with its own 404
        # Someone who may not write here anyway gets the view's own 403 or 404, not a plan message
        if cashbook is not None and cashbook.has_permission(request.user, 'add'):
            gates.assert_cashbook_writable(cashbook, request.user)
