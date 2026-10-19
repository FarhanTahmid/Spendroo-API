"""
Bills attached to entries: the one place they are added and removed.

Adding checks, in order, who may attach, what each file really is
(bill_files.py), what the owner's plan allows for one file, and whether the
owner's storage has room. Nothing is written until every file passed, so a
refusal leaves no half-stored bills behind.

Removing gives the storage back at once and takes the file off the disk after
the commit. The row stays as a tombstone so phones learn of it by sync.
"""
import math
import os
import unicodedata
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from billing import gates
from billing.catalog.keys import F
from spendroo.ids import uuid7
from sync.guard import tombstone

from . import bill_files
from .bill_storage import bill_storage, delete_after_commit, delete_now
from .models import EntryBills

MB = gates.MB
# Largest file the server reads at all, whatever a plan or an override says
HARD_CAP_MB = 50
NAME_LENGTH = 120


class BillConflict(Exception):
    """A bill id the phone made is already taken by a bill of another entry."""


def storage_key(bill_id):
    return f'bill:{bill_id}'


def can_attach(entry, user):
    """An editor attaches to any entry; someone who only adds, to the entries they added."""
    book = entry.cashbook
    if book.has_permission(user, 'edit'):
        return True
    return entry.created_by_id == user.pk and book.has_permission(user, 'add')


def can_remove(bill, user):
    return bill.cashbook.has_permission(user, 'edit')


def file_cap_mb(workspace):
    """(the cap one file is held to, whether the plan set it rather than the server)."""
    limit = gates.limit_in_force(workspace, F.STORAGE_ATTACHMENT_FILE_MB)
    if limit is None or limit >= HARD_CAP_MB:
        return HARD_CAP_MB, False
    return limit, True


def clean_name(name):
    """The file's name as the person knows it, for showing only: no folders, no control characters."""
    name = os.path.basename((name or '').replace('\\', '/'))
    name = ''.join(ch for ch in unicodedata.normalize('NFC', name) if unicodedata.category(ch)[0] != 'C')
    return name.strip()[:NAME_LENGTH]


def _read(upload, workspace):
    cap, by_plan = file_cap_mb(workspace)
    try:
        return bill_files.read_upload(upload, cap * MB)
    except bill_files.BillRejected as error:
        if error.code == bill_files.TOO_LARGE and by_plan:
            # A bigger plan takes it: say so the way every plan limit does (402)
            gates.check_within(workspace, F.STORAGE_ATTACHMENT_FILE_MB, max(math.ceil(upload.size / MB), cap + 1))
        raise


def _prepare(entry, upload, bill_id, actor):
    workspace = entry.workspace
    data = _read(upload, workspace)
    if bill_files.sniff(data) == bill_files.PDF:
        gates.require(workspace, F.STORAGE_ATTACHMENT_PDF)
    inspected = bill_files.inspect(data)
    bill = EntryBills(
        id=bill_id or uuid7(), entry=entry, cashbook_id=entry.cashbook_id, created_by=actor,
        content_type=inspected.content_type, original_name=clean_name(upload.name),
        size_bytes=inspected.size, page_count=inspected.page_count,
        width=inspected.width, height=inspected.height,
    )
    return bill, inspected


def attach(entry, uploads, *, actor):
    """
    Store files as bills of the entry. `uploads` is a list of (file, bill id or
    None). Returns [(bill, created)]: a bill id sent before for this entry
    gives back that bill and counts nothing again, so a phone may retry.
    Raises BillRejected (400), PlanLimit (402) or BillConflict (409).
    """
    results, fresh = [], []
    for upload, bill_id in uploads:
        if bill_id is not None:
            earlier = EntryBills.all_objects.filter(id=bill_id).first()
            if earlier is not None:
                if earlier.entry_id != entry.id:
                    raise BillConflict()
                results.append((earlier, False))
                continue
        bill, inspected = _prepare(entry, upload, bill_id, actor)
        fresh.append((bill, inspected))
        results.append((bill, True))
    if not fresh:
        return results

    written = []
    try:
        with transaction.atomic():
            # Count them all before writing any
            for bill, inspected in fresh:
                gates.store(entry.workspace, inspected.size, storage_key(bill.id), actor=actor)
            for bill, inspected in fresh:
                bill.bill_file.save(f'{bill.id}.{inspected.extension}', ContentFile(inspected.content), save=False)
                written.append(bill.bill_file.name)
                bill.save(force_insert=True)
    except IntegrityError:
        delete_now(written)
        # The same id arrived twice at once and the other request stored it
        if len(fresh) == 1 and EntryBills.all_objects.filter(id=fresh[0][0].id, entry=entry).exists():
            return [(EntryBills.all_objects.get(id=fresh[0][0].id), False)]
        # Taken by a bill this person cannot even see
        raise BillConflict()
    except BaseException:
        delete_now(written)
        raise
    return results


def remove(bill):
    with transaction.atomic():
        bill.soft_delete()
        gates.release_storage(storage_key(bill.id))
        delete_after_commit([bill.bill_file.name])


def discard(bills):
    """Remove every live bill of the queryset at once, as their entry or book goes."""
    live = list(bills.filter(deleted_at__isnull=True).values_list('id', 'bill_file'))
    if not live:
        return
    with transaction.atomic():
        tombstone(EntryBills.all_objects.filter(id__in=[bill_id for bill_id, _ in live]))
        for bill_id, _ in live:
            gates.release_storage(storage_key(bill_id))
        delete_after_commit([name for _, name in live])


@receiver(post_delete, sender=EntryBills)
def _hard_deleted(sender, instance, **kwargs):
    """A whole workspace removed from the admin takes its bills with it: give the room back too."""
    gates.release_storage(storage_key(instance.id))
    delete_after_commit([instance.bill_file.name])


def purge_files(grace=timedelta(hours=1)):
    """
    Take off the storage every file no live bill holds: those of tombstoned
    bills whose removal after the commit failed, and those of uploads that
    rolled back. A file younger than `grace` may belong to an upload still
    under way, so it stays for the next run. Returns the names removed.
    """
    live = set(EntryBills.objects.values_list('bill_file', flat=True))
    cutoff = timezone.now() - grace
    removed = []
    for name in _walk(''):
        if name in live:
            continue
        try:
            if bill_storage.get_modified_time(name) > cutoff:
                continue
        except (NotImplementedError, OSError):
            continue
        delete_now([name])
        removed.append(name)
    return removed


def _walk(folder):
    folders, files = bill_storage.listdir(folder)
    for name in files:
        yield f'{folder}/{name}' if folder else name
    for sub in folders:
        yield from _walk(f'{folder}/{sub}' if folder else sub)
