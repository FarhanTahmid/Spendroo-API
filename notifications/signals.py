from django.db.models.signals import post_save

from cashbook.models import (
    CashBook,
    CashBookAdditionalMember,
    Entry,
    EntryBills,
    EntryCategory,
    EntryField,
    PaymentMethod,
)
from workspaces.models import Membership

from . import widgets

# Whatever wrote them (a phone's sync, the REST API, the admin): the team's widgets hear of it.
# Rows tombstoned in bulk go with one of these saved (a book, an entry, a membership).
WATCHED = (CashBook, Entry, EntryBills, EntryCategory, PaymentMethod, EntryField, CashBookAdditionalMember, Membership)


def _cashbook_row_saved(sender, instance, **kwargs):
    widgets.touched(instance.workspace_id)


for model in WATCHED:
    post_save.connect(_cashbook_row_saved, sender=model, dispatch_uid=f'widgets.{model.__name__}')
