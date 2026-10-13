"""
Hands what the inbox holds to the phones. An item is pushed once: it is
marked the moment its sending is queued, in the same transaction, so a second
pass or a second worker never sends it again.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from reminders.inbox import zone_of
from reminders.models import InboxItem

from . import push, words
from .models import Device, NotificationPreference

logger = logging.getLogger(__name__)

#: Older than this and never pushed (the worker was off, or quiet hours ran long): no longer news
STALE_AFTER = timedelta(hours=36)
#: Items made already settled that are still worth a push: they only inform
INFORMS = ('posted', 'budget_80', 'budget_over', 'goal_milestone', 'goal_reached', 'goal_behind', 'goal_idle')
#: The screen a tap opens (a go_router path in the app)
ROUTE = '/inbox'


def _worth_pushing(item, now):
    if item.notify_at < now - STALE_AFTER:
        return False
    # Answered before it could be said
    return item.resolved_at is None or item.type in INFORMS


def deliver(now=None):
    """Queues a push for every item whose time has come. Returns how many."""
    from .tasks import send_push

    now = now or timezone.now()
    queued = 0
    waiting = InboxItem.objects.filter(pushed_at__isnull=True, notify_at__lte=now).select_related('workspace')
    for item in waiting.order_by('notify_at')[:500]:
        try:
            owner_id = item.workspace.owner_id
            preference = NotificationPreference.of(owner_id)
            wanted = (
                _worth_pushing(item, now) and preference.wants(item.type)
                and Device.objects.filter(user_id=owner_id).exists()
            )
            if wanted and preference.is_quiet(now.astimezone(zone_of(item.workspace)).time()):
                continue  # kept for when the quiet hours end
            with transaction.atomic():
                # Claimed in the database, so two passes at once cannot both send it
                claimed = InboxItem.objects.filter(pk=item.pk, pushed_at__isnull=True).update(pushed_at=now)
                if claimed and wanted:
                    send_push.defer(item_id=str(item.pk))
                    queued += 1
        except Exception:
            logger.exception('Inbox item %s could not be queued for push', item.pk)
    return queued


def payload(item):
    return {
        'kind': 'inbox', 'type': item.type, 'item_id': item.pk, 'workspace_id': item.workspace_id,
        'subject_table': item.subject_table, 'subject_id': item.subject_id, 'route': ROUTE,
    }


def send_to_user(user_id, wording, data):
    """
    To every phone the user is signed in on, each in its own language.
    `wording(locale)` gives (title, body). Returns how many were sent; a
    PushError from any of them is raised after the others were tried.
    """
    sent, failed = 0, None
    for device in Device.objects.filter(user_id=user_id):
        title, body = wording(device.locale)
        try:
            result = push.send(device.fcm_token, title, body, data)
        except push.PushError as error:
            failed = error
            continue
        if result == push.GONE:
            device.delete()
        elif result == push.SENT:
            sent += 1
    if failed is not None and not sent:
        raise failed
    return sent


def send_item(item_id):
    item = InboxItem.objects.select_related('workspace').filter(pk=item_id).first()
    if item is None:
        return 0
    return send_to_user(
        item.workspace.owner_id, lambda locale: words.wording(item.type, item.params, locale), payload(item),
    )


def send_test(user_id):
    return send_to_user(
        user_id,
        lambda locale: (
            words.say('test.title', words.language_of(locale)), words.say('test.body', words.language_of(locale)),
        ),
        {'kind': 'test', 'route': ROUTE},
    )
