from datetime import date
import uuid

from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.test import APIClient

from billing.catalog.keys import F
from billing.models import BillingSettings, FunnelEvent
from billing.testing import grant_plan, set_limit, set_mode
from billing.tests.base import BillingTestCase
from workspaces.models import Membership
from workspaces.tests import UnprivilegedRoleMixin
from reminders.inbox import today_in
from reports.periods import LAST_3_MONTHS, THIS_YEAR, resolve
from . import trends
from .models import Account, Category, Transaction


class AccountTestCase(BillingTestCase):
    """
    Alice's Cash account, opened with 1,000.00, and a bank account beside it.
    seed() gives the two months the app's own tests use (test/account_ledger_test.dart),
    so the same figures are expected on both sides.
    """

    # Plan limits are not what is tested here
    plan = 'business'

    def setUp(self):
        super().setUp()
        if self.plan:
            grant_plan(self.alice, self.plan)
        self.cash = Account.objects.get(workspace=self.personal)
        self.cash.opening_balance_minor = 100000
        self.cash.save()
        self.bank = Account.objects.create(workspace=self.personal, name='Brac Bank', kind='bank')
        self.food = Category.objects.get(workspace=self.personal, name='Food')
        self.salary = Category.objects.get(workspace=self.personal, name='Salary')
        self.url = f'/api/v1/ledger/accounts/{self.cash.id}/'

    def add(self, kind, amount, day, account=None, category=None, note='', group=None):
        signed = -amount if kind == 'expense' else amount
        return Transaction.objects.create(
            account=account or self.cash, kind=kind, amount_minor=signed, occurred_on=day,
            category=category, note=note, transfer_group_id=group,
        )

    def transfer(self, source, target, amount, day, note=''):
        group = uuid.uuid4()
        self.add('transfer', -amount, day, account=source, note=note, group=group)
        self.add('transfer', amount, day, account=target, note=note, group=group)

    def split(self, kind, day, parts, note='', account=None):
        """One transaction filed under several categories: `parts` is (category or None, amount)."""
        amount = sum(part for _, part in parts)
        return Transaction.objects.create(
            account=account or self.cash, kind=kind, amount_minor=-amount if kind == 'expense' else amount,
            occurred_on=day, note=note,
            splits=[{'category_id': category and str(category.id), 'amount_minor': part, 'note': ''}
                    for category, part in parts],
        )

    def seed(self):
        self.add('income', 5000000, '2026-09-01', category=self.salary, note='September pay')
        self.add('expense', 120000, '2026-09-10', category=self.food, note='Groceries')
        self.transfer(self.cash, self.bank, 2000000, '2026-09-15')
        self.add('expense', 45000, '2026-10-02', category=self.food, note='Lunch 50% off')
        self.add('expense', 30000, '2026-10-02', note='Rickshaw')
        self.transfer(self.bank, self.cash, 500000, '2026-10-05', note='ATM')

    def rows(self, client=None, **params):
        response = (client or self.alice_client).get(f'{self.url}transactions/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def notes(self, **params):
        return [row['note'] or row['kind'] for row in self.rows(**params)['results']]

    def summary(self, **params):
        response = self.alice_client.get(f'{self.url}summary/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data


class TransactionPageTests(AccountTestCase):
    def test_pages_walk_every_row_once_even_when_rows_share_a_day_and_a_moment(self):
        for position in range(23):
            self.add('expense', 100 + position, '2026-10-02' if position < 12 else '2026-10-01')
        # One timestamp for all: only the id tells them apart
        Transaction.objects.filter(account=self.cash).update(created_at=timezone.now())

        seen, cursor, pages = [], None, 0
        while True:
            data = self.rows(limit=5, **({'cursor': cursor} if cursor else {}))
            seen += [(row['occurred_on'], row['id']) for row in data['results']]
            pages += 1
            if not data['has_more']:
                self.assertIsNone(data['next_cursor'])
                break
            self.assertEqual(len(data['results']), 5)
            cursor = data['next_cursor']
        self.assertEqual((len(seen), len(set(seen)), pages), (23, 23, 5))
        self.assertEqual(seen, sorted(seen, key=lambda key: (key[0], uuid.UUID(key[1])), reverse=True))

    def test_a_row_added_meanwhile_does_not_shift_the_next_page(self):
        for day in range(1, 7):
            self.add('expense', 100, f'2026-10-{day:02d}', note=f'day {day}')
        first = self.rows(limit=3)
        self.add('expense', 100, '2026-10-20', note='new')
        second = self.rows(limit=3, cursor=first['next_cursor'])
        self.assertEqual([row['note'] for row in second['results']], ['day 3', 'day 2', 'day 1'])

    def test_only_this_account_without_what_was_deleted_and_a_transfer_names_the_other_account(self):
        self.seed()
        self.add('expense', 999, '2026-10-03', account=self.bank)
        rows = self.rows()['results']
        self.assertEqual(len(rows), 6)
        self.assertEqual({row['account_id'] for row in rows}, {str(self.cash.id)})
        transfers = [(row['amount_minor'], row['other_account_name'], row['other_account_id'])
                     for row in rows if row['kind'] == 'transfer']
        self.assertEqual(transfers, [(500000, 'Brac Bank', str(self.bank.id)), (-2000000, 'Brac Bank', str(self.bank.id))])
        groceries = next(row for row in rows if row['note'] == 'Groceries')
        self.assertEqual((groceries['category_name'], groceries['category_color'], groceries['currency']),
                         ('Food', self.food.color, 'BDT'))

        Transaction.objects.get(note='Rickshaw').soft_delete()
        self.assertEqual(len(self.rows()['results']), 5)

    def test_filters(self):
        self.seed()
        october = {'from': '2026-10-01', 'to': '2026-10-31'}
        self.assertEqual(self.notes(**october), ['ATM', 'Rickshaw', 'Lunch 50% off'])
        self.assertEqual(self.notes(**{'from': '2026-09-10', 'to': '2026-10-02'}),
                         ['Rickshaw', 'Lunch 50% off', 'transfer', 'Groceries'])
        self.assertEqual(self.notes(to='2026-09-01'), ['September pay'])

        self.assertEqual(self.notes(kind='transfer'), ['ATM', 'transfer'])
        self.assertEqual(self.notes(kind='income'), ['September pay'])
        self.assertEqual(self.notes(category=str(self.food.id)), ['Lunch 50% off', 'Groceries'])
        # Transfers have no category, but they are not "uncategorised"
        self.assertEqual(self.notes(category='none'), ['Rickshaw'])
        self.assertEqual(self.notes(category=f'{self.salary.id},none'), ['Rickshaw', 'September pay'])
        self.assertEqual(self.notes(category=[str(self.salary.id), 'none']), ['Rickshaw', 'September pay'])
        self.assertEqual(self.notes(category=str(self.food.id), **october), ['Lunch 50% off'])

        self.assertEqual(self.notes(q='groc'), ['Groceries'])
        self.assertEqual(self.notes(q='food'), ['Lunch 50% off', 'Groceries'])  # by category
        self.assertEqual(self.notes(q='brac'), ['ATM', 'transfer'])  # by the other account
        self.assertEqual(self.notes(q='1,200'), ['Groceries'])  # by amount
        self.assertEqual(self.notes(q='১২০০'), ['Groceries'])  # typed in Bangla
        self.assertEqual(self.notes(q='50%'), ['Lunch 50% off'])  # % is not a wildcard
        self.assertEqual(self.notes(q='nothing here'), [])

    def test_bad_input_is_refused(self):
        for params in ({'from': 'yesterday'}, {'from': '2026-10-02', 'to': '2026-10-01'}, {'kind': 'gift'},
                       {'category': 'food'}, {'cursor': 'abc'}, {'limit': 'ten'}, {'limit': 0}):
            response = self.alice_client.get(f'{self.url}transactions/', params)
            self.assertEqual(response.status_code, 400, params)
            self.assertIn('detail', response.data)
        self.assertEqual(self.alice_client.get(f'{self.url}summary/', {'include': 'everything'}).status_code, 400)

    def test_limit_is_capped(self):
        for _ in range(3):
            self.add('expense', 100, '2026-10-01')
        self.assertEqual(len(self.rows(limit=100000)['results']), 3)


class SummaryTests(AccountTestCase):
    def test_the_summary_adds_up_and_opening_plus_in_minus_out_is_closing(self):
        self.seed()
        total = self.summary()
        self.assertEqual((total['income_minor'], total['expense_minor']), (5000000, 195000))
        self.assertEqual((total['transfer_in_minor'], total['transfer_out_minor']), (500000, 2000000))
        self.assertEqual((total['cash_in_minor'], total['cash_out_minor'], total['count']), (5500000, 2195000, 6))
        self.assertEqual((total['period_opening_minor'], total['period_closing_minor']), (100000, 3405000))
        self.assertEqual(total['period_opening_minor'] + total['net_minor'], total['period_closing_minor'])
        self.assertEqual((total['largest_expense_minor'], total['first_day'], total['last_day']),
                         (120000, '2026-09-01', '2026-10-05'))
        self.assertEqual((total['balance_minor'], total['opening_balance_minor'], total['currency'], total['is_locked']),
                         (3405000, 100000, 'BDT', False))
        self.assertNotIn('series', total)

        october = {'from': '2026-10-01', 'to': '2026-10-31'}
        month = self.summary(**october)
        self.assertEqual((month['cash_in_minor'], month['cash_out_minor']), (500000, 75000))
        self.assertEqual((month['period_opening_minor'], month['period_closing_minor']), (2980000, 3405000))

        # Narrowing changes what is counted, never the balances around the period
        food = self.summary(category=str(self.food.id), **october)
        self.assertEqual((food['expense_minor'], food['cash_in_minor'], food['count']), (45000, 0, 1))
        self.assertEqual((food['period_opening_minor'], food['period_closing_minor'], food['balance_minor']),
                         (2980000, 3405000, 3405000))
        searched = self.summary(q='brac')
        self.assertEqual((searched['transfer_in_minor'], searched['transfer_out_minor'], searched['count']),
                         (500000, 2000000, 2))

        empty = self.summary(q='zzz')
        self.assertEqual((empty['count'], empty['cash_in_minor'], empty['first_day']), (0, 0, None))

    def test_categories(self):
        self.seed()
        data = self.summary(include='categories')
        self.assertEqual(
            [(row['name'], row['amount_minor'], row['count'], row['largest_minor']) for row in data['expense_categories']],
            [('Food', 165000, 2, 120000), (None, 30000, 1, 30000)],
        )
        self.assertEqual(data['expense_categories'][0]['category_id'], str(self.food.id))
        self.assertEqual([(row['name'], row['amount_minor']) for row in data['income_categories']], [('Salary', 5000000)])

        # A deleted category reads as none
        self.food.soft_delete()
        data = self.summary(include='categories')
        self.assertEqual([(row['name'], row['amount_minor']) for row in data['expense_categories']], [(None, 195000)])

    def test_the_series_runs_by_day_then_by_month_once_the_span_is_long(self):
        self.seed()
        series = self.summary(include='series')['series']
        self.assertEqual((series['monthly'], series['opening_minor']), (False, 100000))
        self.assertEqual(
            [(p['date'], p['in_minor'], p['out_minor'], p['balance_minor']) for p in series['points']],
            [('2026-09-01', 5000000, 0, 5100000), ('2026-09-10', 0, 120000, 4980000),
             ('2026-09-15', 0, 2000000, 2980000), ('2026-10-02', 0, 75000, 2905000),
             ('2026-10-05', 500000, 0, 3405000)],
        )

        # In and out follow the filter; the balance still counts everything
        food = self.summary(include='series', category=str(self.food.id))['series']
        self.assertEqual([(p['in_minor'], p['out_minor']) for p in food['points']],
                         [(0, 0), (0, 120000), (0, 0), (0, 45000), (0, 0)])
        self.assertEqual(food['points'][-1]['balance_minor'], 3405000)

        self.add('income', 1000, '2026-01-20')
        series = self.summary(include='series')['series']
        self.assertTrue(series['monthly'])
        self.assertEqual(
            [(p['date'], p['in_minor'], p['out_minor'], p['balance_minor']) for p in series['points']],
            [('2026-01-01', 1000, 0, 101000), ('2026-09-01', 5000000, 2120000, 2981000),
             ('2026-10-01', 500000, 75000, 3406000)],
        )
        self.assertEqual(self.summary(include='series', **{'from': '2026-10-01', 'to': '2026-10-31'})['series']['opening_minor'],
                         2981000)


class SplitTests(AccountTestCase):
    """Cash taken out and spent on several things: one transaction, each part under its own category."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.transport = Category.objects.get(workspace=self.personal, name='Transport')
        self.atm = self.split('expense', '2026-10-06', [(self.food, 20000), (self.transport, 10000), (None, 20000)],
                              note='ATM cash')
        self.october = {'from': '2026-10-01', 'to': '2026-10-31'}

    def test_the_parts_must_add_up_and_a_transfer_cannot_be_split(self):
        part = {'category_id': str(self.food.id), 'amount_minor': 100, 'note': ''}
        cases = [
            ('expense', -300, [part, part]),            # adds up to 200
            ('expense', -100, [part]),                  # one part is just a category
            ('expense', -200, [part, {**part, 'amount_minor': 0}, part]),
            ('expense', -200, [part, {**part, 'category_id': 'food'}]),
            ('transfer', -200, [part, part]),
        ]
        for kind, amount, splits in cases:
            row = Transaction(
                account=self.cash, workspace=self.personal, currency='BDT', kind=kind, amount_minor=amount,
                occurred_on='2026-10-07', splits=splits, transfer_group_id=uuid.uuid4() if kind == 'transfer' else None,
            )
            with self.assertRaises(ValidationError, msg=(kind, amount, splits)):
                row.full_clean()

    def test_a_split_has_no_category_of_its_own(self):
        self.atm.category = self.food
        self.atm.save()
        self.atm.refresh_from_db()
        self.assertIsNone(self.atm.category_id)
        row = next(row for row in self.rows()['results'] if row['note'] == 'ATM cash')
        self.assertEqual([part['amount_minor'] for part in row['splits']], [20000, 10000, 20000])

    def test_each_part_counts_under_its_own_category(self):
        data = self.summary(include='categories', **self.october)
        self.assertEqual(
            [(row['name'], row['amount_minor'], row['count'], row['largest_minor']) for row in data['expense_categories']],
            [('Food', 65000, 2, 45000), (None, 50000, 2, 30000), ('Transport', 10000, 1, 10000)],
        )
        # Whatever the split, the whole amount left the account
        self.assertEqual((data['expense_minor'], data['count']), (125000, 4))
        self.assertEqual(sum(row['amount_minor'] for row in data['expense_categories']), data['expense_minor'])

    def test_a_choice_of_categories_counts_only_the_chosen_parts(self):
        food = self.summary(category=str(self.food.id), include='categories,series', **self.october)
        self.assertEqual((food['expense_minor'], food['count'], food['largest_expense_minor']), (65000, 2, 45000))
        self.assertEqual([(row['name'], row['amount_minor']) for row in food['expense_categories']], [('Food', 65000)])
        self.assertEqual([(p['date'], p['out_minor']) for p in food['series']['points']],
                         [('2026-10-02', 45000), ('2026-10-05', 0), ('2026-10-06', 20000)])
        # The balance still counts all of it
        self.assertEqual(food['series']['points'][-1]['balance_minor'], 3355000)

        none = self.summary(category='none', **self.october)
        self.assertEqual((none['expense_minor'], none['count']), (50000, 2))
        both = self.summary(category=f'{self.transport.id},none', **self.october)
        self.assertEqual(both['expense_minor'], 60000)
        self.assertEqual(self.notes(category=str(self.transport.id)), ['ATM cash'])

    def test_a_part_under_a_deleted_category_reads_as_none(self):
        self.transport.soft_delete()
        data = self.summary(include='categories', **self.october)
        self.assertEqual([(row['name'], row['amount_minor']) for row in data['expense_categories']],
                         [('Food', 65000), (None, 60000)])


class AccessTests(UnprivilegedRoleMixin, AccountTestCase):
    def assertHidden(self, client):
        for path in ('transactions/', 'summary/'):
            response = client.get(f'{self.url}{path}')
            self.assertEqual(response.status_code, 404, path)
            self.assertEqual(response.data, {'detail': 'Account not found.'})

    def test_another_users_account_is_not_found(self):
        self.seed()
        self.assertHidden(self.bob_client)
        self.assertEqual(self.alice_client.get(f'/api/v1/ledger/accounts/{uuid.uuid4()}/summary/').status_code, 404)

    def test_a_member_who_does_not_run_the_workspace_cannot_read_it(self):
        Membership.objects.create(workspace=self.personal, user=self.bob, role=Membership.ROLE_VIEWER)
        self.assertHidden(self.bob_client)

    def test_a_deleted_account_is_not_found(self):
        self.cash.soft_delete()
        self.assertHidden(self.alice_client)

    def test_signed_out_is_refused(self):
        self.assertIn(APIClient().get(f'{self.url}summary/').status_code, (401, 403))

    def test_row_level_security_lets_the_owner_read_and_nobody_else(self):
        self.seed()
        self.use_unprivileged_role()
        self.assertEqual(len(self.rows()['results']), 6)
        self.assertEqual(self.summary(include='categories,series')['balance_minor'], 3405000)
        self.assertHidden(self.bob_client)


class LockedAccountTests(AccountTestCase):
    """On Free, with more accounts than the four it allows."""
    plan = None

    def test_a_locked_account_can_still_be_read(self):
        accounts = [self.cash, self.bank] + [
            Account.objects.create(workspace=self.personal, name=f'Wallet {n}') for n in range(3)
        ]
        self.age(accounts)
        newest = accounts[-1]
        Transaction.objects.create(account=newest, kind='expense', amount_minor=-500, occurred_on='2026-10-01')
        url = f'/api/v1/ledger/accounts/{newest.id}/'
        summary = self.alice_client.get(f'{url}summary/')
        self.assertEqual((summary.status_code, summary.data['is_locked'], summary.data['balance_minor']), (200, True, -500))
        self.assertEqual(len(self.alice_client.get(f'{url}transactions/').data['results']), 1)
        self.assertFalse(self.summary()['is_locked'])


class CustomRangeTests(AccountTestCase):
    """On Free, chosen dates are refused and the presets the app offers are not."""
    plan = None

    def get(self, path, **params):
        return self.alice_client.get(f'{self.url}{path}', params)

    def test_presets_are_free(self):
        today = today_in(self.personal)
        presets = [{}, {'from': '2025-02-01', 'to': '2025-02-28'}, {'from': '2026-09-01', 'to': '2026-09-30'}]
        for name in (LAST_3_MONTHS, THIS_YEAR):
            period = resolve({'period': name}, today)
            presets.append({'from': period.date_from.isoformat(), 'to': period.date_to.isoformat()})
        for path in ('summary/', 'transactions/'):
            for params in presets:
                self.assertEqual(self.get(path, **params).status_code, 200, (path, params))

    def test_chosen_dates_are_a_plan_feature(self):
        chosen = [
            {'from': '2026-09-10', 'to': '2026-10-02'},
            {'from': '2026-09-01', 'to': '2026-10-31'},
            {'from': '2026-09-01'},
            {'to': '2026-09-30'},
        ]
        for path in ('summary/', 'transactions/'):
            for params in chosen:
                response = self.get(path, **params)
                self.assertEqual(
                    (response.status_code, response.data['feature']), (402, 'insights.custom_range'), (path, params),
                )
        grant_plan(self.alice, 'plus')
        for path in ('summary/', 'transactions/'):
            self.assertEqual(self.get(path, **chosen[0]).status_code, 200, path)

    def test_a_stranger_learns_nothing_about_the_plan(self):
        other = Account.objects.create(workspace=self.personal, name='Other')
        response = self.bob_client.get(f'/api/v1/ledger/accounts/{other.id}/summary/', {'from': '2026-09-10', 'to': '2026-10-02'})
        self.assertEqual(response.status_code, 404)


class TrendsTests(UnprivilegedRoleMixin, AccountTestCase):
    """
    The workspace's cash-flow trend. seed() gives the figures the app's own
    tests use (test/repository_test.dart), so both sides expect the same.
    """
    plan = None

    def setUp(self):
        super().setUp()
        self.trends_url = f'/api/v1/ledger/workspaces/{self.personal.id}/trends/'

    def get(self, client=None, **params):
        return (client or self.alice_client).get(self.trends_url, params)

    def trend(self, **params):
        response = self.get(**params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_each_month_is_income_beside_expense_oldest_first_with_empty_months_kept(self):
        self.seed()
        data = self.trend(months=3, to='2026-10')
        self.assertEqual(
            (data['currency'], data['months'], data['date_from'], data['date_to']),
            ('BDT', 3, '2026-08-01', '2026-10-31'),
        )
        # Transfers are the user's own money moving, neither income nor expense
        self.assertEqual(
            [(p['month'], p['income_minor'], p['expense_minor']) for p in data['points']],
            [('2026-08-01', 0, 0), ('2026-09-01', 5000000, 120000), ('2026-10-01', 0, 75000)],
        )

    def test_every_account_counts_in_its_currency_and_deleted_rows_do_not(self):
        self.seed()
        self.add('income', 7000, '2026-10-20', account=self.bank)
        dollars = Account.objects.create(workspace=self.personal, name='Payoneer', kind='bank', currency='USD')
        self.add('income', 900, '2026-10-21', account=dollars)
        self.add('expense', 333, '2026-10-22').soft_delete()

        october = self.trend(months=1, to='2026-10')['points']
        self.assertEqual([(p['income_minor'], p['expense_minor']) for p in october], [(7000, 75000)])
        usd = self.trend(months=1, to='2026-10', currency='usd')
        self.assertEqual((usd['currency'], usd['points'][0]['income_minor']), ('USD', 900))

    def test_the_defaults_are_six_months_to_this_month_in_the_workspace_currency(self):
        data = self.trend()
        this_month = today_in(self.personal).replace(day=1)
        self.assertEqual((data['months'], data['currency'], len(data['points'])), (6, 'BDT', 6))
        self.assertEqual(data['points'][-1]['month'], this_month.isoformat())

    def test_bad_parameters_are_refused(self):
        for params in ({'months': 'six'}, {'months': 0}, {'months': trends.MAX_MONTHS + 1},
                       {'to': '2026-13'}, {'to': '2026-10-01'}, {'currency': 'TAKA'}):
            self.assertEqual(self.get(**params).status_code, 400, params)

    def test_more_months_than_the_plan_allows_is_refused_with_the_limit(self):
        self.assertEqual(self.get(months=6).status_code, 200)
        response = self.get(months=12)
        self.assertEqual(response.status_code, 402)
        self.assertEqual(
            {key: response.data[key] for key in ('code', 'feature', 'reason', 'limit', 'current', 'plan', 'upgrade_to')},
            {'code': 'plan_limit', 'feature': 'insights.trend_months', 'reason': 'limit', 'limit': 6,
             'current': 12, 'plan': 'free', 'upgrade_to': 'plus'},
        )

        grant_plan(self.alice, 'plus')
        self.assertEqual(len(self.trend(months=24)['points']), 24)
        self.assertEqual(self.get(months=25).status_code, 402)

    def test_an_override_moves_the_limit(self):
        set_limit(self.alice, F.INSIGHTS_TREND_MONTHS, 12)
        self.assertEqual(self.get(months=12).status_code, 200)
        set_limit(self.alice, F.INSIGHTS_TREND_MONTHS, None)
        self.assertEqual(self.get(months=trends.MAX_MONTHS).status_code, 200)

    def test_log_only_lets_it_through_and_notes_the_hit(self):
        set_mode(BillingSettings.MODE_LOG_ONLY)
        self.assertEqual(self.get(months=24).status_code, 200)
        hit = FunnelEvent.objects.get(kind=FunnelEvent.KIND_LIMIT_HIT)
        self.assertEqual((hit.feature_key, hit.enforced), ('insights.trend_months', False))

    def test_a_stranger_learns_nothing_not_even_the_plan(self):
        for params in ({}, {'months': 24}):
            response = self.get(client=self.bob_client, **params)
            self.assertEqual((response.status_code, response.data), (404, {'detail': 'Workspace not found.'}))
        self.assertEqual(self.alice_client.get(f'/api/v1/ledger/workspaces/{uuid.uuid4()}/trends/').status_code, 404)
        Membership.objects.create(workspace=self.personal, user=self.bob, role=Membership.ROLE_VIEWER)
        self.assertEqual(self.get(client=self.bob_client).status_code, 404)
        self.assertIn(APIClient().get(self.trends_url).status_code, (401, 403))

    def test_row_level_security_lets_the_owner_read(self):
        self.seed()
        self.use_unprivileged_role()
        self.assertEqual(self.trend(months=2, to='2026-10')['points'][0]['income_minor'], 5000000)


class TrendMathTests(AccountTestCase):
    """What Year in review builds on: any span, across the turn of a year."""

    def test_monthly_totals_cross_a_year(self):
        self.add('income', 100, '2025-11-30')
        self.add('expense', 40, '2025-12-01')
        self.add('expense', 60, '2026-01-31')
        totals = trends.monthly_totals(self.personal, 'BDT', date(2025, 11, 15), date(2026, 2, 3))
        self.assertEqual(
            [(t['month'], t['income_minor'], t['expense_minor']) for t in totals],
            [(date(2025, 11, 1), 100, 0), (date(2025, 12, 1), 0, 40), (date(2026, 1, 1), 0, 60),
             (date(2026, 2, 1), 0, 0)],
        )

    def test_windows_and_trimming(self):
        self.assertEqual(trends.month_window(date(2026, 2, 14), 3), (date(2025, 12, 1), date(2026, 2, 28)))
        self.assertEqual(trends.month_window(date(2024, 2, 1), 1), (date(2024, 2, 1), date(2024, 2, 29)))
        points = [{'date': date(2025, 1, 1)}, {'date': date(2025, 6, 1)}, {'date': date(2025, 9, 1)}]
        self.assertEqual(trends.trim_months(points, date(2025, 9, 1), None), (points, 0))
        self.assertEqual(trends.trim_months(points, date(2025, 9, 1), 12), (points, 0))
        # Kept: April to September. January to March are hidden
        self.assertEqual(trends.trim_months(points, date(2025, 9, 1), 6), (points[1:], 3))


class SeriesCapTests(AccountTestCase):
    """A month-by-month series keeps only the months the plan charts; all time stays free."""
    plan = None

    def seed_months(self):
        # Ten months: 100 in each, from January to October 2026
        for month in range(1, 11):
            self.add('income', 100, f'2026-{month:02d}-05')

    def test_free_keeps_the_latest_six_months_and_says_how_many_went(self):
        self.seed_months()
        series = self.summary(include='series')['series']
        self.assertEqual(series['hidden_months'], 4)
        self.assertEqual([p['date'] for p in series['points']],
                         ['2026-05-01', '2026-06-01', '2026-07-01', '2026-08-01', '2026-09-01', '2026-10-01'])
        # The balance still counts what is not shown
        self.assertEqual(series['opening_minor'], 100000 + 400)
        self.assertEqual(series['points'][-1]['balance_minor'], 100000 + 1000)

    def test_a_plan_with_room_keeps_everything(self):
        self.seed_months()
        grant_plan(self.alice, 'plus')
        series = self.summary(include='series')['series']
        self.assertEqual((series['hidden_months'], len(series['points']), series['opening_minor']), (0, 10, 100000))

    def test_not_enforcing_trims_nothing(self):
        self.seed_months()
        set_mode(BillingSettings.MODE_OFF)
        self.assertEqual(self.summary(include='series')['series']['hidden_months'], 0)

    def test_a_day_by_day_series_is_never_trimmed(self):
        self.seed()
        series = self.summary(include='series')['series']
        self.assertEqual((series['monthly'], series['hidden_months'], len(series['points'])), (False, 0, 5))


class SchemaTests(BillingTestCase):
    def test_the_endpoints_are_in_the_openapi_schema(self):
        paths = self.alice_client.get('/api/v1/schema/').data['paths']
        for path in ('transactions/', 'summary/'):
            self.assertIn(f'/api/v1/ledger/accounts/{{account_id}}/{path}', paths)
        self.assertIn('/api/v1/ledger/workspaces/{workspace_id}/trends/', paths)
