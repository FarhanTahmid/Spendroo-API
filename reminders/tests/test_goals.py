"""Savings goals as the app writes them, what the plan allows, and what the user is told about them."""
import uuid
from types import SimpleNamespace
from datetime import date, timedelta

from billing.catalog import defaults
from billing.catalog.keys import F
from billing import gates
from billing.gates import REASON_LIMIT, REASON_LOCKED
from billing.entitlements import forget
from billing.models import EntitlementOverride
from billing.testing import set_limit
from ledger_personal import goals as goal_math
from ledger_personal.models import Account, SavingsGoal
from notifications import words
from reminders import goals
from reminders.models import InboxItem, Rule

from .base import ReminderTestCase


class GoalTestCase(ReminderTestCase):
    def send(self, *mutations):
        with self.captureOnCommitCallbacks(execute=True):
            return self.push(self.alice_client, self.personal, *mutations)

    def new_goal(self, name='Laptop', target=5000000, **data):
        """The two mutations the app sends for a new goal: its account, then the goal."""
        account = self.upsert('accounts', name=name, kind='goal', currency='BDT')
        goal = self.upsert(
            'savings_goals', account_id=account['row_id'], name=name, target_minor=target, **data,
        )
        return account, goal

    def make_goal(self, name='Laptop', target=5000000, **data):
        results = self.send(*self.new_goal(name, target, **data))
        for result in results:
            self.assertApplied(result)
        return SavingsGoal.objects.get(id=results[1]['row']['id'])

    def legs(self, source, target, amount, day='2026-10-13'):
        group = str(uuid.uuid4())
        return [
            self.upsert('transactions', account_id=str(source.id), kind='transfer', amount_minor=-amount,
                        transfer_group_id=group, occurred_on=day),
            self.upsert('transactions', account_id=str(target.id), kind='transfer', amount_minor=amount,
                        transfer_group_id=group, occurred_on=day),
        ]

    def told(self):
        """(type, milestone) of what the user was told, oldest first."""
        return [(item.type, item.params.get('mark')) for item in InboxItem.objects.order_by('created_at', 'id')]


class GoalSyncTests(GoalTestCase):
    def test_a_goal_is_made_with_its_account_and_money_moves_in_by_transfer(self):
        goal = self.make_goal(target_on='2027-03-15', icon='laptop', color='#3B9EE5')
        self.assertEqual((goal.currency, goal.status, goal.account.kind), ('BDT', 'active', 'goal'))

        for result in self.send(*self.legs(self.cash, goal.account, 30000)):
            self.assertApplied(result)
        progress = goal_math.progress(goal, date(2026, 10, 13))
        self.assertEqual((progress.saved_minor, progress.percent, progress.remaining_minor), (30000, 0, 4970000))

    def test_money_is_never_spent_straight_from_a_goal(self):
        goal = self.make_goal()
        results = self.send(
            self.upsert('transactions', account_id=str(goal.account_id), kind='expense', amount_minor=-100,
                        occurred_on='2026-10-13'),
            self.upsert('transactions', account_id=str(goal.account_id), kind='income', amount_minor=100,
                        occurred_on='2026-10-13'),
            # No schedule takes money out of a goal
            self.upsert('recurring_rules', kind='transfer', account_id=str(goal.account_id),
                        to_account_id=str(self.cash.id), amount_minor=100, freq='monthly', start_on='2026-10-13'),
        )
        self.assertEqual([r['error']['code'] for r in results], ['invalid'] * 3)

    def test_a_goal_account_stays_one(self):
        goal = self.make_goal()
        results = self.send(
            self.upsert('accounts', str(goal.account_id), kind='cash'),
            self.upsert('accounts', str(self.bank.id), kind='goal'),
            self.upsert('accounts', name='Sneaky', kind='goal', opening_balance_minor=500),
            # A goal needs an account of kind goal
            self.upsert('savings_goals', account_id=str(self.bank.id), name='x', target_minor=1),
            self.upsert('savings_goals', account_id=str(goal.account_id), name='Twice', target_minor=1),
        )
        self.assertEqual([r['status'] for r in results], ['rejected'] * 5)

    def test_the_server_says_when_a_goal_is_reached(self):
        goal = self.make_goal()
        result = self.send(self.upsert('savings_goals', str(goal.id), status='reached'))[0]
        self.assertEqual(result['error']['code'], 'invalid')

    def test_a_goal_with_money_in_it_is_emptied_before_it_is_deleted(self):
        goal = self.make_goal()
        self.send(*self.legs(self.cash, goal.account, 1000))
        result = self.send(self.delete('savings_goals', str(goal.id)))[0]
        self.assertEqual(result['error']['code'], 'forbidden')
        self.send(*self.legs(goal.account, self.cash, 1000))
        self.assertApplied(self.send(self.delete('savings_goals', str(goal.id)))[0])


