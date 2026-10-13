from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.utils import timezone

from billing.models import Subscription
from identity.models import UserSettings
from ledger_personal.models import Category
from ledger_personal.tests import AccountTestCase
from reminders import engine
from reminders.models import InboxItem, Occurrence, Rule

DHAKA = ZoneInfo('Asia/Dhaka')


class ReminderTestCase(AccountTestCase):
    """
    Alice on the Business plan, in Dhaka, with the clock held at 13 October
    2026, 10:00. `on()` moves it and runs the engine, as the worker would.
    """

    def setUp(self):
        # Before anything is made: a plan granted "later" than the clock would not be in force yet
        self.clock = self.at('2026-10-13')
        patcher = mock.patch.object(timezone, 'now', side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        # A plan starts at the real time it was granted, which the held clock has not reached
        Subscription.objects.update(starts_at=self.clock - timedelta(days=1))
        self.transport = Category.objects.get(workspace=self.personal, name='Transport')

    def at(self, day, hour=10, zone=DHAKA):
        return datetime.combine(date.fromisoformat(day), time(hour), tzinfo=zone)

    def on(self, day, hour=10):
        self.clock = self.at(day, hour)
        engine.run()

    def set_zone(self, name, user=None):
        UserSettings.objects.update_or_create(user=user or self.alice, defaults={'timezone': name})

    def rule(self, kind='expense', **fields):
        fields.setdefault('freq', 'monthly')
        fields.setdefault('start_on', '2026-10-13')
        if kind != Rule.KIND_REMINDER:
            fields.setdefault('account', self.cash)
            fields.setdefault('amount_minor', 150000)
        if kind == 'transfer':
            fields.setdefault('to_account', self.bank)
        fields.setdefault('title', 'Rent')
        rule = Rule(workspace=self.personal, kind=kind, **fields)
        rule.full_clean(exclude=['workspace'])
        rule.save()
        return rule

    def dates(self, rule, *statuses):
        found = Occurrence.objects.filter(rule=rule)
        if statuses:
            found = found.filter(status__in=statuses)
        return [(occurrence.due_on.isoformat(), occurrence.status) for occurrence in found.order_by('due_on')]

    def occurrence(self, rule, day):
        return Occurrence.objects.get(rule=rule, due_on=day)

    def said(self, rule=None):
        """(type, key without ids) of what the user has been told, oldest first."""
        items = InboxItem.objects.order_by('created_at', 'id')
        return [(item.type, item.dedupe_key.split(':', 2)[2]) for item in items]
