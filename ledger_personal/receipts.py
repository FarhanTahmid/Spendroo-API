"""
Receipts: the photos and PDFs kept with personal expenses and income.
Checking, counting and storing them is attachments/service.py's; what is the
ledger's own is which transactions take one and who may see them.
"""
from attachments import kinds
from workspaces.models import Membership

from .models import TransactionReceipt


class ReceiptNotAllowed(Exception):
    """Money moved between one's own accounts, or into a goal, was paid to nobody: there is no receipt."""


def is_manager(user, workspace):
    """The personal ledger is for those who run the workspace, as it is in sync."""
    return workspace.role_of(user) in Membership.MANAGER_ROLES


def check_takes_receipts(transaction):
    if transaction.kind not in ('expense', 'income'):
        raise ReceiptNotAllowed()


RECEIPT = kinds.register(kinds.Kind(
    name='receipt', model=TransactionReceipt, parent_field='transaction', file_field='receipt_file',
    can_view=lambda user, receipt: is_manager(user, receipt.workspace),
    related=('workspace',),
))
