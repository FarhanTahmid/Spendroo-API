"""
The kinds of row a file can be attached to. Each app registers its own from
its AppConfig.ready(): cashbook the bills of entries, ledger_personal the
receipts of transactions. Everything else (checking the file, counting it,
storing it, the signed link, the nightly purge) works the same for all.
"""
from dataclasses import dataclass, field
from typing import Callable

from django.db.models.signals import post_delete

from billing import gates

from .storage import delete_after_commit


@dataclass(frozen=True)
class Kind:
    #: 'bill' or 'receipt': the signed link's marker and the storage counter's key prefix
    name: str
    #: the attachment model, a subclass of attachments.models.Attachment
    model: type
    #: the foreign key to the row it is attached to ('entry', 'transaction')
    parent_field: str
    #: the model's FileField
    file_field: str
    #: whether a user may still open a row's file through a link made for them
    can_view: Callable = field(compare=False)
    #: relations the file view reads to decide can_view
    related: tuple = ()

    def storage_key(self, row_id):
        return f'{self.name}:{row_id}'

    def file_of(self, row):
        return getattr(row, self.file_field)

    def parent_id_of(self, row):
        return getattr(row, f'{self.parent_field}_id')


_kinds = {}


def register(kind):
    _kinds[kind.name] = kind
    post_delete.connect(_hard_deleted, sender=kind.model, weak=False, dispatch_uid=f'attachments.{kind.name}')
    return kind


def get(name):
    return _kinds.get(name)


def every():
    return list(_kinds.values())


def of_model(model):
    return next(kind for kind in _kinds.values() if kind.model is model)


def _hard_deleted(sender, instance, **kwargs):
    """A whole workspace removed from the admin takes its files with it: give the room back too."""
    kind = of_model(sender)
    gates.release_storage(kind.storage_key(instance.id))
    delete_after_commit([kind.file_of(instance).name])
