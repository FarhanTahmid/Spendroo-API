from ledger_personal.models import Transaction
from reminders import engine
from reminders.models import InboxItem, Occurrence, Rule

from .base import ReminderTestCase

UPCOMING, DUE, DONE, SKIPPED = Occurrence.UPCOMING, Occurrence.DUE, Occurrence.DONE, Occurrence.SKIPPED


class FallingDueTests(ReminderTestCase):
    def test_the_next_date_waits_and_running_again_changes_nothing(self):
        rent = self.rule(month_day=31)
        self.on('2026-10-13')
        self.assertEqual(self.dates(rent), [('2026-10-31', UPCOMING)])
        rent.refresh_from_db()
        self.assertEqual((rent.next_due_on.isoformat(), rent.done_count, rent.needs_refresh), ('2026-10-31', 0, False))

        version = rent.version
        for _ in range(3):
            self.on('2026-10-20')
        rent.refresh_from_db()
        # Nothing moved, so the rule is not sent to the app again
        self.assertEqual((self.dates(rent), rent.version), ([('2026-10-31', UPCOMING)], version))
        self.assertEqual(InboxItem.objects.count(), 0)

    def test_on_the_day_the_user_is_asked_and_the_one_after_lines_up(self):
        rent = self.rule(month_day=31)
        self.on('2026-10-31', hour=1)
        self.assertEqual(self.dates(rent), [('2026-10-31', DUE), ('2026-11-30', UPCOMING)])
        # Not in the small hours: at the usual time
        self.assertEqual(self.said(), [])
        self.on('2026-10-31', hour=9)
        self.on('2026-10-31', hour=15)
        self.assertEqual(self.said(), [('due', 'due')])
        item = InboxItem.objects.get()
        self.assertEqual((item.params['title'], item.params['amount_minor'], item.params['due_on'], item.subject_table),
                         ('Rent', 150000, '2026-10-31', 'recurring_occurrences'))
        self.assertEqual(Transaction.objects.filter(occurrence__isnull=False).count(), 0)

    def test_what_is_not_answered_is_brought_up_again_then_left_alone(self):
        self.rule(month_day=31)
        for day in ('2026-10-31', '2026-11-01', '2026-11-02', '2026-11-03', '2026-11-07', '2026-11-20'):
            self.on(day)
            self.on(day, hour=18)
        self.assertEqual(self.said(), [('due', 'due'), ('overdue', 'overdue:1'), ('overdue', 'overdue:2'),
                                       ('overdue', 'overdue:3')])

    def test_after_a_quiet_spell_only_the_latest_is_said(self):
        rent = self.rule(month_day=31)
        self.on('2026-10-13')
        self.on('2026-11-05')
        self.assertEqual(self.dates(rent), [('2026-10-31', DUE), ('2026-11-30', UPCOMING)])
        self.assertEqual(self.said(), [('overdue', 'overdue:2')])

    def test_confirming_records_it_and_settles_what_was_said(self):
        rent = self.rule(month_day=31, category=self.food)
        self.on('2026-10-31')
        waiting = self.occurrence(rent, '2026-10-31')
        paid = Transaction.objects.create(
            account=self.cash, kind='expense', amount_minor=-155000, occurred_on='2026-11-01', occurrence=waiting,
        )
        rent.refresh_from_db()
        self.assertEqual((self.dates(rent), rent.done_count),
                         ([('2026-10-31', DONE), ('2026-11-30', UPCOMING)], 1))
        self.assertIsNotNone(InboxItem.objects.get().resolved_at)
        self.on('2026-11-07')
        self.assertEqual(self.said(), [('due', 'due')])  # nothing more about it

        # Taken back: it is not asked about again
        paid.soft_delete()
        rent.refresh_from_db()
        self.assertEqual((self.dates(rent)[0], rent.done_count), (('2026-10-31', SKIPPED), 0))

    def test_paying_ahead_of_the_day_moves_on_to_the_next(self):
        rent = self.rule(month_day=31)
        self.on('2026-10-13')
        Transaction.objects.create(
            account=self.cash, kind='expense', amount_minor=-150000, occurred_on='2026-10-20',
            occurrence=self.occurrence(rent, '2026-10-31'),
        )
        self.assertEqual(self.dates(rent), [('2026-10-31', DONE), ('2026-11-30', UPCOMING)])
        self.on('2026-10-31')
        self.assertEqual(self.said(), [])

    def test_a_reminder_ahead_of_the_day_says_how_many_days(self):
        rent = self.rule(month_day=31, remind_days_before=3)
        self.on('2026-10-27')
        self.on('2026-10-28', hour=8)
        self.assertEqual(self.said(), [])
        self.on('2026-10-28', hour=9)
        self.on('2026-10-29')
        self.assertEqual(self.said(), [('upcoming', 'upcoming')])
        self.assertEqual(InboxItem.objects.get().params['days'], 3)
        self.on('2026-10-31')
        self.assertEqual(self.said(), [('upcoming', 'upcoming'), ('due', 'due')])
        self.assertEqual(self.dates(rent, DUE), [('2026-10-31', DUE)])

    def test_salary_is_expected_and_a_reminder_reminds(self):
        self.rule('income', title='Salary', category=self.salary, month_day=31, remind_days_before=1)
        self.rule(Rule.KIND_REMINDER, title='Pay the credit card', freq='once', start_on='2026-10-31')
        self.on('2026-10-30')
        self.on('2026-10-31')
        self.assertEqual(sorted(kind for kind, _ in self.said()), ['income_expected', 'income_expected', 'reminder'])

    def test_put_off_until_later(self):
        rent = self.rule(month_day=31)
        self.on('2026-10-31')
        waiting = self.occurrence(rent, '2026-10-31')
        waiting.snoozed_until = self.at('2026-11-04', hour=20)
        waiting.save()
        for day in ('2026-11-01', '2026-11-03'):
            self.on(day)
        self.assertEqual(self.said(), [('due', 'due')])
        self.on('2026-11-04', hour=21)
        self.on('2026-11-08')
        self.assertEqual(len(self.said()), 2)
        self.assertEqual(self.said()[1][0], 'due')


