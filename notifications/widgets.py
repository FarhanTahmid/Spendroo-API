"""
Keeps the business and cashbook widgets on teammates' home screens current.

A cashbook is team work: when one member writes, the others' widgets should
show it without their app being opened. The phones are not polled. Instead,
shortly after a change, each phone whose widgets show a changed book gets one
silent push, and pulls that business alone (lib/features/home_widget/business/
biz_widget_background.dart in the app). To spare batteries:

- only phones whose widgets show the business or a changed book are told,
  and only when their user may see that book;
- changes arriving together are told once (DEBOUNCE), and a business tells
  its phones at most once a MIN_GAP however busy its team is;
- the push has normal priority and one collapse key per business, so a phone
  asleep gets one message when it next wakes, not one per change;
- the message carries how far the changes go (a server_seq): a phone that
  already has them, such as the one that made them, stops before the network.
"""
import logging
import math
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from procrastinate.exceptions import AlreadyEnqueued

from workspaces.models import Membership, Workspace

from . import push
from .models import Device, WidgetPulse

logger = logging.getLogger(__name__)

#: How long after a change its push leaves: a sync brings changes by the dozen
DEBOUNCE = 10
#: The least time between two pushes for one business
MIN_GAP = timedelta(seconds=60)
#: A phone off for longer gets the news from its own sync when the app opens
TTL = 6 * 60 * 60
KIND = 'widgets'
#: Roles that see every book of the business; staff see the books granted to them
ALL_BOOKS = (*Membership.MANAGER_ROLES, Membership.ROLE_VIEWER)

_local = threading.local()


def _book_models():
    # Imported here: cashbook imports from the apps this one sits beside
    from cashbook.models import (
        CashBook,
        CashBookAdditionalMember,
        Entry,
        EntryBills,
        EntryCategory,
        EntryField,
        PaymentMethod,
    )
    return CashBook, (Entry, EntryBills, EntryCategory, PaymentMethod, EntryField, CashBookAdditionalMember)


@contextmanager
def batch():
    """Changes committed inside are told once, at the end: a sync push commits its mutations one by one."""
    if getattr(_local, 'batch', None) is not None:
        yield
        return
    pending = _local.batch = set()
    try:
        yield
    finally:
        _local.batch = None
    for workspace_id in pending:
        _after_commit(workspace_id)


def touched(workspace_id):
    """A row of a cashbook of this workspace was written: its widgets hear of it once that is committed."""
    pending = getattr(_local, 'batch', None)
    if pending is not None:
        # A write rolled back after this leaves a push that finds nothing new, and sends nothing
        pending.add(workspace_id)
    else:
        _after_commit(workspace_id)


def _after_commit(workspace_id):
    transaction.on_commit(lambda: widgets_changed(workspace_id), robust=True)


def widgets_changed(workspace_id):
    """Tell the business's widgets shortly, if any phone shows them."""
    if not Workspace.objects.filter(pk=workspace_id, kind=Workspace.KIND_BUSINESS).exists():
        return
    if not _watchers(workspace_id).exists():
        return
    _defer(workspace_id, DEBOUNCE)


def _watchers(workspace_id):
    CashBook, _ = _book_models()
    books = list(CashBook.all_objects.filter(workspace_id=workspace_id).values_list('id', flat=True))
    return Device.objects.filter(Q(widget_workspaces__contains=[workspace_id]) | Q(widget_books__overlap=books))


def _defer(workspace_id, seconds):
    from .tasks import notify_widgets

    try:
        # On its own savepoint: finding the lock taken must not spoil a transaction this runs inside
        with transaction.atomic():
            notify_widgets.configure(
                queueing_lock=f'widgets:{workspace_id}', schedule_in={'seconds': seconds},
            ).defer(workspace_id=str(workspace_id))
    except AlreadyEnqueued:
        pass  # already on its way, and will carry this change too


def changed_books(workspace_id, since):
    """Book -> the latest server_seq among its rows written after `since`, deletions included."""
    CashBook, children = _book_models()
    changed = dict(
        CashBook.all_objects.filter(workspace_id=workspace_id, server_seq__gt=since).values_list('id', 'server_seq')
    )
    for model in children:
        rows = (
            model.all_objects.filter(workspace_id=workspace_id, server_seq__gt=since)
            .values('cashbook_id').annotate(seq=Max('server_seq')).values_list('cashbook_id', 'seq')
        )
        for book_id, seq in rows:
            changed[book_id] = max(seq, changed.get(book_id, 0))
    return changed


def notify(workspace_id, now=None):
    """
    Sends the silent push to every phone that shows a changed book of the
    business. Returns how many were sent; a PushError is raised, after the
    others were tried, when none could be.
    """
    _, children = _book_models()
    grant_model = children[-1]
    workspace_id = uuid.UUID(str(workspace_id))
    now = now or timezone.now()
    pulse, _ = WidgetPulse.objects.get_or_create(workspace_id=workspace_id)
    if pulse.sent_at is not None and now - pulse.sent_at < MIN_GAP:
        _defer(workspace_id, math.ceil((MIN_GAP - (now - pulse.sent_at)).total_seconds()))
        return 0

    changed = changed_books(workspace_id, pulse.seq)
    if not changed:
        return 0
    roles = dict(
        Membership.objects.filter(workspace_id=workspace_id, deleted_at__isnull=True).values_list('user_id', 'role')
    )
    # A grant taken away just now counts too: that phone's widget has to learn the book is gone
    granted = {}
    grants = grant_model.all_objects.filter(workspace_id=workspace_id, cashbook_id__in=changed).filter(
        Q(deleted_at__isnull=True) | Q(server_seq__gt=pulse.seq)
    )
    for user_id, book_id in grants.values_list('member_id', 'cashbook_id'):
        granted.setdefault(user_id, set()).add(book_id)

    sent, failed = 0, None
    devices = Device.objects.filter(user_id__in=roles).filter(
        Q(widget_workspaces__contains=[workspace_id]) | Q(widget_books__overlap=list(changed))
    )
    for device in devices:
        visible = set(changed) if roles[device.user_id] in ALL_BOOKS else granted.get(device.user_id, set())
        if not visible:
            continue
        if workspace_id not in device.widget_workspaces and not visible & set(device.widget_books):
            continue
        data = {'kind': KIND, 'workspace_id': workspace_id, 'seq': max(changed[book] for book in visible)}
        try:
            result = push.send_data(device.fcm_token, data, collapse_key=f'{KIND}:{workspace_id}', ttl=TTL)
        except push.PushError as error:
            failed = error
            continue
        if result == push.GONE:
            device.delete()
        elif result == push.SENT:
            sent += 1
    if failed is not None and not sent:
        # Kept where it was: the retry tells the same changes
        raise failed
    pulse.seq = max(changed.values())
    pulse.sent_at = now
    pulse.save(update_fields=['seq', 'sent_at'])
    return sent
