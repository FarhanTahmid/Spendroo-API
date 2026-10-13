from procrastinate.contrib.django.models import ProcrastinateJob

from billing.models import Subscription
from billing.testing import grant_plan
from ledger_personal.models import Budget
from reminders import budgets
from reminders.models import InboxItem, Rule
from reminders.tasks import evaluate_budgets

from .base import ReminderTestCase


class BudgetAlertTests(ReminderTestCase):
    """Food has 1,000.00 a month. Alice's day is 13 October 2026."""
    plan = None  # budget alerts are for every plan

    def setUp(self):
        super().setUp()
        self.budget = Budget.objects.create(workspace=self.personal, category=self.food, amount_minor=100000)

    def look(self):
        budgets.evaluate(self.personal.id)
        return [(item.type, item.params['percent']) for item in InboxItem.objects.order_by('created_at', 'id')]

    def test_said_once_at_eighty_percent_and_once_when_it_is_gone(self):
        self.add('expense', 79900, '2026-10-02', category=self.food)
        self.assertEqual(self.look(), [])
        self.add('expense', 100, '2026-10-03', category=self.food)
        self.add('expense', 5000, '2026-10-04', category=self.food)
        self.assertEqual(self.look(), [('budget_80', 85)])
        self.assertEqual(self.look(), [('budget_80', 85)])
        warning = InboxItem.objects.get()
        self.assertEqual(
            (warning.params['category'], warning.params['amount_minor'], warning.params['spent_minor'],
             warning.params['month'], warning.subject_table, warning.subject_id),
            ('Food', 100000, 85000, '2026-10', 'budgets', self.budget.id),
        )
        self.assertIsNotNone(warning.resolved_at)

        # Exactly spent is not over; one more taka is
        self.add('expense', 15000, '2026-10-05', category=self.food)
        self.assertEqual(self.look(), [('budget_80', 85)])
        self.add('expense', 100, '2026-10-06', category=self.food)
        self.assertEqual(self.look(), [('budget_80', 85), ('budget_over', 100)])

        # A new month starts clean, and says it again when the time comes
        self.clock = self.at('2026-11-02')
        self.add('expense', 120000, '2026-11-01', category=self.food)
        self.assertEqual(self.look()[-1], ('budget_over', 120))
        self.assertEqual(InboxItem.objects.count(), 3)

    def test_other_months_other_kinds_and_other_categories_do_not_count(self):
        self.add('expense', 99000, '2026-09-30', category=self.food)
        self.add('expense', 99000, '2026-10-02', category=self.transport)
        self.add('expense', 99000, '2026-10-02')
        self.add('income', 99000, '2026-10-02', category=self.salary)
        self.assertEqual(self.look(), [])

    def test_a_split_spends_from_the_budget_of_each_part(self):
        self.split('expense', '2026-10-06', [(self.food, 90000), (self.transport, 500000), (None, 10000)])
        self.assertEqual(self.look(), [('budget_80', 90)])

    def test_a_one_month_override_takes_the_place_of_the_usual_limit(self):
        Budget.objects.create(workspace=self.personal, category=self.food, amount_minor=200000, month='2026-10-01')
        self.add('expense', 150000, '2026-10-02', category=self.food)
        self.assertEqual(self.look(), [])
        self.add('expense', 20000, '2026-10-03', category=self.food)
        self.assertEqual(self.look(), [('budget_80', 85)])
        self.assertEqual(InboxItem.objects.get().params['amount_minor'], 200000)

    def test_a_push_that_touches_money_has_the_budgets_looked_at(self):
        spend = self.upsert('transactions', account_id=str(self.cash.id), category_id=str(self.food.id), kind='expense',
                            amount_minor=-95000, occurred_on='2026-10-13')
        # Twice in a row: the second finds the look already on its way
        self.push(self.alice_client, self.personal, spend)
        self.push(self.alice_client, self.personal, self.upsert('transactions', spend['row_id'], note='Bazar'))
        self.push(self.alice_client, self.personal, self.upsert('categories', str(self.food.id), color='#3B9EE5'))
        waiting = ProcrastinateJob.objects.filter(task_name='reminders.tasks.evaluate_budgets')
        self.assertEqual([job.args for job in waiting], [{'workspace_id': str(self.personal.id)}])
        evaluate_budgets(workspace_id=str(self.personal.id))
        self.assertEqual(InboxItem.objects.get().type, 'budget_80')

    def test_what_an_auto_rule_records_counts_at_once(self):
        grant_plan(self.alice, 'plus')
        Subscription.objects.update(starts_at=self.clock)
        self.rule(title='Groceries box', category=self.food, amount_minor=90000, mode=Rule.MODE_AUTO, freq='daily')
        self.on('2026-10-13')
        self.assertEqual(sorted(item.type for item in InboxItem.objects.all()), ['budget_80', 'posted'])
