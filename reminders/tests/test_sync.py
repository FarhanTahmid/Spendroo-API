"""Rules and occurrences as the app writes and reads them, and what the plan allows."""
from billing.catalog.keys import F
from billing.testing import grant_plan
from ledger_personal.models import Transaction
from reminders.models import InboxItem, Occurrence, Rule

from .base import ReminderTestCase

PULL = '/api/v1/sync/pull/'
ME = '/api/v1/me/'


class RuleSyncTestCase(ReminderTestCase):
    def rent(self, **data):
        fields = {
            'kind': 'expense', 'title': 'Rent', 'account_id': str(self.cash.id), 'amount_minor': 150000,
            'freq': 'monthly', 'month_day': 31, 'start_on': '2026-10-13',
        }
        return self.upsert('recurring_rules', **{**fields, **data})

    def reminder(self, title='Pay the credit card', **data):
        fields = {'kind': 'reminder', 'title': title, 'freq': 'monthly', 'start_on': '2026-10-20'}
        return self.upsert('recurring_rules', **{**fields, **data})

    def send(self, *mutations):
        """Pushed as the app does; what is worked out once a change is saved has happened by the return."""
        with self.captureOnCommitCallbacks(execute=True):
            return self.push(self.alice_client, self.personal, *mutations)

    def pulled(self, table):
        response = self.alice_client.get(PULL, {'workspace': str(self.personal.id), 'since': 0})
        self.assertEqual(response.status_code, 200, response.data)
        return [row for row in response.data['changes'][table] if row['deleted_at'] is None]