class RecordingTests(ReminderTestCase):
    def test_an_auto_rule_records_once_and_says_so(self):
        rent = self.rule(month_day=31, mode=Rule.MODE_AUTO, category=self.food, note='House rent')
        self.on('2026-10-31', hour=1)
        self.on('2026-10-31', hour=12)
        recorded = Transaction.objects.get(occurrence__rule=rent)
        self.assertEqual(
            (recorded.amount_minor, recorded.kind, recorded.source, recorded.occurred_on.isoformat(), recorded.note,
             recorded.category, recorded.account, recorded.currency),
            (-150000, 'expense', 'recurring', '2026-10-31', 'House rent', self.food, self.cash, 'BDT'),
        )
        rent.refresh_from_db()
        self.assertEqual((self.dates(rent), rent.done_count), ([('2026-10-31', DONE), ('2026-11-30', UPCOMING)], 1))
        receipt = InboxItem.objects.get()
        self.assertEqual((receipt.type, receipt.subject_table, receipt.subject_id), ('posted', 'transactions', recorded.id))
        self.assertIsNotNone(receipt.resolved_at)

    def test_income_a_split_and_a_transfer(self):
        parts = [{'category_id': str(self.food.id), 'amount_minor': 100000}, {'category_id': None, 'amount_minor': 50000}]
        self.rule('income', title='Salary', category=self.salary, mode=Rule.MODE_AUTO, amount_minor=5000000)
        self.rule(title='Bazar', mode=Rule.MODE_AUTO, splits=parts)
        self.rule('transfer', title='Savings', mode=Rule.MODE_AUTO, amount_minor=20000)
        self.on('2026-10-13')
        rows = {row.occurrence.rule.title: row for row in Transaction.objects.filter(occurrence__isnull=False, amount_minor__lt=0)}
        self.assertEqual(Transaction.objects.get(occurrence__rule__title='Salary').amount_minor, 5000000)
        self.assertEqual([part['amount_minor'] for part in rows['Bazar'].splits], [100000, 50000])
        legs = Transaction.objects.filter(occurrence__rule__title='Savings').order_by('amount_minor')
        self.assertEqual([(leg.account, leg.amount_minor) for leg in legs], [(self.cash, -20000), (self.bank, 20000)])
        self.assertEqual(legs[0].transfer_group_id, legs[1].transfer_group_id)

        # A category deleted since reads as none in what is recorded next
        self.food.soft_delete()
        self.on('2026-11-13')
        bazar = Transaction.objects.filter(occurrence__rule__title='Bazar').order_by('occurred_on').last()
        self.assertEqual([part['category_id'] for part in bazar.splits], [None, None])

    def test_an_archived_account_holds_it_back_until_it_returns(self):
        rent = self.rule(month_day=31, mode=Rule.MODE_AUTO)
        self.cash.is_archived = True
        self.cash.save()
        self.on('2026-10-31')
        self.on('2026-11-02')
        self.assertEqual(self.dates(rent), [('2026-10-31', DUE), ('2026-11-30', UPCOMING)])
        self.assertEqual(self.said(), [('rule_blocked', 'blocked')])
        self.assertFalse(Transaction.objects.filter(occurrence__isnull=False).exists())

        self.cash.is_archived = False
        self.cash.save()
        self.on('2026-11-03')
        recorded = Transaction.objects.get(occurrence__rule=rent)
        # Dated the day it fell due, not the day it could be written
        self.assertEqual(recorded.occurred_on.isoformat(), '2026-10-31')
        self.assertEqual(self.dates(rent)[0], ('2026-10-31', DONE))
        self.assertIsNotNone(InboxItem.objects.get(type='rule_blocked').resolved_at)