class GoalPlanTests(GoalTestCase):
    plan = None  # Free

    def test_free_gets_one_goal_and_archived_ones_do_not_count(self):
        self.assertEqual(defaults.value_for(defaults.FREE, F.SAVINGS_GOALS), 1)
        first = self.make_goal()
        account, second = self.new_goal('Trip')
        results = self.send(account, second)
        # The goal's account is no account of the four Free has: only the goal is refused
        self.assertApplied(results[0])
        self.assertPlanLimit(results[1], F.SAVINGS_GOALS, REASON_LIMIT)

        self.send(self.upsert('savings_goals', str(first.id), status='archived'))
        self.assertApplied(self.send(self.upsert(
            'savings_goals', second['row_id'], account_id=account['row_id'], name='Trip', target_minor=100,
        ))[0])

    def test_goal_accounts_do_not_count_as_accounts(self):
        self.make_goal()
        # Free has four accounts: Cash and Brac Bank are two of them
        results = self.send(*(self.upsert('accounts', name=f'Bank {n}', kind='bank') for n in range(3)))
        self.assertEqual([r['status'] for r in results], ['applied', 'applied', 'rejected'])

    def test_a_locked_goal_takes_no_more_money_but_gives_it_back(self):
        paid_for = set_limit(self.alice, F.SAVINGS_GOALS, None)
        # In force already: the held clock is behind the real time it was made at
        EntitlementOverride.objects.filter(pk=paid_for.pk).update(starts_at=self.clock - timedelta(days=1))
        forget()
        made = [self.make_goal('Laptop'), self.make_goal('Trip')]
        for goal in made:
            self.send(*self.legs(self.cash, goal.account, 5000))
        # Back to Free: the oldest goal is kept
        paid_for.delete()
        forget()
        state = gates.keep_state(self.personal, F.SAVINGS_GOALS)
        kept, locked = sorted(made, key=lambda goal: goal.id not in state.kept)
        self.assertEqual(state.locked, {locked.id})

        into = self.send(*self.legs(self.cash, locked.account, 100))
        self.assertApplied(into[0])  # leaving Cash is fine: the leg into the goal is what is locked
        self.assertPlanLimit(into[1], F.SAVINGS_GOALS, REASON_LOCKED)
        for result in self.send(*self.legs(locked.account, self.cash, 5000)):
            self.assertApplied(result)
        for result in self.send(*self.legs(self.cash, kept.account, 100)):
            self.assertApplied(result)
        # Archiving is a way back under the limit
        self.assertApplied(self.send(self.upsert('savings_goals', str(locked.id), status='archived'))[0])


