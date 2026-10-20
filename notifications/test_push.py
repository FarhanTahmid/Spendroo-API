from datetime import time
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, override_settings
from procrastinate.contrib.django.models import ProcrastinateJob

from identity.models import UserSettings
from ledger_personal.models import Budget
from reminders import budgets
from reminders.models import InboxItem
from reminders.tests.base import ReminderTestCase

from . import delivery, push, words
from .models import Device, NotificationPreference
from .tasks import send_push

DEVICES = '/api/v1/me/devices/'
SETTINGS = '/api/v1/me/notification-settings/'


class PushTestCase(ReminderTestCase):
    """Alice's phone is signed in. `sent` collects what Firebase would have been given."""

    def setUp(self):
        super().setUp()
        self.phone = Device.objects.create(user=self.alice, fcm_token='alice-phone', locale='en')
        self.sent = []
        self.worked = set()
        self.answers = {}
        patcher = mock.patch.object(push, 'send', side_effect=self._send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, token, title, body, data=None):
        answer = self.answers.get(token, push.SENT)
        if isinstance(answer, Exception):
            raise answer
        self.sent.append((token, title, body))
        self.data = data
        return answer

    def queued(self):
        waiting = ProcrastinateJob.objects.filter(task_name='notifications.tasks.send_push')
        return [job for job in waiting if job.id not in self.worked]

    def work(self):
        """What the worker does: hand over what is due, then send what was queued."""
        delivery.deliver()
        for job in self.queued():
            send_push(**job.args)
            self.worked.add(job.id)


class DeviceTests(PushTestCase):
    def register(self, client, token='pixel', **fields):
        return client.put(DEVICES, {'token': token, **fields}, format='json')

    def test_a_phone_registers_once_and_follows_whoever_signs_in_on_it(self):
        response = self.register(self.alice_client, locale='bn', timezone='Asia/Kolkata', app_version='1.0.0')
        self.assertEqual(response.status_code, 204)
        self.register(self.alice_client, locale='en')
        phone = Device.objects.get(fcm_token='pixel')
        self.assertEqual((phone.user, phone.locale), (self.alice, 'en'))
        # Where the phone is becomes where the user's day is read
        self.assertEqual(UserSettings.objects.get(user=self.alice).timezone, 'Asia/Kolkata')

        self.register(self.bob_client)
        self.assertEqual(Device.objects.get(fcm_token='pixel').user, self.bob)
        self.assertEqual(Device.objects.count(), 2)

    def test_a_time_zone_nobody_knows_is_refused(self):
        self.assertEqual(self.register(self.alice_client, timezone='Mars/Olympus').status_code, 400)
        self.assertFalse(Device.objects.filter(fcm_token='pixel').exists())

    def test_signing_out_forgets_the_phone_and_only_ones_own(self):
        forget = f'{DEVICES}forget/'
        self.assertEqual(self.bob_client.post(forget, {'token': 'alice-phone'}, format='json').status_code, 204)
        self.assertTrue(Device.objects.filter(fcm_token='alice-phone').exists())
        self.assertEqual(self.alice_client.post(forget, {'token': 'alice-phone'}, format='json').status_code, 204)
        self.assertFalse(Device.objects.exists())

    def test_a_test_notification_goes_to_the_users_own_phones(self):
        Device.objects.create(user=self.bob, fcm_token='bob-phone')
        response = self.alice_client.post(f'{SETTINGS}test/')
        self.assertEqual((response.status_code, response.data['devices'], response.data['sent']), (200, 1, 1))
        self.assertEqual(self.sent, [('alice-phone', 'Notifications are working', 'This is how Spendroo will remind you.')])