class TimingTests(ReminderTestCase):
    def test_nothing_is_filled_in_before_the_rule_was_made(self):
        old = self.rule(start_on='2026-01-05', month_day=5, mode=Rule.MODE_AUTO)
        self.on('2026-10-13')
        self.assertEqual(self.dates(old), [('2026-11-05', UPCOMING)])
        self.assertFalse(Transaction.objects.filter(occurrence__isnull=False).exists())

    def test_days_the_server_was_down_are_caught_up(self):
        daily = self.rule(freq='daily')
        self.on('2026-10-13')
        self.on('2026-10-16')
        self.assertEqual(self.dates(daily), [
            ('2026-10-13', DUE), ('2026-10-14', DUE), ('2026-10-15', DUE), ('2026-10-16', DUE), ('2026-10-17', UPCOMING),
        ])

    def test_a_long_way_behind_it_skips_ahead_a_few_at_a_time(self):
        daily = self.rule(freq='daily')
        self.on('2026-10-13')
        self.on('2027-06-01')
        due = self.dates(daily, DUE)
        # 60 days back from 1 June is 2 April; one run does 24 of them
        self.assertEqual((due[1][0], len(due)), ('2027-04-02', 1 + engine.MAX_STEPS))
        for _ in range(3):
            self.on('2027-06-01', hour=11)
        self.assertEqual(self.dates(daily)[-1], ('2027-06-02', UPCOMING))

    def test_it_stops_after_its_count_or_its_last_day(self):
        twice = self.rule(freq='daily', max_count=2)
        short = self.rule(freq='daily', end_on='2026-10-14', title='Short')
        once = self.rule(Rule.KIND_REMINDER, freq='once', title='Renew passport')
        self.on('2026-10-13')
        self.on('2026-10-20')
        for rule in (twice, short):
            self.assertEqual(self.dates(rule), [('2026-10-13', DUE), ('2026-10-14', DUE)])
        self.assertEqual(self.dates(once), [('2026-10-13', DUE)])
        for rule in (twice, short, once):
            rule.refresh_from_db()
            self.assertEqual((rule.next_due_on, rule.ended), (None, True))

    def test_pausing_drops_what_was_coming_and_resuming_starts_from_today(self):
        daily = self.rule(freq='daily')
        self.on('2026-10-13')
        daily.is_paused, daily.needs_refresh = True, True
        daily.save()
        self.on('2026-10-20')
        daily.refresh_from_db()
        self.assertEqual((self.dates(daily), daily.next_due_on), ([('2026-10-13', DUE)], None))

        daily.is_paused, daily.needs_refresh = False, True
        daily.save()
        self.on('2026-10-25')
        self.assertEqual(self.dates(daily), [('2026-10-13', DUE), ('2026-10-25', DUE), ('2026-10-26', UPCOMING)])

    def test_changing_the_schedule_moves_the_date_to_come(self):
        rent = self.rule(month_day=31)
        self.on('2026-10-13')
        rent.month_day, rent.needs_refresh = 5, True
        rent.save()
        engine.refresh(rent.id)
        rent.refresh_from_db()
        self.assertEqual((self.dates(rent), rent.next_due_on.isoformat()), ([('2026-11-05', UPCOMING)], '2026-11-05'))

        # Changed on a day it already fell due: that day is not made twice
        daily = self.rule(freq='daily', title='Tea')
        self.on('2026-10-13')
        daily.interval, daily.needs_refresh = 2, True
        daily.save()
        engine.refresh(daily.id)
        self.assertEqual(self.dates(daily), [('2026-10-13', DUE), ('2026-10-15', UPCOMING)])

    def test_a_deleted_rule_leaves_nothing_open(self):
        rent = self.rule(freq='daily')
        self.on('2026-10-13')
        self.on('2026-10-13', hour=11)
        rent.soft_delete()
        self.assertEqual(self.dates(rent), [])
        self.assertIsNotNone(InboxItem.objects.get().resolved_at)
        self.on('2026-10-20')
        self.assertEqual(Occurrence.objects.filter(rule=rent).count(), 0)

    def test_the_day_turns_where_the_owner_lives(self):
        mine = self.rule(freq='once', start_on='2026-10-14')
        theirs = Rule.objects.create(
            workspace=self.shop, kind=Rule.KIND_REMINDER, title='File the return', freq='once', start_on='2026-10-14',
        )
        from workspaces.models import Workspace
        Workspace.objects.filter(pk=self.shop.pk).update(owner=self.bob)
        self.set_zone('Pacific/Kiritimati')              # UTC+14
        self.set_zone('America/Los_Angeles', self.bob)   # UTC-7
        # 13 October 12:00 UTC: already the 14th on Kiritimati, still the 13th in Los Angeles
        self.clock = self.at('2026-10-13', hour=18)
        engine.run()
        self.assertEqual((self.dates(mine), self.dates(theirs)),
                         ([('2026-10-14', DUE)], [('2026-10-14', UPCOMING)]))
