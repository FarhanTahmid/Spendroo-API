from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from cashbook.models import CashBook, CashBookAdditionalMember, Entry
from sync.tests import SyncTestCase
from workspaces.models import Membership

from . import push, widgets
from .models import Device, WidgetPulse
from .tasks import notify_widgets

User = get_user_model()

DEVICES = '/api/v1/me/devices/'
TASK = 'notifications.tasks.notify_widgets'


class WidgetPushTestCase(SyncTestCase):
    """
    Alice owns the Shop with two books. Bob is staff with the Till only,
    Carol a viewer. `sent` collects (token, data) as Firebase would get them.
    """

    def setUp(self):
        super().setUp()
        self.carol = User.objects.create_user(email='carol@example.com', password='pass-12345')
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_STAFF)
        Membership.objects.create(workspace=self.shop, user=self.carol, role=Membership.ROLE_VIEWER)
        self.till = CashBook.objects.create(workspace=self.shop, book_name='Till', currency='BDT')
        self.bank = CashBook.objects.create(workspace=self.shop, book_name='Bank', currency='BDT')
        CashBookAdditionalMember.objects.create(cashbook=self.till, member=self.bob, role='editor')
        self.sent = []
        self.answers = {}
        patcher = mock.patch.object(push, 'send_data', side_effect=self._send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, token, data, *, collapse_key, ttl):
        answer = self.answers.get(token, push.SENT)
        if isinstance(answer, Exception):
            raise answer
        self.sent.append((token, data))
        self.assertEqual((collapse_key, ttl), (f'widgets:{self.shop.id}', widgets.TTL))
        return answer

    def phone(self, user, token, workspaces=(), books=()):
        return Device.objects.create(
            user=user, fcm_token=token, widget_workspaces=list(workspaces), widget_books=list(books),
        )

    def queued(self):
        return list(ProcrastinateJob.objects.filter(task_name=TASK, status='todo'))

    def entry(self, book, amount=500):
        """An entry written on the server, committed."""
        with self.captureOnCommitCallbacks(execute=True):
            return Entry.objects.create(
                cashbook=book, entry_type='cash_in', amount_minor=amount, entry_date='2026-10-01',
                created_by=self.alice,
            )

    def told(self):
        return sorted(token for token, _ in self.sent)


class DeviceTargetsTests(WidgetPushTestCase):
    def test_the_phone_says_what_its_widgets_show_and_leaving_them_out_keeps_them(self):
        put = lambda **fields: self.alice_client.put(DEVICES, {'token': 'pixel', **fields}, format='json')
        self.assertEqual(put(widget_workspaces=[str(self.shop.id)], widget_books=[str(self.till.id)] * 2).status_code, 204)
        phone = Device.objects.get(fcm_token='pixel')
        self.assertEqual((phone.widget_workspaces, phone.widget_books), ([self.shop.id], [self.till.id]))

        put(locale='bn')
        phone.refresh_from_db()
        self.assertEqual((phone.locale, phone.widget_books), ('bn', [self.till.id]))

        put(widget_workspaces=[], widget_books=[])
        phone.refresh_from_db()
        self.assertEqual((phone.widget_workspaces, phone.widget_books), ([], []))

    def test_targets_must_be_ids(self):
        response = self.alice_client.put(DEVICES, {'token': 'pixel', 'widget_books': ['till']}, format='json')
        self.assertEqual(response.status_code, 400)


class QueueingTests(WidgetPushTestCase):
    def test_nothing_is_queued_while_no_phone_shows_the_business(self):
        self.phone(self.bob, 'bob-phone')
        self.entry(self.till)
        self.assertEqual(self.queued(), [])

    def test_a_change_queues_one_push_shortly_however_many_follow(self):
        self.phone(self.carol, 'carol-phone', books=[self.bank.id])
        before = timezone.now()
        self.entry(self.till)
        self.entry(self.bank)
        jobs = self.queued()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].queueing_lock, f'widgets:{self.shop.id}')
        self.assertGreaterEqual(jobs[0].scheduled_at, before + timedelta(seconds=widgets.DEBOUNCE))

    def test_a_sync_push_is_told_once_and_only_when_something_was_applied(self):
        self.phone(self.carol, 'carol-phone', workspaces=[self.shop.id])
        bad = self.upsert('entries', cashbook_id=str(self.till.id), entry_type='cash_in', amount_minor=-1,
                          entry_date='2026-10-01')
        with self.captureOnCommitCallbacks(execute=True):
            self.push(self.alice_client, self.shop, bad)
        self.assertEqual(self.queued(), [])

        good = [
            self.upsert('entries', cashbook_id=str(self.till.id), entry_type='cash_in', amount_minor=amount,
                        entry_date='2026-10-01')
            for amount in (100, 200, 300)
        ]
        with self.captureOnCommitCallbacks(execute=True):
            results = self.push(self.alice_client, self.shop, *good)
        self.assertEqual([r['status'] for r in results], ['applied'] * 3)
        self.assertEqual(len(self.queued()), 1)

    def test_a_personal_workspace_has_no_team_to_tell(self):
        self.phone(self.alice, 'alice-phone', workspaces=[self.personal.id])
        with mock.patch.object(widgets, '_defer') as defer:
            widgets.widgets_changed(self.personal.id)
        defer.assert_not_called()


