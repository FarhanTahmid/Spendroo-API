"""
Where attachment files live, and how a phone gets one.

Files sit in the 'bills' storage (settings.STORAGES): a private folder on disk
for now, an object store later. Bills and receipts share it. Either way nobody
reaches a file by its path. The app asks for a link that is good for a few
minutes and only for the person it was made for (link_for). On disk the link
points at AttachmentFileView, which checks it again and streams the file; an
object store signs its own.
"""
from datetime import timedelta

from django.core import signing
from django.core.files.storage import FileSystemStorage, storages
from django.core.signals import setting_changed
from django.db import transaction
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import LazyObject, empty

# Kept from when only bills had files, so a link made just before still opens
SALT = 'cashbook.bill-file'
LINK_SECONDS = 300


class _BillStorage(LazyObject):
    def _setup(self):
        self._wrapped = storages['bills']


bill_storage = _BillStorage()


def get_bill_storage():
    """For FileField(storage=...): migrations keep the name of this function, not a storage."""
    return bill_storage


@receiver(setting_changed)
def _storages_changed(*, setting, **kwargs):
    if setting in ('STORAGES', 'BILL_FILES_ROOT'):
        bill_storage._wrapped = empty


def _on_disk():
    return isinstance(storages['bills'], FileSystemStorage)


def link_for(kind, row, user):
    """(url, expires_at). On disk the url is a path on this server; an object store gives a full one."""
    expires_at = timezone.now() + timedelta(seconds=LINK_SECONDS)
    if _on_disk():
        token = signing.dumps({'k': kind.name, 'b': str(row.id), 'u': str(user.pk)}, salt=SALT, compress=True)
        return reverse('bill-file', args=[token]), expires_at
    name = kind.file_of(row).name
    try:
        url = bill_storage.url(name, expire=LINK_SECONDS)
    except TypeError:  # a backend that cannot sign: its own url, private by its own rules
        url = bill_storage.url(name)
    return url, expires_at


def read_link(token):
    """
    (kind name, row id, user id) of a link still in date. Raises
    signing.BadSignature (expired included). A link from before receipts
    names no kind: it is a bill's.
    """
    payload = signing.loads(token, salt=SALT, max_age=LINK_SECONDS)
    return payload.get('k', 'bill'), payload['b'], payload['u']


def delete_after_commit(names):
    """Remove files once the rows that held them are tombstoned for good. A rollback keeps them."""
    names = [name for name in names if name]
    if names:
        transaction.on_commit(lambda: delete_now(names))


def delete_now(names):
    for name in names:
        try:
            bill_storage.delete(name)
        except OSError:
            pass  # gone already, or the store is down: purge_bill_files picks it up later