class RuleSyncTests(RuleSyncTestCase):
    def test_a_rule_written_offline_gets_its_first_date_at_once(self):
        rent = self.rent(category_id=str(self.food.id), mode='auto', remind_days_before=3, remind_at='20:30:00')
        result = self.send(rent)[0]
        self.assertApplied(result)
        self.assertEqual((result['row']['currency'], result['row']['week_start'], result['row']['set_pos']), ('BDT', 5, None))

        rule = self.pulled('recurring_rules')[0]
        self.assertEqual((rule['next_due_on'].isoformat(), rule['done_count'], rule['mode']), ('2026-10-31', 0, 'auto'))
        self.assertNotIn('needs_refresh', rule)
        waiting = self.pulled('recurring_occurrences')
        self.assertEqual([(str(row['rule_id']), row['due_on'].isoformat(), row['status']) for row in waiting],
                         [(rent['row_id'], '2026-10-31', 'upcoming')])

        # A new day of the month moves the date to come; a new note does not
        self.send(self.upsert('recurring_rules', rent['row_id'], month_day=5, set_pos=None))
        self.assertEqual([row['due_on'].isoformat() for row in self.pulled('recurring_occurrences')], ['2026-11-05'])
        seq = self.pulled('recurring_occurrences')[0]['server_seq']
        self.send(self.upsert('recurring_rules', rent['row_id'], note='House'))
        self.assertEqual(self.pulled('recurring_occurrences')[0]['server_seq'], seq)

        self.send(self.delete('recurring_rules', rent['row_id']))
        self.assertEqual(self.pulled('recurring_occurrences'), [])

    def test_what_a_rule_must_and_must_not_hold(self):
        other = Rule.objects.create(workspace=self.shop, kind='reminder', title='x', freq='daily', start_on='2026-10-13')
        results = self.send(
            self.rent(account_id=None),                                   # where from?
            self.rent(amount_minor=None),
            self.rent(amount_minor=-5),
            self.rent(category_id=str(self.salary.id)),                   # an income category on an expense
            self.rent(freq='monthly', month_day=40),
            self.rent(set_pos=1),                                         # the first what?
            self.rent(kind='transfer'),                                   # where to?
            self.rent(kind='transfer', to_account_id=str(self.cash.id)),
            self.rent(to_account_id=str(self.bank.id)),                   # not a transfer
            self.rent(splits=[{'category_id': str(self.food.id), 'amount_minor': 1}, {'category_id': None, 'amount_minor': 2}]),
            self.reminder(title=''),
            self.reminder(account_id=str(self.cash.id)),
            self.upsert('recurring_rules', str(other.id), title='Mine now'),
        )
        self.assertEqual([r['status'] for r in results], ['rejected'] * len(results))
        self.assertEqual({r['error']['code'] for r in results[:-1]}, {'invalid'})
        self.assertEqual(Rule.objects.filter(workspace=self.personal).count(), 0)

        good = self.send(
            self.rent(kind='transfer', to_account_id=str(self.bank.id), freq='weekly', weekday_mask=0b0100010, interval=2),
            self.rent(freq='monthly', month_day=None, weekday_mask=1 << 4, set_pos=-1, max_count=6, end_on='2027-10-13'),
            self.rent(splits=[{'category_id': str(self.food.id), 'amount_minor': 100000},
                              {'category_id': None, 'amount_minor': 50000}]),
            self.reminder(amount_minor=None, mode='auto'),
        )
        for result in good:
            self.assertApplied(result)
        # A reminder has nothing for the server to record by itself
        self.assertEqual(good[-1]['row']['mode'], 'confirm')
        kind = self.send(self.upsert('recurring_rules', good[-1]['id'] and good[-1]['row']['id'], kind='expense'))[0]
        self.assertEqual(kind['error']['code'], 'immutable')

    def test_the_app_skips_puts_off_and_ticks_off_but_makes_nothing(self):
        rent, card = self.rent(), self.reminder(freq='once', start_on='2026-10-13')
        self.send(rent, card)
        self.on('2026-10-13')
        coming = Occurrence.objects.get(rule_id=rent['row_id'])
        due = Occurrence.objects.get(rule_id=card['row_id'])

        made = self.upsert('recurring_occurrences', rule_id=rent['row_id'], due_on='2026-12-01', status='upcoming')
        results = self.send(
            made,
            self.delete('recurring_occurrences', str(coming.id)),
            self.upsert('recurring_occurrences', str(coming.id), status='done'),      # only a transaction settles it
            self.upsert('recurring_occurrences', str(coming.id), due_on='2026-12-01'),  # ignored: not the app's
            self.upsert('recurring_occurrences', str(due.id), snoozed_until='2026-10-14T03:00:00Z'),
        )
        self.assertEqual([r['status'] for r in results], ['rejected', 'rejected', 'rejected', 'applied', 'applied'])
        self.assertEqual([r['error']['code'] for r in results[:3]], ['forbidden', 'forbidden', 'invalid'])
        coming.refresh_from_db()
        self.assertEqual((coming.due_on.isoformat(), coming.status), ('2026-10-31', 'upcoming'))

        # Skipping what was coming lines up the one after; a reminder is ticked off by hand
        results = self.send(
            self.upsert('recurring_occurrences', str(coming.id), status='skipped'),
            self.upsert('recurring_occurrences', str(due.id), status='done'),
            self.upsert('recurring_occurrences', str(due.id), status='due'),          # no way back
        )
        self.assertEqual([r['status'] for r in results], ['applied', 'applied', 'rejected'])
        self.assertEqual(self.dates(Rule.objects.get(id=rent['row_id'])),
                         [('2026-10-31', 'skipped'), ('2026-11-30', 'upcoming')])
        self.assertEqual(Rule.objects.get(id=card['row_id']).done_count, 1)

    def test_confirming_on_two_phones_records_it_once(self):
        rent = self.rent(mode='confirm', month_day=13)
        self.send(rent)
        self.on('2026-10-13')
        due = Occurrence.objects.get(rule_id=rent['row_id'], status='due')
        self.assertEqual([row['type'] for row in self.pulled('inbox_items')], ['due'])

        def paid(amount):
            return self.upsert('transactions', account_id=str(self.cash.id), kind='expense', amount_minor=-amount,
                               occurred_on='2026-10-13', occurrence_id=str(due.id))
        first, second = paid(152000), paid(150000)
        results = self.send(first, second)
        self.assertEqual([r['status'] for r in results], ['applied', 'rejected'])
        # Refused when it is checked: the occurrence already has its transaction
        self.assertEqual(results[1]['error']['code'], 'invalid')
        self.assertEqual(Transaction.objects.get(occurrence=due).amount_minor, -152000)
        due.refresh_from_db()
        self.assertEqual(due.status, 'done')
        item = self.pulled('inbox_items')[0]
        self.assertIsNotNone(item['resolved_at'])
        self.assertNotIn('pushed_at', item)

        # Which date it settles is said once
        moved = self.send(self.upsert('transactions', first['row_id'], occurrence_id=None))[0]
        self.assertEqual(moved['error']['code'], 'immutable')
        # Read, and nothing else, is the app's to say about an item
        read = self.send(self.upsert('inbox_items', str(item['id']), read_at='2026-10-13T05:00:00Z', type='overdue'))[0]
        self.assertApplied(read)
        stored = InboxItem.objects.get()
        self.assertEqual((stored.type, stored.read_at is not None), ('due', True))
        self.assertEqual(self.send(self.delete('inbox_items', str(item['id'])))[0]['error']['code'], 'forbidden')

    def test_the_time_zone_and_week_start_come_from_the_phone(self):
        response = self.alice_client.patch(ME, {'timezone': 'Europe/London', 'week_start': 0}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['timezone'], response.data['week_start']), ('Europe/London', 0))
        self.assertEqual(self.bob_client.get(ME).data['timezone'], 'Asia/Dhaka')
        for bad in ({'timezone': 'Mars/Olympus'}, {'week_start': 7}):
            self.assertEqual(self.alice_client.patch(ME, bad, format='json').status_code, 400)