class PreferenceTests(PushTestCase):
    def test_defaults_are_read_before_anything_is_saved_and_a_change_is_kept(self):
        response = self.alice_client.get(SETTINGS)
        self.assertEqual(
            (response.data['push_enabled'], response.data['reminder_time'], response.data['nudge_time'],
             response.data['quiet_from']),
            (True, '09:00:00', '21:00:00', None),
        )
        self.assertFalse(NotificationPreference.objects.exists())
        response = self.alice_client.patch(SETTINGS, {'budgets': False, 'reminder_time': '07:30'}, format='json')
        self.assertEqual(response.status_code, 200)
        kept = NotificationPreference.objects.get(user=self.alice)
        self.assertEqual((kept.budgets, kept.reminder_time, kept.bills), (False, time(7, 30), True))
        self.assertFalse(NotificationPreference.objects.filter(user=self.bob).exists())

    def test_quiet_hours_need_both_ends(self):
        self.assertEqual(self.alice_client.patch(SETTINGS, {'quiet_from': '22:00'}, format='json').status_code, 400)
        both = self.alice_client.patch(SETTINGS, {'quiet_from': '22:00', 'quiet_to': '07:00'}, format='json')
        self.assertEqual(both.status_code, 200)
        kept = NotificationPreference.objects.get(user=self.alice)
        self.assertEqual(
            [kept.is_quiet(time(hour)) for hour in (21, 22, 23, 3, 7, 12)], [False, True, True, True, False, False],
        )

    def test_the_users_own_hour_is_when_reminders_come(self):
        NotificationPreference.objects.create(user=self.alice, reminder_time=time(18, 0))
        self.rule(month_day=31)
        self.on('2026-10-31', hour=12)
        self.assertEqual(self.said(), [])
        self.on('2026-10-31', hour=18)
        self.assertEqual(self.said(), [('due', 'due')])
        # A rule that names its hour keeps it
        self.rule(month_day=31, title='Gas', remind_at=time(8, 0))
        self.on('2026-11-30', hour=9)
        self.assertIn(('due', 'due'), self.said()[1:])


class DeliveryTests(PushTestCase):
    def test_what_falls_due_is_pushed_once_in_the_phones_language(self):
        Device.objects.create(user=self.alice, fcm_token='alice-tablet', locale='bn_BD')
        rent = self.rule(month_day=31)
        self.on('2026-10-31')
        self.work()
        self.assertEqual(sorted(self.sent), [
            ('alice-phone', 'Rent is due today', 'BDT 1,500.00 · Tap to confirm or skip.'),
            ('alice-tablet', 'Rent: আজ দেওয়ার দিন', 'BDT ১,৫০০.০০ · নিশ্চিত করতে বা বাদ দিতে চাপুন।'),
        ])
        item = InboxItem.objects.get()
        self.assertIsNotNone(item.pushed_at)
        self.assertEqual(
            (self.data['type'], self.data['item_id'], self.data['subject_id'], self.data['route']),
            ('due', item.pk, self.occurrence(rent, '2026-10-31').pk, '/inbox'),
        )
        self.work()
        self.on('2026-10-31', hour=20)
        self.work()
        self.assertEqual(len(self.sent), 2)

    def test_a_recorded_transaction_and_a_budget_alert_are_pushed_though_settled(self):
        self.rule(month_day=31, mode='auto', category=self.food)
        Budget.objects.create(workspace=self.personal, category=self.food, amount_minor=100000)
        self.on('2026-10-31')
        budgets.evaluate(self.personal.id)
        self.work()
        self.assertEqual(sorted(title for _, title, _ in self.sent), ['Food is over its budget', 'Rent was recorded'])

    def test_answered_before_its_time_stale_or_switched_off_is_not_pushed(self):
        rent = self.rule(month_day=31)
        self.on('2026-10-31')
        waiting = self.occurrence(rent, '2026-10-31')
        waiting.status = 'skipped'
        waiting.save()
        from reminders import engine
        engine.settled(waiting.pk)
        self.work()
        self.assertEqual(self.sent, [])
        self.assertIsNotNone(InboxItem.objects.get().pushed_at)

        NotificationPreference.objects.create(user=self.alice, bills=False)
        self.on('2026-11-30')
        self.work()
        self.assertEqual(self.sent, [])
        self.assertFalse(InboxItem.objects.filter(pushed_at__isnull=True).exists())

    def test_nobody_signed_in_means_nothing_is_queued(self):
        Device.objects.all().delete()
        self.rule(month_day=31)
        self.on('2026-10-31')
        self.assertEqual(delivery.deliver(), 0)
        self.assertEqual(self.queued(), [])
        # Signing in later does not bring up what was said while away
        Device.objects.create(user=self.alice, fcm_token='new-phone')
        self.work()
        self.assertEqual(self.sent, [])

    def test_quiet_hours_hold_a_push_until_they_end(self):
        NotificationPreference.objects.create(user=self.alice, quiet_from=time(22, 0), quiet_to=time(7, 0),
                                              reminder_time=time(23, 0))
        self.rule(month_day=31)
        self.on('2026-10-31', hour=23)
        self.work()
        self.assertEqual(self.sent, [])
        self.assertIsNone(InboxItem.objects.get().pushed_at)
        self.on('2026-11-01', hour=7)
        self.work()
        self.assertEqual([title for _, title, _ in self.sent], ['Rent is due today'])

    def test_a_phone_that_is_gone_is_forgotten_and_an_outage_is_tried_again(self):
        Device.objects.create(user=self.alice, fcm_token='old-phone')
        self.answers['old-phone'] = push.GONE
        self.rule(month_day=31)
        self.on('2026-10-31')
        self.work()
        self.assertEqual([token for token, _, _ in self.sent if token == 'alice-phone'], ['alice-phone'])
        self.assertEqual(list(Device.objects.values_list('fcm_token', flat=True)), ['alice-phone'])

        self.answers['alice-phone'] = push.PushError('down')
        with self.assertRaises(push.PushError):
            delivery.send_item(InboxItem.objects.get().pk)
        self.assertTrue(Device.objects.filter(fcm_token='alice-phone').exists())


