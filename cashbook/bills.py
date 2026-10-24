"""
Bills: the photos and PDFs kept with business entries. Checking, counting and
storing them is attachments/service.py's; what is the cashbook's own is who
may add and remove them, and who may open one.
"""
from attachments import kinds

from .models import EntryBills


def can_attach(entry, user):
    """An editor attaches to any entry; someone who only adds, to the entries they added."""
    book = entry.cashbook
    if book.has_permission(user, 'edit'):
        return True
    return entry.created_by_id == user.pk and book.has_permission(user, 'add')


def can_remove(bill, user):
    return bill.cashbook.has_permission(user, 'edit')


BILL = kinds.register(kinds.Kind(
    name='bill', model=EntryBills, parent_field='entry', file_field='bill_file',
    can_view=lambda user, bill: bill.cashbook.has_permission(user, 'view'),
    related=('cashbook__workspace',),
))