class FreePlanTests(RuleSyncTestCase):
    plan = None

    def test_recurring_transactions_are_a_plan_feature_and_reminders_are_not(self):
        self.assertPlanLimit(self.send(self.rent())[0], F.RECURRING_TRANSACTIONS, 'feature')
        self.assertEqual(Rule.objects.count(), 0)

        reminders = [self.reminder(f'Reminder {number}') for number in range(4)]
        results = self.send(*reminders)
        self.assertEqual([r['status'] for r in results], ['applied'] * 3 + ['rejected'])
        self.assertPlanLimit(results[3], F.REMINDERS_STANDALONE, 'limit')
        self.assertEqual(results[3]['error']['meta']['limit'], 3)

        # A paused one does not count, so pausing makes room
        self.assertApplied(self.send(self.upsert('recurring_rules', reminders[0]['row_id'], is_paused=True))[0])
        self.assertApplied(self.send(reminders[3] | {'id': self.upsert('x')['id']})[0])
        back = self.send(self.upsert('recurring_rules', reminders[0]['row_id'], is_paused=False))[0]
        self.assertPlanLimit(back, F.REMINDERS_STANDALONE, 'limit')
        self.assertEqual(self.entitlements()['features'][str(F.REMINDERS_STANDALONE)]['limit'], 3)

    def test_after_a_downgrade_rules_rest_and_can_still_be_deleted(self):
        subscription = grant_plan(self.alice, 'plus')
        type(subscription).objects.update(starts_at=self.clock)
        rent = self.rent(mode='auto', month_day=13)
        self.assertApplied(self.send(rent)[0])
        self.on('2026-10-13')
        self.assertEqual(Transaction.objects.filter(occurrence__isnull=False).count(), 1)

        subscription.delete()
        self.on('2026-11-13')
        rule = Rule.objects.get(id=rent['row_id'])
        self.assertEqual((self.dates(rule), rule.next_due_on), ([('2026-10-13', 'done')], None))
        self.assertEqual(Transaction.objects.filter(occurrence__isnull=False).count(), 1)
        # Kept and shown, read-only
        self.assertPlanLimit(self.send(self.upsert('recurring_rules', rent['row_id'], title='Flat'))[0],
                             F.RECURRING_TRANSACTIONS)

        # Back on the plan it picks up from today, filling in nothing it missed
        grant_plan(self.alice, 'plus')
        type(subscription).objects.update(starts_at=self.clock)
        self.on('2026-12-20')
        self.assertEqual(self.dates(rule), [('2026-10-13', 'done'), ('2027-01-13', 'upcoming')])
        self.assertEqual(Transaction.objects.filter(occurrence__isnull=False).count(), 1)

        # Deleting is always allowed, whatever the plan
        type(subscription).objects.all().delete()
        self.assertApplied(self.send(self.delete('recurring_rules', rent['row_id']))[0])