class WordingTests(SimpleTestCase):
    def test_each_kind_of_item_has_its_sentence(self):
        rent = {'title': 'Rent', 'amount_minor': 150000, 'currency': 'BDT'}
        cases = [
            ('upcoming', {**rent, 'days': 3}, 'Rent is due in 3 days', 'BDT 1,500.00'),
            ('upcoming', {**rent, 'days': 1}, 'Rent is due tomorrow', 'BDT 1,500.00'),
            ('income_expected', {**rent, 'days': 2}, 'Rent is expected in 2 days', 'BDT 1,500.00'),
            ('income_expected', rent, 'Has Rent come in?', 'BDT 1,500.00 · Tap to confirm or skip.'),
            ('overdue', rent, 'Rent is still waiting', 'BDT 1,500.00 · Tap to confirm or skip.'),
            ('rule_blocked', rent, 'Rent could not be recorded. Check its account.', 'BDT 1,500.00'),
            ('reminder', {'title': 'Call the landlord', 'amount_minor': None, 'currency': ''}, 'Call the landlord', ''),
            ('reminder', {'title': ''}, 'Untitled', ''),
            ('budget_80', {'category': 'Food', 'percent': 85, 'amount_minor': 100000, 'spent_minor': 85000,
                           'currency': 'BDT'}, 'Food: 85% of the budget is spent', 'BDT 850.00 of BDT 1,000.00'),
            ('something_newer', {'title': 'Rent'}, 'Rent', ''),
        ]
        for kind, params, title, body in cases:
            self.assertEqual(words.wording(kind, params, 'en'), (title, body), kind)
        self.assertEqual(words.wording('upcoming', {**rent, 'days': 3}, 'bn'), ('Rent: আর ৩ দিন বাকি', 'BDT ১,৫০০.০০'))
        self.assertEqual(words.language_of('fr_FR'), 'en')