class GoalNewsTests(GoalTestCase):
    def test_each_quarter_is_told_once_and_reaching_it_pauses_the_schedule(self):
        goal = self.make_goal(target=10000)
        rule = self.rule('transfer', account=self.cash, to_account=goal.account, amount_minor=1000, mode='auto')
        self.send(*self.legs(self.cash, goal.account, 3000))
        goals.evaluate(self.personal.id)
        self.send(*self.legs(self.cash, goal.account, 1000))
        goals.evaluate(self.personal.id)
        goals.evaluate(self.personal.id)
        self.assertEqual(self.told(), [('goal_milestone', 25)])

        # 30% to 100% at once is one piece of news
        self.send(*self.legs(self.cash, goal.account, 6000))
        goals.evaluate(self.personal.id)
        self.assertEqual(self.told(), [('goal_milestone', 25), ('goal_reached', 100)])
        goal.refresh_from_db()
        rule.refresh_from_db()
        self.assertEqual((goal.status, goal.milestones_hit, rule.is_paused), ('reached', 0b1111, True))

        # A higher target: the goal is active again, and what it has not earned is told again later
        self.assertApplied(self.send(self.upsert('savings_goals', str(goal.id), target_minor=40000))[0])
        goal.refresh_from_db()
        self.assertEqual((goal.status, goal.milestones_hit), ('active', 0b0001))

    def test_falling_behind_is_said_once_a_week_and_idling_once_a_month(self):
        goal = self.make_goal(target=120000, target_on='2027-10-13')
        self.on('2027-01-13')  # a quarter of the way through the year, nothing saved
        goals.daily()
        goals.daily()
        self.on('2027-01-20')
        goals.daily()
        behind = InboxItem.objects.filter(type='goal_behind')
        self.assertEqual(behind.count(), 2)
        needed = goal_math.progress(goal, date(2027, 1, 20)).needed_per_month_minor
        self.assertEqual(behind.first().params['needed_minor'], needed)

        # Back on pace: nothing about pace, but nothing put in for a month is said
        self.send(*self.legs(self.cash, goal.account, 40000, day='2026-11-01'))
        self.on('2027-01-27')
        goals.daily()
        self.assertEqual(InboxItem.objects.filter(type='goal_behind').count(), 2)
        self.assertEqual(InboxItem.objects.filter(type='goal_idle').count(), 1)

    def test_the_push_says_what_the_inbox_says(self):
        params = {'title': 'Laptop', 'mark': 50, 'percent': 52, 'amount_minor': 5000000, 'saved_minor': 2600000,
                  'currency': 'BDT', 'needed_minor': 280000, 'days': 31}
        self.assertEqual(words.wording('goal_milestone', params, 'en')[0], '50% of the way to Laptop')
        self.assertEqual(words.wording('goal_reached', params, 'en')[0], 'You reached Laptop!')
        self.assertIn('a month', words.wording('goal_behind', params, 'en')[1])
        self.assertIn('৩১', words.wording('goal_idle', params, 'bn')[0])


class GoalMathTests(GoalTestCase):
    """The same cases as the app's test/goal_math_test.dart."""

    def test_pace(self):
        start, end = date(2026, 1, 1), date(2027, 1, 1)
        self.assertEqual(goal_math.expected_by(36500, start, end, date(2026, 7, 2)), 18200)
        self.assertEqual(goal_math.expected_by(36500, start, end, date(2028, 1, 1)), 36500)
        self.assertEqual(goal_math.pace_of(100, 100, None), goal_math.REACHED)
        self.assertEqual(goal_math.pace_of(10, 100, None), goal_math.NO_DATE)
        self.assertEqual(goal_math.pace_of(89, 100, 100), goal_math.BEHIND)
        self.assertEqual(goal_math.pace_of(90, 100, 100), goal_math.ON_TRACK)
        self.assertEqual(goal_math.pace_of(111, 200, 100), goal_math.AHEAD)
        self.assertEqual(goal_math.milestones_passed(74), 0b0011)
        self.assertEqual(goal_math.milestones_passed(130), 0b1111)

    def test_what_each_month_needs(self):
        # 365 days are 11.99 average months: a little over 10,000 each, rounded up
        goal = SimpleNamespace(
            account=None, target_minor=120000, target_on=date(2027, 10, 13), created_at=None,
        )
        progress = goal_math.progress(goal, date(2026, 10, 13), saved_minor=0)
        self.assertEqual(progress.needed_per_month_minor, 10007)