class NotifyTests(WidgetPushTestCase):
    def setUp(self):
        super().setUp()
        self.entry(self.till)
        # What was there before is not news
        widgets.notify(self.shop.id, now=timezone.now() - timedelta(hours=1))
        self.sent.clear()

    def test_only_phones_showing_a_changed_book_their_user_may_see_are_told(self):
        self.phone(self.alice, 'alice-business', workspaces=[self.shop.id])
        self.phone(self.carol, 'carol-bank', books=[self.bank.id])
        self.phone(self.carol, 'carol-till', books=[self.till.id])
        self.phone(self.bob, 'bob-till', books=[self.till.id])
        self.phone(self.bob, 'bob-business', workspaces=[self.shop.id])
        self.phone(self.bob, 'bob-nothing')
        outsider = User.objects.create_user(email='eve@example.com', password='pass-12345')
        self.phone(outsider, 'eve-snooping', workspaces=[self.shop.id], books=[self.bank.id])

        entry = self.entry(self.bank)
        self.assertEqual(widgets.notify(self.shop.id), 2)
        # Bob sees only the Till, so a change in the Bank is none of his phones' business
        self.assertEqual(self.told(), ['alice-business', 'carol-bank'])
        entry.refresh_from_db()
        self.assertEqual(self.sent[0][1], {'kind': 'widgets', 'workspace_id': self.shop.id, 'seq': entry.server_seq})

    def test_staff_hear_of_their_own_books_with_how_far_those_went(self):
        self.phone(self.bob, 'bob-business', workspaces=[self.shop.id])
        till_entry = self.entry(self.till)
        self.entry(self.bank)
        widgets.notify(self.shop.id)
        till_entry.refresh_from_db()
        # The Bank moved later, but Bob's phone only needs to have pulled the Till's change
        self.assertEqual(self.sent, [('bob-business', {
            'kind': 'widgets', 'workspace_id': self.shop.id, 'seq': till_entry.server_seq,
        })])

    def test_a_grant_taken_away_reaches_the_phone_that_showed_the_book(self):
        self.phone(self.bob, 'bob-till', books=[self.till.id])
        grant = CashBookAdditionalMember.objects.get(cashbook=self.till, member=self.bob)
        with self.captureOnCommitCallbacks(execute=True):
            grant.soft_delete()
        widgets.notify(self.shop.id)
        self.assertEqual(self.told(), ['bob-till'])

    def test_what_was_told_is_not_told_again(self):
        self.phone(self.carol, 'carol-phone', workspaces=[self.shop.id])
        self.entry(self.till)
        self.assertEqual(widgets.notify(self.shop.id, now=timezone.now() - timedelta(minutes=5)), 1)
        self.assertEqual(widgets.notify(self.shop.id), 0)

    def test_a_busy_team_tells_its_phones_at_most_once_a_minute(self):
        self.phone(self.carol, 'carol-phone', workspaces=[self.shop.id])
        WidgetPulse.objects.filter(workspace=self.shop).update(sent_at=timezone.now() - timedelta(seconds=20))
        self.entry(self.till)
        ProcrastinateJob.objects.filter(task_name=TASK).delete()
        before = timezone.now()
        self.assertEqual(widgets.notify(self.shop.id), 0)
        self.assertEqual(self.sent, [])
        # Put off to when the minute is up, and then the change is told
        jobs = self.queued()
        self.assertEqual(len(jobs), 1)
        self.assertGreaterEqual(jobs[0].scheduled_at, before + timedelta(seconds=39))
        self.assertLessEqual(jobs[0].scheduled_at, before + timedelta(seconds=42))
        self.assertEqual(widgets.notify(self.shop.id, now=timezone.now() + timedelta(seconds=41)), 1)

    def test_a_phone_that_is_gone_is_forgotten_and_firebase_being_down_is_tried_again(self):
        self.phone(self.alice, 'alice-old', workspaces=[self.shop.id])
        self.phone(self.carol, 'carol-phone', workspaces=[self.shop.id])
        self.answers = {'alice-old': push.GONE, 'carol-phone': push.PushError('down')}
        self.entry(self.till)
        with self.assertRaises(push.PushError):
            notify_widgets(workspace_id=str(self.shop.id))
        self.assertFalse(Device.objects.filter(fcm_token='alice-old').exists())

        # Nothing was marked told, so the retry carries the same change
        self.answers = {}
        self.sent.clear()
        self.assertEqual(widgets.notify(self.shop.id), 1)
        self.assertEqual(self.told(), ['carol-phone'])

