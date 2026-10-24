"""
Attachments (bills of entries, receipts of transactions): the one place they
are added and removed.

Adding checks, in order, what each file really is (files.py), what the
owner's plan allows for one file, and whether the owner's storage has room.
Who may attach is the caller's to check first. Nothing is written until every
file passed, so a refusal leaves no half-stored files behind.

Removing gives the storage back at once and takes the file off the disk after
the commit. The row stays as a tombstone so phones learn of it by sync.
"""
import math
import os
import unicodedata
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from billing import gates
from billing.catalog.keys import F
from spendroo.ids import uuid7
from sync.guard import tombstone

from . import files, kinds
from .storage import bill_storage, delete_after_commit, delete_now

MB = gates.MB
# Largest file the server reads at all, whatever a plan or an override says
HARD_CAP_MB = 50
NAME_LENGTH = 120


class AttachmentConflict(Exception):
    """An id the phone made is already taken by an attachment of another row."""


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
        return files.read_upload(upload, cap * MB)
    except files.FileRejected as error:
        if error.code == files.TOO_LARGE and by_plan:
            # A bigger plan takes it: say so the way every plan limit does (402)
            gates.check_within(workspace, F.STORAGE_ATTACHMENT_FILE_MB, max(math.ceil(upload.size / MB), cap + 1))
        raise


def _prepare(kind, parent, upload, row_id, actor):
    workspace = parent.workspace
    data = _read(upload, workspace)
    if files.sniff(data) == files.PDF:
        gates.require(workspace, F.STORAGE_ATTACHMENT_PDF)
    inspected = files.inspect(data)
    row = kind.model(
        id=row_id or uuid7(), created_by=actor, **{kind.parent_field: parent},
        content_type=inspected.content_type, original_name=clean_name(upload.name),
        size_bytes=inspected.size, page_count=inspected.page_count,
        width=inspected.width, height=inspected.height,
    )
    return row, inspected


def attach(kind, parent, uploads, *, actor):
    """
    Store files as attachments of `parent`. `uploads` is a list of (file, id
    or None). Returns [(row, created)]: an id sent before for this parent
    gives back that row and counts nothing again, so a phone may retry.
    Raises FileRejected (400), PlanLimit (402) or AttachmentConflict (409).
    """
    results, fresh = [], []
    for upload, row_id in uploads:
        if row_id is not None:
            earlier = kind.model.all_objects.filter(id=row_id).first()
            if earlier is not None:
                if kind.parent_id_of(earlier) != parent.id:
                    raise AttachmentConflict()
                results.append((earlier, False))
                continue
        row, inspected = _prepare(kind, parent, upload, row_id, actor)
        fresh.append((row, inspected))
        results.append((row, True))
    if not fresh:
        return results

    written = []
    try:
        with transaction.atomic():
            # Count them all before writing any
            for row, inspected in fresh:
                gates.store(parent.workspace, inspected.size, kind.storage_key(row.id), actor=actor)
            for row, inspected in fresh:
                stored = kind.file_of(row)
                stored.save(f'{row.id}.{inspected.extension}', ContentFile(inspected.content), save=False)
                written.append(stored.name)
                row.save(force_insert=True)
    except IntegrityError:
        delete_now(written)
        # The same id arrived twice at once and the other request stored it
        if len(fresh) == 1:
            again = kind.model.all_objects.filter(id=fresh[0][0].id).first()
            if again is not None and kind.parent_id_of(again) == parent.id:
                return [(again, False)]
        # Taken by an attachment this person cannot even see
        raise AttachmentConflict()
    except BaseException:
        delete_now(written)
        raise
    return results


def remove(kind, row):
    with transaction.atomic():
        row.soft_delete()
        gates.release_storage(kind.storage_key(row.id))
        delete_after_commit([kind.file_of(row).name])


def discard(kind, rows):
    """Remove every live attachment of the queryset at once, as the row or book they hang off goes."""
    live = list(rows.filter(deleted_at__isnull=True).values_list('id', kind.file_field))
    if not live:
        return
    with transaction.atomic():
        tombstone(kind.model.all_objects.filter(id__in=[row_id for row_id, _ in live]))
        for row_id, _ in live:
            gates.release_storage(kind.storage_key(row_id))
        delete_after_commit([name for _, name in live])


def purge_files(grace=timedelta(hours=1)):
    """
    Take off the storage every file no live attachment of any kind holds:
    those of tombstoned rows whose removal after the commit failed, and those
    of uploads that rolled back. A file younger than `grace` may belong to an
    upload still under way, so it stays for the next run. Returns the names removed.
    """
    live = set()
    for kind in kinds.every():
        live.update(kind.model.objects.values_list(kind.file_field, flat=True))
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
    folders, names = bill_storage.listdir(folder)
    for name in names:
        yield f'{folder}/{name}' if folder else name
    for sub in folders:
        yield from _walk(f'{folder}/{sub}' if folder else sub)
