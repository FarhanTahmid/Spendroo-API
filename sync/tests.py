import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from billing.testing import grant_plan
from cashbook.models import CashBook, CashBookAdditionalMember, Entry, EntryCategory
from ledger_personal.models import Account, Budget, Category, Transaction
from workspaces.models import Membership
from workspaces.services import DEMO_ENTRIES, create_workspace, get_personal_workspace
from workspaces.tests import UnprivilegedRoleMixin
from .registry import TABLES

User = get_user_model()

PULL = '/api/v1/sync/pull/'
PUSH = '/api/v1/sync/push/'


def new_id():
    return str(uuid.uuid4())


class SyncTestCase(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(email='alice@example.com', password='pass-12345')
        self.bob = User.objects.create_user(email='bob@example.com', password='pass-12345')
        self.alice_client = APIClient()
        self.alice_client.force_authenticate(self.alice)
        self.bob_client = APIClient()
        self.bob_client.force_authenticate(self.bob)
        self.personal = get_personal_workspace(self.alice)
        self.shop = create_workspace(owner=self.alice, name='Shop')
        # These tests are about how sync behaves, not about what a plan
        # allows (billing/tests cover that), so the owner gets everything
        grant_plan(self.alice, 'business')

    def push(self, client, workspace, *mutations):
        response = client.post(PUSH, {'workspace': str(workspace.id), 'mutations': list(mutations)}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['results']

    def pull(self, client, workspace, since=0, **params):
        response = client.get(PULL, {'workspace': str(workspace.id), 'since': since, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def upsert(self, table, row_id=None, **data):
        return {'id': new_id(), 'table': table, 'op': 'upsert', 'row_id': row_id or new_id(), 'data': data}

    def delete(self, table, row_id):
        return {'id': new_id(), 'table': table, 'op': 'delete', 'row_id': row_id}

    def push_book_with_entry(self, client=None, workspace=None):
        """Returns (book mutation, entry mutation, results)"""
        book = self.upsert('cashbooks', book_name='Till', currency='BDT')
        entry = self.upsert(
            'entries', cashbook_id=book['row_id'], entry_type='cash_in', amount_minor=5000,
            entry_date='2026-10-01', title='Sale',
        )
        results = self.push(client or self.alice_client, workspace or self.shop, book, entry)
        return book, entry, results


class PushTests(SyncTestCase):
    def test_create_with_client_ids(self):
        book, entry, results = self.push_book_with_entry()
        self.assertEqual([r['status'] for r in results], ['applied', 'applied'])
        saved = Entry.objects.get(id=entry['row_id'])
        self.assertEqual((saved.amount_minor, saved.currency, saved.created_by), (5000, 'BDT', self.alice))
        self.assertEqual(saved.workspace, self.shop)
        # The response carries the server's row so the client can match it
        self.assertEqual(results[1]['row']['version'], 1)
        self.assertGreater(results[1]['row']['server_seq'], results[0]['row']['server_seq'])

    def test_replayed_mutation_is_not_applied_twice(self):
        book, entry, _ = self.push_book_with_entry()
        edit = self.upsert('entries', entry['row_id'], amount_minor=700)
        first = self.push(self.alice_client, self.shop, edit)
        second = self.push(self.alice_client, self.shop, edit)
        self.assertEqual(first[0]['status'], 'applied')
        self.assertEqual(second[0]['status'], 'applied')
        self.assertEqual(Entry.objects.get(id=entry['row_id']).version, 2)

    def test_edits_to_different_fields_both_survive(self):
        book, entry, _ = self.push_book_with_entry()
        self.push(self.alice_client, self.shop, self.upsert('entries', entry['row_id'], amount_minor=900))
        self.push(self.alice_client, self.shop, self.upsert('entries', entry['row_id'], title='Corrected'))
        saved = Entry.objects.get(id=entry['row_id'])
        self.assertEqual((saved.amount_minor, saved.title, saved.version), (900, 'Corrected', 3))

    def test_delete_is_a_tombstone_and_wins_over_a_later_edit(self):
        book, entry, _ = self.push_book_with_entry()
        results = self.push(
            self.alice_client, self.shop,
            self.delete('entries', entry['row_id']),
            self.delete('entries', entry['row_id']),
            self.upsert('entries', entry['row_id'], amount_minor=1),
        )
        self.assertEqual([r['status'] for r in results], ['applied', 'applied', 'rejected'])
        self.assertEqual(results[2]['error']['code'], 'deleted')
        self.assertIsNotNone(results[2]['row']['deleted_at'])
        self.assertEqual(Entry.all_objects.get(id=entry['row_id']).amount_minor, 5000)

    def test_invalid_rows_are_rejected_without_stopping_the_batch(self):
        book = self.upsert('cashbooks', book_name='Till', currency='BDT')
        bad_amounts = [
            self.upsert('entries', cashbook_id=book['row_id'], entry_type='cash_in', amount_minor=amount,
                        entry_date='2026-10-01')
            for amount in (10.5, '100', -5, 0, True)
        ]
        bad_date = self.upsert('entries', cashbook_id=book['row_id'], entry_type='cash_in', amount_minor=5,
                               entry_date='yesterday')
        good = self.upsert('entries', cashbook_id=book['row_id'], entry_type='cash_out', amount_minor=5,
                           entry_date='2026-10-01')
        results = self.push(self.alice_client, self.shop, book, *bad_amounts, bad_date, good)
        self.assertEqual([r['status'] for r in results], ['applied'] + ['rejected'] * 6 + ['applied'])
        self.assertTrue(all(r['error']['code'] == 'invalid' for r in results[1:7]))
        self.assertEqual(Entry.objects.count(), 1)

    def test_currency_and_parent_cannot_change(self):
        book, entry, _ = self.push_book_with_entry()
        other = self.upsert('cashbooks', book_name='Safe', currency='BDT')
        results = self.push(
            self.alice_client, self.shop, other,
            self.upsert('cashbooks', book['row_id'], currency='USD'),
            self.upsert('entries', entry['row_id'], cashbook_id=other['row_id']),
        )
        self.assertEqual([r.get('error', {}).get('code') for r in results], [None, 'immutable', 'immutable'])

    def test_server_owned_columns_are_ignored(self):
        book, entry, _ = self.push_book_with_entry()
        self.push(self.alice_client, self.shop, self.upsert(
            'entries', entry['row_id'], workspace_id=str(self.personal.id), version=99,
            created_by_id=str(self.bob.id), currency='USD', deleted_at='2020-01-01T00:00:00Z',
        ))
        saved = Entry.objects.get(id=entry['row_id'])
        self.assertEqual((saved.workspace, saved.version, saved.created_by, saved.currency),
                         (self.shop, 2, self.alice, 'BDT'))

    def test_starting_categories_have_colours_of_their_own(self):
        colours = dict(Category.objects.filter(workspace=self.personal).values_list('name', 'color'))
        self.assertEqual(colours, Category.DEFAULT_COLORS)
        self.assertEqual(len(set(colours.values())), len(Category.DEFAULT_EXPENSE) + len(Category.DEFAULT_INCOME))

    def test_category_colours(self):
        food = Category.objects.get(workspace=self.personal, name='Food')
        book = self.upsert('cashbooks', book_name='Till', currency='BDT')
        rent = self.upsert('entry_categories', cashbook_id=book['row_id'], category_name='Rent', color='#3B9EE5')
        results = self.push(
            self.alice_client, self.personal,
            self.upsert('categories', str(food.id), color='#E8705F'),
            self.upsert('categories', name='Pets', kind='expense', color='red'),  # not #RRGGBB
        )
        self.assertEqual([r.get('error', {}).get('code') for r in results], [None, 'invalid'])
        self.assertEqual([r['status'] for r in self.push(self.alice_client, self.shop, book, rent)], ['applied'] * 2)

        pulled = self.pull(self.alice_client, self.personal)['changes']['categories']
        self.assertEqual({row['name']: row['color'] for row in pulled}['Food'], '#E8705F')
        self.assertEqual(self.pull(self.alice_client, self.shop)['changes']['entry_categories'][0]['color'], '#3B9EE5')

        # Back to the colour the app picks
        self.push(self.alice_client, self.personal, self.upsert('categories', str(food.id), color=None))
        food.refresh_from_db()
        self.assertEqual((food.color, food.name), (None, 'Food'))

    def test_personal_ledger_tables(self):
        account = Account.objects.get(workspace=self.personal)
        category = Category.objects.filter(workspace=self.personal, kind='expense').first()
        savings = self.upsert('accounts', name='Savings', kind='savings', currency='BDT')
        group = new_id()
        results = self.push(
            self.alice_client, self.personal, savings,
            self.upsert('transactions', account_id=str(account.id), category_id=str(category.id), kind='expense',
                        amount_minor=-2500, occurred_on='2026-10-01', note='Lunch'),
            self.upsert('transactions', account_id=str(account.id), kind='transfer', amount_minor=-10000,
                        transfer_group_id=group, occurred_on='2026-10-01'),
            self.upsert('transactions', account_id=savings['row_id'], kind='transfer', amount_minor=10000,
                        transfer_group_id=group, occurred_on='2026-10-01'),
            self.upsert('budgets', category_id=str(category.id), amount_minor=500000, currency='BDT'),
            # An expense must be negative, and a transfer needs its group
            self.upsert('transactions', account_id=str(account.id), kind='expense', amount_minor=2500,
                        occurred_on='2026-10-01'),
            self.upsert('transactions', account_id=str(account.id), kind='transfer', amount_minor=-1,
                        occurred_on='2026-10-01'),
        )
        self.assertEqual([r['status'] for r in results], ['applied'] * 5 + ['rejected'] * 2)
        self.assertEqual(Transaction.objects.filter(workspace=self.personal).count(), 3)
        self.assertEqual(sum(t.amount_minor for t in Transaction.objects.filter(transfer_group_id=group)), 0)


class SplitSyncTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.account = Account.objects.get(workspace=self.personal)
        food, transport = Category.objects.filter(workspace=self.personal, name__in=['Food', 'Transport']).order_by('name')
        self.parts = [
            {'category_id': str(food.id), 'amount_minor': 300, 'note': 'Lunch'},
            {'category_id': str(transport.id), 'amount_minor': 150},
            {'category_id': None, 'amount_minor': 50},
        ]
        self.food = food

    def expense(self, **data):
        return self.upsert('transactions', account_id=str(self.account.id), kind='expense', occurred_on='2026-10-01',
                           **data)

    def test_a_split_travels_with_its_amount(self):
        created = self.expense(amount_minor=-500, splits=self.parts)
        result = self.push(self.alice_client, self.personal, created)[0]
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(
            result['row']['splits'],
            [{**self.parts[0]}, {**self.parts[1], 'note': ''}, {**self.parts[2], 'note': ''}],
        )
        self.assertIsNone(result['row']['category_id'])
        pulled = self.pull(self.alice_client, self.personal)['changes']['transactions']
        self.assertEqual(pulled[-1]['splits'], result['row']['splits'])

        # The amount and its parts change together, in one mutation
        smaller = [self.parts[0], {**self.parts[1], 'amount_minor': 100}]
        results = self.push(
            self.alice_client, self.personal,
            self.upsert('transactions', created['row_id'], amount_minor=-400, splits=smaller),
            # An app from before splits changes the amount alone
            self.upsert('transactions', created['row_id'], amount_minor=-900),
            self.upsert('transactions', created['row_id'], note='Bazar'),
            # Back to one category
            self.upsert('transactions', created['row_id'], splits=[], category_id=str(self.food.id)),
        )
        self.assertEqual([r['status'] for r in results], ['applied', 'rejected', 'applied', 'applied'])
        self.assertEqual(results[1]['error']['code'], 'invalid')
        saved = Transaction.objects.get(id=created['row_id'])
        self.assertEqual((saved.amount_minor, saved.splits, saved.category, saved.note), (-400, [], self.food, 'Bazar'))

    def test_a_part_must_name_a_category_of_this_workspace(self):
        book = self.push_book_with_entry()[0]
        elsewhere = EntryCategory.objects.create(cashbook_id=book['row_id'], category_name='Sales')
        other = Category.objects.filter(workspace=get_personal_workspace(self.bob)).first()
        for stranger in (elsewhere, other):
            parts = [{'category_id': str(stranger.id), 'amount_minor': 250}, {'category_id': None, 'amount_minor': 250}]
            result = self.push(self.alice_client, self.personal, self.expense(amount_minor=-500, splits=parts))[0]
            self.assertEqual((result['status'], result['error']['code']), ('rejected', 'invalid'))

    def test_a_category_deleted_since_does_not_block_other_edits(self):
        created = self.expense(amount_minor=-500, splits=self.parts)
        self.push(self.alice_client, self.personal, created)
        self.food.soft_delete()
        result = self.push(self.alice_client, self.personal, self.upsert('transactions', created['row_id'], note='x'))[0]
        self.assertEqual(result['status'], 'applied')


class BudgetSyncTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.food = Category.objects.get(workspace=self.personal, name='Food')

    def budget(self, month=None, amount=500000, category=None):
        data = {'category_id': str((category or self.food).id), 'amount_minor': amount, 'currency': 'BDT'}
        if month is not None:
            data['month'] = month
        return self.upsert('budgets', **data)

    def test_month_override_lives_beside_the_recurring_budget(self):
        results = self.push(
            self.alice_client, self.personal,
            self.budget(), self.budget('2026-10-01', amount=800000),
            # One override per month, one recurring budget per category
            self.budget('2026-10-01', amount=1), self.budget(amount=1),
        )
        self.assertEqual([r['status'] for r in results], ['applied', 'applied', 'rejected', 'rejected'])
        self.assertEqual(str(results[1]['row']['month']), '2026-10-01')
        self.assertEqual(Budget.objects.filter(category=self.food).count(), 2)

    def test_month_must_be_a_first_day_and_category_an_expense(self):
        salary = Category.objects.get(workspace=self.personal, name='Salary')
        results = self.push(
            self.alice_client, self.personal, self.budget('2026-10-15'), self.budget(category=salary),
        )
        self.assertEqual([r['error']['code'] for r in results], ['invalid', 'invalid'])

    def test_deleted_override_frees_the_month(self):
        first = self.budget('2026-11-01')
        self.push(self.alice_client, self.personal, first, self.delete('budgets', first['row_id']))
        results = self.push(self.alice_client, self.personal, self.budget('2026-11-01'))
        self.assertEqual(results[0]['status'], 'applied')


class TransferSyncTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.cash = Account.objects.get(workspace=self.personal)
        self.savings = Account.objects.create(workspace=self.personal, name='Savings', kind='savings')

    def leg(self, account, amount, group):
        return self.upsert('transactions', account_id=str(account.id), kind='transfer', amount_minor=amount,
                           transfer_group_id=group, occurred_on='2026-10-01')

    def test_a_transfer_has_two_legs_moving_opposite_ways(self):
        group, other_group = new_id(), new_id()
        results = self.push(
            self.alice_client, self.personal,
            self.leg(self.cash, -1000, group), self.leg(self.savings, 1000, group),
            self.leg(self.savings, 1000, group),  # a third leg
            self.leg(self.cash, -500, other_group), self.leg(self.savings, -500, other_group),  # same direction
        )
        self.assertEqual([r['status'] for r in results], ['applied', 'applied', 'rejected', 'applied', 'rejected'])
        self.assertEqual(results[2]['error']['code'], 'invalid')

    def test_editing_a_leg_keeps_the_pair_consistent(self):
        group = new_id()
        out_leg, in_leg = self.leg(self.cash, -1000, group), self.leg(self.savings, 1000, group)
        self.push(self.alice_client, self.personal, out_leg, in_leg)
        results = self.push(
            self.alice_client, self.personal,
            self.upsert('transactions', out_leg['row_id'], amount_minor=-2000),
            self.upsert('transactions', in_leg['row_id'], amount_minor=2000),
            self.upsert('transactions', in_leg['row_id'], amount_minor=-2000),
        )
        self.assertEqual([r['status'] for r in results], ['applied', 'applied', 'rejected'])


class CashBookCascadeTests(SyncTestCase):
    def test_deleting_a_book_tombstones_its_rows_for_every_device(self):
        book, entry, _ = self.push_book_with_entry()
        cursor = self.pull(self.alice_client, self.shop)['next_since']
        self.push(self.alice_client, self.shop, self.delete('cashbooks', book['row_id']))

        changes = self.pull(self.alice_client, self.shop, since=cursor)['changes']
        self.assertEqual([str(row['id']) for row in changes['cashbooks']], [book['row_id']])
        self.assertEqual([str(row['id']) for row in changes['entries']], [entry['row_id']])
        tombstones = [row for table in ('entries', 'entry_categories', 'payment_methods') for row in changes[table]]
        self.assertTrue(tombstones)
        self.assertTrue(all(row['deleted_at'] for row in tombstones))
        self.assertFalse(Entry.objects.filter(cashbook_id=book['row_id']).exists())
        self.assertFalse(EntryCategory.objects.filter(cashbook_id=book['row_id']).exists())


class PushPermissionTests(SyncTestCase):
    def test_outsider_cannot_push_or_pull(self):
        self.assertEqual(self.bob_client.get(PULL, {'workspace': str(self.shop.id)}).status_code, 404)
        response = self.bob_client.post(PUSH, {'workspace': str(self.shop.id), 'mutations': []}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_cannot_write_rows_of_another_workspace_through_own(self):
        book, entry, _ = self.push_book_with_entry()
        bob_personal = get_personal_workspace(self.bob)
        results = self.push(
            self.bob_client, bob_personal,
            self.upsert('entries', entry['row_id'], amount_minor=1),
            self.delete('entries', entry['row_id']),
            self.upsert('entries', cashbook_id=book['row_id'], entry_type='cash_in', amount_minor=1,
                        entry_date='2026-10-01'),
        )
        self.assertEqual([r['status'] for r in results], ['rejected'] * 3)
        self.assertTrue(all(r['row'] is None for r in results))
        saved = Entry.objects.get(id=entry['row_id'])
        self.assertEqual((saved.amount_minor, saved.deleted_at), (5000, None))

    def test_rows_only_the_server_makes(self):
        account = Account.objects.get(workspace=self.personal)
        table = TABLES['accounts']
        with mock.patch.object(table, 'client_creates', False), mock.patch.object(table, 'client_deletes', False):
            results = self.push(
                self.alice_client, self.personal,
                self.upsert('accounts', name='Savings', kind='savings', currency='BDT'),
                self.delete('accounts', str(account.id)),
                # Its writable columns stay open
                self.upsert('accounts', str(account.id), name='Wallet'),
            )
        self.assertEqual([r['status'] for r in results], ['rejected', 'rejected', 'applied'])
        self.assertEqual([r['error']['code'] for r in results[:2]], ['forbidden', 'forbidden'])
        account.refresh_from_db()
        self.assertEqual((account.name, account.deleted_at), ('Wallet', None))

    def test_a_table_can_refuse_a_write_by_what_changed(self):
        account = Account.objects.get(workspace=self.personal)

        def on_write(workspace, user, obj, creating, before):
            if not creating and before.get('name') == account.name:
                raise ValidationError('The name is settled.')

        with mock.patch.object(TABLES['accounts'], 'on_write', on_write):
            results = self.push(
                self.alice_client, self.personal,
                self.upsert('accounts', str(account.id), name='Wallet'),
                self.upsert('accounts', str(account.id), is_archived=True),
            )
        self.assertEqual([r['status'] for r in results], ['rejected', 'applied'])
        self.assertEqual(results[0]['error']['code'], 'invalid')

    def test_category_from_another_book_is_rejected(self):
        book, entry, _ = self.push_book_with_entry()
        other = CashBook.objects.create(workspace=self.shop, book_name='Safe')
        foreign = EntryCategory.objects.create(cashbook=other, category_name='Rent')
        results = self.push(self.alice_client, self.shop,
                            self.upsert('entries', entry['row_id'], category_id=str(foreign.id)))
        self.assertEqual(results[0]['error']['code'], 'invalid')

    def test_staff_roles(self):
        book, entry, _ = self.push_book_with_entry()
        safe = CashBook.objects.create(workspace=self.shop, book_name='Safe')
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_STAFF)
        CashBookAdditionalMember.objects.create(cashbook_id=book['row_id'], member=self.bob, role='editor')

        results = self.push(
            self.bob_client, self.shop,
            self.upsert('entries', cashbook_id=book['row_id'], entry_type='cash_out', amount_minor=100,
                        entry_date='2026-10-01'),                                    # editor may add entries
            self.upsert('entries', cashbook_id=str(safe.id), entry_type='cash_out', amount_minor=100,
                        entry_date='2026-10-01'),                                    # not in a book without a grant
            self.upsert('entry_categories', cashbook_id=book['row_id'], category_name='X'),  # admin only
            self.upsert('cashbooks', book_name='Mine', currency='BDT'),              # managers only
            self.delete('cashbooks', book['row_id']),                                # owner only
            self.upsert('accounts', name='Wallet', currency='BDT'),                  # managers only
        )
        self.assertEqual([r['status'] for r in results], ['applied'] + ['rejected'] * 5)
        self.assertEqual([r['error']['code'] for r in results[1:]], ['forbidden'] * 5)


    def test_a_contributor_adds_entries_but_never_changes_them(self):
        book, entry, _ = self.push_book_with_entry()
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_STAFF)
        CashBookAdditionalMember.objects.create(cashbook_id=book['row_id'], member=self.bob, role='contributor')

        added = self.upsert('entries', cashbook_id=book['row_id'], entry_type='cash_in', amount_minor=100,
                            entry_date='2026-10-01')
        results = self.push(self.bob_client, self.shop, added)
        self.assertEqual(results[0]['status'], 'applied')

        results = self.push(
            self.bob_client, self.shop,
            self.upsert('entries', added['row_id'], amount_minor=1),     # not even their own
            self.upsert('entries', entry['row_id'], amount_minor=1),
            self.delete('entries', entry['row_id']),
            self.upsert('cashbooks', book['row_id'], book_name='Mine'),
        )
        self.assertEqual([r['status'] for r in results], ['rejected'] * 4)
        self.assertEqual({r['error']['code'] for r in results}, {'forbidden'})


class PullTests(SyncTestCase):
    def test_pull_returns_changes_after_cursor_including_tombstones(self):
        book, entry, _ = self.push_book_with_entry()
        first = self.pull(self.alice_client, self.shop)
        self.assertEqual([r['id'] for r in first['changes']['cashbooks']], [uuid.UUID(book['row_id'])])
        self.assertEqual(first['changes']['entries'][0]['amount_minor'], 5000)
        self.assertFalse(first['has_more'])
        self.assertEqual(first['accessible_cashbook_ids'], [uuid.UUID(book['row_id'])])

        # Nothing new
        again = self.pull(self.alice_client, self.shop, since=first['next_since'])
        self.assertTrue(all(rows == [] for rows in again['changes'].values()))
        self.assertEqual(again['next_since'], first['next_since'])

        self.push(self.alice_client, self.shop, self.delete('entries', entry['row_id']))
        after = self.pull(self.alice_client, self.shop, since=first['next_since'])
        self.assertEqual(after['changes']['cashbooks'], [])
        self.assertIsNotNone(after['changes']['entries'][0]['deleted_at'])

    def test_bulk_updates_still_move_the_cursor(self):
        book, entry, _ = self.push_book_with_entry()
        cursor = self.pull(self.alice_client, self.shop)['next_since']
        Entry.objects.filter(id=entry['row_id']).update(title='Edited in admin')
        changed = self.pull(self.alice_client, self.shop, since=cursor)['changes']['entries']
        self.assertEqual([row['title'] for row in changed], ['Edited in admin'])

    def test_pull_pages_through_everything_exactly_once(self):
        book = self.upsert('cashbooks', book_name='Till', currency='BDT')
        entries = [
            self.upsert('entries', cashbook_id=book['row_id'], entry_type='cash_in', amount_minor=i + 1,
                        entry_date='2026-10-01')
            for i in range(7)
        ]
        self.push(self.alice_client, self.shop, book, *entries)

        seen, since, pages = [], 0, 0
        while True:
            page = self.pull(self.alice_client, self.shop, since=since, limit=3)
            seen += [str(row['id']) for row in page['changes']['entries']]
            since, pages = page['next_since'], pages + 1
            if not page['has_more']:
                break
        self.assertEqual(sorted(seen), sorted(e['row_id'] for e in entries))
        self.assertGreater(pages, 2)

    def test_personal_workspace_starts_with_defaults(self):
        changes = self.pull(self.alice_client, self.personal)['changes']
        self.assertEqual([a['name'] for a in changes['accounts']], ['Cash'])
        self.assertEqual(len(changes['categories']), len(Category.DEFAULT_EXPENSE) + len(Category.DEFAULT_INCOME))

    def test_staff_only_receive_granted_books(self):
        book, entry, _ = self.push_book_with_entry()
        safe = CashBook.objects.create(workspace=self.shop, book_name='Safe')
        Entry.objects.create(cashbook=safe, amount_minor=1, entry_date='2026-10-01')
        Account.objects.create(workspace=self.shop, name='Owner wallet')
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_STAFF)
        grant = CashBookAdditionalMember.objects.create(cashbook_id=book['row_id'], member=self.bob, role='viewer')

        data = self.pull(self.bob_client, self.shop)
        self.assertEqual([str(r['id']) for r in data['changes']['cashbooks']], [book['row_id']])
        self.assertEqual([str(r['id']) for r in data['changes']['entries']], [entry['row_id']])
        self.assertEqual(data['changes']['accounts'], [])
        self.assertEqual(data['accessible_cashbook_ids'], [uuid.UUID(book['row_id'])])

        grant.soft_delete()
        data = self.pull(self.bob_client, self.shop)
        self.assertEqual(data['accessible_cashbook_ids'], [])
        self.assertEqual(data['changes']['entries'], [])


class DemoBusinessTests(SyncTestCase):
    def test_demo_business_is_created_once_and_can_be_deleted(self):
        response = self.alice_client.post('/api/v1/workspaces/demo/')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['is_demo'])
        self.assertEqual(self.alice_client.post('/api/v1/workspaces/demo/').data['id'], response.data['id'])

        demo_id = response.data['id']
        changes = self.alice_client.get(PULL, {'workspace': demo_id}).data['changes']
        self.assertEqual(len(changes['cashbooks']), 1)
        self.assertEqual(len(changes['entries']), len(DEMO_ENTRIES))

        self.assertEqual(self.bob_client.delete(f'/api/v1/workspaces/{demo_id}/').status_code, 404)
        self.assertEqual(self.alice_client.delete(f'/api/v1/workspaces/{demo_id}/').status_code, 204)
        self.assertEqual(self.alice_client.delete(f'/api/v1/workspaces/{self.personal.id}/').status_code, 403)


class UnprivilegedRoleEndToEndTests(UnprivilegedRoleMixin, SyncTestCase):
    """
    The same flows on a database role that row-level security applies to,
    which is how production runs. Catches code that only works as a superuser.
    """

    def setUp(self):
        super().setUp()
        self.use_unprivileged_role()

    def test_signup_seed_push_pull_and_rest(self):
        # Created under RLS: the personal-workspace seed has to pass the policies
        carol = User.objects.create_user(email='carol@example.com', password='pass-12345')
        client = APIClient()
        client.force_authenticate(carol)
        personal = get_personal_workspace(carol)
        self.assertEqual(len(self.pull(client, personal)['changes']['accounts']), 1)

        demo = client.post('/api/v1/workspaces/demo/')
        self.assertEqual(demo.status_code, 201, demo.data)

        book, entry, results = self.push_book_with_entry()
        self.assertEqual([r['status'] for r in results], ['applied', 'applied'])
        self.assertEqual(len(self.pull(self.alice_client, self.shop)['changes']['entries']), 1)

        created = self.alice_client.post('/api/v1/cashbooks/', {'book_name': 'Bank', 'workspace': str(self.shop.id)})
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(len(self.alice_client.get('/api/v1/cashbooks/').data['results']), 2)

        # Bob still sees nothing of Alice's
        results = self.push(self.bob_client, get_personal_workspace(self.bob),
                            self.upsert('entries', entry['row_id'], amount_minor=1))
        self.assertEqual(results[0]['status'], 'rejected')
        self.assertEqual(self.bob_client.get('/api/v1/cashbooks/').data['results'], [])


CENSUS = '/api/v1/sync/census/'


class HardDeleteGuardTests(SyncTestCase):
    """A hard delete leaves no tombstone, so phones would keep the row for good: the database refuses it."""

    def test_every_synced_table_refuses_a_hard_delete(self):
        from django.db import connection

        from .guard import TRIGGER
        with connection.cursor() as cursor:
            cursor.execute('SELECT tgrelid::regclass::text FROM pg_trigger WHERE tgname = %s', [TRIGGER])
            guarded = {name.strip('"') for (name,) in cursor.fetchall()}
        tables = {table.model._meta.db_table for table in TABLES.values()}
        self.assertEqual(tables - guarded, set(), 'add sync.guard.guard_deletes() for these in a migration')

    def test_a_hard_delete_is_refused_and_the_row_stays(self):
        from django.db import IntegrityError, transaction
        cash = Account.objects.get(workspace=self.personal, name='Cash')
        row = Transaction.objects.create(account=cash, kind='expense', amount_minor=-2300, occurred_on='2026-10-01')
        for queryset in (Transaction.all_objects.filter(id=row.id), Account.all_objects.filter(id=cash.id)):
            with self.assertRaises(IntegrityError), transaction.atomic():
                queryset.delete()
        self.assertTrue(Transaction.objects.filter(id=row.id).exists())

    def test_removing_a_whole_workspace_is_the_one_way_through(self):
        from django.contrib import admin
        from django.db import IntegrityError, transaction

        from workspaces.models import Workspace
        from .guard import allow_hard_delete
        cash = Account.objects.get(workspace=self.personal, name='Cash')
        Transaction.objects.create(account=cash, kind='expense', amount_minor=-100, occurred_on='2026-10-01')
        CashBook.objects.create(workspace=self.shop, book_name='Till')
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.shop.delete()
        # Through the admin a business goes whole, its book with it
        admin.site._registry[Workspace].delete_model(None, self.shop)
        self.assertFalse(Workspace.objects.filter(id=self.shop.id).exists())
        with allow_hard_delete():
            self.personal.delete()
        self.assertFalse(Transaction.all_objects.filter(account=cash).exists())
        # The permission ends with the block
        other = get_personal_workspace(self.bob)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Account.all_objects.filter(workspace=other).delete()

    def test_a_tombstone_reaches_the_phone(self):
        from .guard import tombstone
        cash = Account.objects.get(workspace=self.personal, name='Cash')
        row = Transaction.objects.create(account=cash, kind='expense', amount_minor=-5600, occurred_on='2026-10-03')
        since = self.pull(self.alice_client, self.personal)['next_since']
        self.assertEqual(tombstone(Transaction.objects.filter(id=row.id)), 1)
        changes = self.pull(self.alice_client, self.personal, since=since)['changes']['transactions']
        self.assertEqual([(str(r['id']), r['deleted_at'] is not None) for r in changes], [(str(row.id), True)])


class AdminDeleteTests(SyncTestCase):
    def test_every_synced_model_is_tombstoned_by_the_admin(self):
        from django.contrib import admin

        from .admin import TombstoneOnDeleteAdmin
        for table in TABLES.values():
            model_admin = admin.site._registry.get(table.model)
            if model_admin is not None:
                self.assertIsInstance(model_admin, TombstoneOnDeleteAdmin, table.model.__name__)

    def test_deleting_in_the_admin_leaves_a_tombstone(self):
        from django.contrib import admin
        cash = Account.objects.get(workspace=self.personal, name='Cash')
        rows = [
            Transaction.objects.create(account=cash, kind='expense', amount_minor=-amount, occurred_on='2026-10-01')
            for amount in (100, 200)
        ]
        model_admin = admin.site._registry[Transaction]
        model_admin.delete_queryset(None, Transaction.objects.filter(id__in=[r.id for r in rows]))
        self.assertEqual(
            Transaction.all_objects.filter(id__in=[r.id for r in rows], deleted_at__isnull=False).count(), 2,
        )
        deleted, _, perms_needed, protected = model_admin.get_deleted_objects([cash], None)
        self.assertEqual((len(deleted), perms_needed, protected), (1, set(), []))


class SeedDemoTests(SyncTestCase):
    def test_a_reset_tombstones_what_it_replaces(self):
        from django.core.management import call_command
        cash = Account.objects.get(workspace=self.personal, name='Cash')
        mine = Transaction.objects.create(account=cash, kind='expense', amount_minor=-2300, occurred_on='2026-10-01')
        since = self.pull(self.alice_client, self.personal)['next_since']
        call_command('seed_demo', self.alice.username, '--reset', stdout=mock.MagicMock())
        changes = self.pull(self.alice_client, self.personal, since=since)['changes']
        gone = {str(r['id']) for r in changes['transactions'] if r['deleted_at']}
        self.assertIn(str(mine.id), gone)
        # Again on top: the demo rows are tombstoned in turn and nothing is refused
        call_command('seed_demo', self.alice.username, '--reset', stdout=mock.MagicMock())


class CensusTests(SyncTestCase):
    def census(self, table, ids, client=None, workspace=None):
        return (client or self.alice_client).post(
            CENSUS, {'workspace': str((workspace or self.personal).id), 'table': table, 'ids': ids}, format='json',
        )

    def test_rows_the_server_does_not_have_are_named(self):
        from .guard import tombstone
        cash = Account.objects.get(workspace=self.personal, name='Cash')
        kept = Transaction.objects.create(account=cash, kind='expense', amount_minor=-100, occurred_on='2026-10-01')
        deleted = Transaction.objects.create(account=cash, kind='expense', amount_minor=-200, occurred_on='2026-10-01')
        tombstone(Transaction.objects.filter(id=deleted.id))
        ghost = new_id()
        response = self.census('transactions', [str(kept.id), str(deleted.id), ghost])
        self.assertEqual(response.status_code, 200, response.data)
        # A tombstone is not missing: the pull already tells the phone
        self.assertEqual(response.data, {'missing': [ghost]})

    def test_another_workspaces_rows_count_as_missing(self):
        book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        # The shop's book, asked about in the personal workspace
        self.assertEqual(self.census('cashbooks', [str(book.id)]).data, {'missing': [str(book.id)]})
        self.assertEqual(self.census('cashbooks', [str(book.id)], workspace=self.shop).data, {'missing': []})

    def test_bad_requests(self):
        from .views import MAX_CENSUS_IDS
        self.assertEqual(self.census('transactions', [new_id()], client=self.bob_client).status_code, 404)
        self.assertEqual(self.census('users', [new_id()]).status_code, 400)
        self.assertEqual(self.census('transactions', 'all').status_code, 400)
        self.assertEqual(self.census('transactions', ['not-an-id']).status_code, 400)
        self.assertEqual(
            self.census('transactions', [new_id() for _ in range(MAX_CENSUS_IDS + 1)]).status_code, 400,
        )
        self.assertIn(APIClient().post(CENSUS, {}, format='json').status_code, (401, 403))