class SenderTests(SimpleTestCase):
    """push.send against a Firebase that is not there."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.account = {
            'project_id': 'spendroo-test', 'client_email': 'push@spendroo-test.iam.gserviceaccount.com',
            'private_key_id': 'k1', 'token_uri': push.TOKEN_URL,
            'private_key': key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ).decode(),
        }

    def setUp(self):
        push._forget_access()
        credentials = mock.patch.object(push, '_credentials', return_value=self.account)
        credentials.start()
        self.addCleanup(credentials.stop)

    def answer(self, status, body):
        response = mock.Mock(status_code=status, text=str(body))
        response.json.return_value = body
        return response

    @override_settings(FCM_CREDENTIALS_FILE='')
    def test_without_a_key_nothing_is_sent(self):
        mock.patch.stopall()
        with mock.patch.object(push.requests, 'post') as post:
            self.assertEqual(push.send('token', 'Title', 'Body'), push.SKIPPED)
            self.assertFalse(push.configured())
        post.assert_not_called()

    def test_signs_in_once_and_sends_the_message_firebase_expects(self):
        token = self.answer(200, {'access_token': 'ya29.x', 'expires_in': 3600})
        with mock.patch.object(push.requests, 'post', side_effect=[token, self.answer(200, {}), self.answer(200, {})]) as post:
            self.assertEqual(push.send('phone', 'Rent is due today', 'BDT 1,500.00', {'item_id': 7, 'x': None}), push.SENT)
            self.assertEqual(push.send('phone', 'Again', ''), push.SENT)
        self.assertEqual(post.call_count, 3)
        sign_in, first, second = post.call_args_list
        self.assertEqual(sign_in.args[0], push.TOKEN_URL)
        self.assertEqual(first.args[0], 'https://fcm.googleapis.com/v1/projects/spendroo-test/messages:send')
        self.assertEqual(first.kwargs['headers'], {'Authorization': 'Bearer ya29.x'})
        self.assertEqual(first.kwargs['json']['message'], {
            'token': 'phone', 'notification': {'title': 'Rent is due today', 'body': 'BDT 1,500.00'},
            'data': {'item_id': '7', 'x': ''},
            'android': {'priority': 'high', 'notification': {'channel_id': 'reminders'}},
        })
        self.assertEqual(second.kwargs['json']['message']['notification'], {'title': 'Again'})

    def test_a_silent_message_shows_nothing_and_waits_for_the_phone_to_wake(self):
        token = self.answer(200, {'access_token': 'ya29.x', 'expires_in': 3600})
        gone = self.answer(404, {'error': {'status': 'NOT_FOUND', 'details': [{'errorCode': 'UNREGISTERED'}]}})
        with mock.patch.object(push.requests, 'post', side_effect=[token, self.answer(200, {}), gone]) as post:
            data = {'kind': 'widgets', 'seq': 41}
            self.assertEqual(push.send_data('phone', data, collapse_key='widgets:shop', ttl=21600), push.SENT)
            self.assertEqual(push.send_data('phone', data, collapse_key='widgets:shop', ttl=21600), push.GONE)
        self.assertEqual(post.call_args_list[1].kwargs['json']['message'], {
            'token': 'phone', 'data': {'kind': 'widgets', 'seq': '41'},
            'android': {'priority': 'normal', 'collapse_key': 'widgets:shop', 'ttl': '21600s'},
        })

    def test_firebases_answers_are_told_apart(self):
        token = self.answer(200, {'access_token': 'ya29.x', 'expires_in': 3600})
        gone = self.answer(404, {'error': {'status': 'NOT_FOUND', 'details': [{'errorCode': 'UNREGISTERED'}]}})
        bad = self.answer(400, {'error': {'status': 'INVALID_ARGUMENT', 'message': 'bad'}})
        down = self.answer(503, {'error': {'status': 'UNAVAILABLE'}})
        with mock.patch.object(push.requests, 'post', side_effect=[token, gone, bad, down]):
            self.assertEqual(push.send('phone', 'T', 'B'), push.GONE)
            self.assertEqual(push.send('phone', 'T', 'B'), push.REFUSED)
            with self.assertRaises(push.PushError):
                push.send('phone', 'T', 'B')
