"""
What the user is told, and when. Each thing is said once: an item is found
again by its key, however often the engine comes back to it.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from identity.models import DEFAULT_TIMEZONE, UserSettings
from notifications.models import NotificationPreference

from .models import InboxItem, Occurrence, Rule

#: Days after the due date an unanswered occurrence is brought up again
OVERDUE_AFTER = (1, 3, 7)

OCCURRENCES = 'recurring_occurrences'
TRANSACTIONS = 'transactions'
RULES = 'recurring_rules'


def zone_of(workspace):
    """The owner's time zone: where their day begins and ends."""
    name = UserSettings.objects.filter(user_id=workspace.owner_id).values_list('timezone', flat=True).first()
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def today_in(workspace, now=None):
    return (now or timezone.now()).astimezone(zone_of(workspace)).date()


def usual_time_of(workspace):
    """The hour the owner is reminded at, in their own time, when a rule names none."""
    return NotificationPreference.of(workspace.owner_id).reminder_time


def moment(rule, day, zone, usual):
    """The instant a reminder about `day` is given: the rule's hour, or the usual one."""
    return datetime.combine(day, rule.remind_at or usual, tzinfo=zone)


def describe(rule, occurrence):
    """What the wording is filled from, as things stand now."""
    category = rule.category if rule.category_id and rule.category.deleted_at is None else None
    return {
        'title': rule.title or rule.note or (category.name if category else ''),
        'kind': rule.kind,
        'mode': rule.mode,
        'amount_minor': rule.amount_minor,
        'currency': rule.currency,
        'account': rule.account.name if rule.account_id else '',
        'due_on': occurrence.due_on.isoformat(),
    }


def tell(workspace, kind, key, params, subject_table, subject_id, now, resolved=False):
    """The item with this key, made now if it was never said. Returns (item, whether it is new)."""
    return InboxItem.all_objects.get_or_create(
        workspace=workspace, dedupe_key=key,
        defaults={
            'type': kind, 'params': params, 'subject_table': subject_table, 'subject_id': subject_id,
            'notify_at': now, 'resolved_at': now if resolved else None,
        },
    )


def resolve(subject_id, now=None):
    """What these items were about is settled: they ask for nothing any more."""
    for item in InboxItem.objects.filter(subject_id=subject_id, resolved_at__isnull=True):
        item.resolved_at = now or timezone.now()
        item.save(update_fields=['resolved_at'])


def _due_type(rule):
    if rule.is_reminder:
        return 'reminder'
    return 'income_expected' if rule.kind == 'income' else 'due'


def remind(occurrence, now):
    """
    Says what there is to say about an open occurrence at this moment: that it
    is coming, that it is due, that it is still waiting. Only the latest of
    these: after a quiet spell the user is not told four things at once.
    """
    rule, workspace = occurrence.rule, occurrence.workspace
    zone = zone_of(workspace)
    usual = None if rule.remind_at else usual_time_of(workspace)
    params = describe(rule, occurrence)
    subject = (OCCURRENCES, occurrence.id)

    if occurrence.status == Occurrence.UPCOMING:
        days = rule.remind_days_before
        if days and now >= moment(rule, occurrence.due_on - timedelta(days=days), zone, usual):
            left = (occurrence.due_on - now.astimezone(zone).date()).days
            if left > 0:
                kind = 'income_expected' if rule.kind == 'income' else 'upcoming'
                tell(workspace, kind, f'occ:{occurrence.id}:upcoming', {**params, 'days': left}, *subject, now)
        return

    if occurrence.status != Occurrence.DUE:
        return
    if occurrence.snoozed_until is not None:
        if now < occurrence.snoozed_until:
            return  # put off: nothing is said before then
        key = f'occ:{occurrence.id}:snooze:{occurrence.snoozed_until.isoformat()}'
        tell(workspace, _due_type(rule), key, params, *subject, now)
        return
    # An auto rule that could not record: rule_blocked has said so already
    if rule.mode == Rule.MODE_AUTO and not rule.is_reminder:
        return
    stages = [(f'occ:{occurrence.id}:due', _due_type(rule), occurrence.due_on, {})]
    stages += [
        (f'occ:{occurrence.id}:overdue:{position}', 'overdue', occurrence.due_on + timedelta(days=days), {'days': days})
        for position, days in enumerate(OVERDUE_AFTER, start=1)
    ]
    reached = [stage for stage in stages if now >= moment(rule, stage[2], zone, usual)]
    if reached:
        key, kind, _, extra = reached[-1]
        tell(workspace, kind, key, {**params, **extra}, *subject, now)
