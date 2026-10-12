from datetime import timedelta

from django.utils import timezone

from billing import gates, locks
from billing.catalog.keys import F
from billing.models import KeepSelection, Stamp
from billing.rules import rule_for
from billing.testing import grant_plan, set_limit
from cashbook.models import CashBook, Entry
from ledger_personal.models import Account, Category, Transaction
from workspaces.models import Membership, Workspace
from workspaces.services import create_demo_business, create_workspace

from .base import BillingTestCase, client_for, new_id

BOOKS = '/api/v1/cashbooks/'
KEEP = '/api/v1/billing/keep/'


class AccountDowngradeTests(BillingTestCase):
    """Alice made seven accounts on Plus, then went back to Free, which allows four."""

    def setUp(self):
        super().setUp()
        self.subscription = grant_plan(self.alice, 'plus')
        for number in range(6):
            self.assertApplied(self.push_one(self.account(f'Account {number}')))
        by_name = {account.name: account for account in Account.objects.filter(workspace=self.personal)}
        self.accounts = [by_name['Cash']] + [by_name[f'Account {number}'] for number in range(6)]
        self.age(self.accounts)
        self.ids = [str(account.id) for account in self.accounts]
        self.subscription.delete()

    def lock(self):
        found = [lock for lock in self.entitlements()['locks'] if lock['feature'] == 'personal.accounts']
        self.assertEqual(len(found), 1, self.entitlements()['locks'])
        return found[0]

    def choose(self, ids, client=None, scope=None):
        return (client or self.alice_client).put(
            f'{KEEP}personal.accounts/', {'scope': str(scope or self.personal.id), 'ids': ids}, format='json',
        )

    def test_nothing_is_deleted_and_the_oldest_stay_editable_until_she_chooses(self):
        self.assertEqual(Account.objects.filter(workspace=self.personal).count(), 7)
        lock = self.lock()
        self.assertEqual(lock['scope'], str(self.personal.id))
        self.assertEqual((lock['limit'], lock['pending'], lock['can_change_at']), (4, True, None))
        self.assertEqual(lock['kept'], self.ids[:4])
        self.assertEqual(lock['locked'], sorted(self.ids[4:]))

    def test_a_locked_account_is_read_only_and_a_kept_one_is_not(self):
        result = self.push_one(self.upsert('accounts', self.ids[5], name='Renamed'))
        self.assertPlanLimit(result, F.PERSONAL_ACCOUNTS, 'locked')
        # The app is handed the row as it still is, so it can put it back
        self.assertEqual(result['row']['name'], 'Account 4')
        self.assertApplied(self.push_one(self.upsert('accounts', self.ids[1], name='Renamed')))

    def test_transactions_follow_their_account(self):
        def spend(account_id):
            return self.upsert(
                'transactions', account_id=account_id, kind='expense', amount_minor=-500, occurred_on='2026-10-01',
            )
        self.assertPlanLimit(self.push_one(spend(self.ids[6])), F.PERSONAL_ACCOUNTS, 'locked')
        kept = spend(self.ids[0])
        self.assertApplied(self.push_one(kept))
        self.assertEqual(Transaction.objects.count(), 1)
        # Moving a transaction onto a locked account is a write to it too
        self.assertPlanLimit(
            self.push_one(self.upsert('transactions', kept['row_id'], account_id=self.ids[6])),
            F.PERSONAL_ACCOUNTS, 'locked',
        )

    def test_locked_accounts_can_be_archived_or_deleted_which_frees_the_rest(self):
        self.assertApplied(self.push_one(self.upsert('accounts', self.ids[6], is_archived=True)))
        self.assertApplied(self.push_one(self.delete('accounts', self.ids[5])))
        self.assertApplied(self.push_one(self.delete('accounts', self.ids[4])))
        self.assertEqual(self.entitlements()['locks'], [])
        self.assertApplied(self.push_one(self.upsert('accounts', self.ids[3], name='Free again')))

    def test_reads_keep_working(self):
        response = self.alice_client.get('/api/v1/sync/pull/', {'workspace': str(self.personal.id), 'since': 0})
        self.assertEqual(len(response.data['changes']['accounts']), 7)

    def test_upgrading_unlocks_everything_at_once(self):
        grant_plan(self.alice, 'plus')
        self.assertEqual(self.entitlements()['locks'], [])
        self.assertApplied(self.push_one(self.upsert('accounts', self.ids[6], name='Back')))

    def test_her_choice_is_honoured(self):
        wanted = [self.ids[0], self.ids[2], self.ids[5], self.ids[6]]
        response = self.choose(wanted)
        self.assertEqual(response.status_code, 200, response.data)
        lock = [lock for lock in response.data['locks'] if lock['feature'] == 'personal.accounts'][0]
        self.assertEqual((lock['kept'], lock['pending']), (wanted, False))
        self.assertEqual(lock['locked'], sorted([self.ids[1], self.ids[3], self.ids[4]]))

        self.assertApplied(self.push_one(self.upsert('accounts', self.ids[6], name='Chosen')))
        self.assertPlanLimit(self.push_one(self.upsert('accounts', self.ids[1], name='Not chosen')),
                             F.PERSONAL_ACCOUNTS, 'locked')

    def test_the_choice_cannot_be_changed_again_before_the_cooldown(self):
        self.assertEqual(self.choose(self.ids[:4]).status_code, 200)
        lock = self.lock()
        can_change_at = timezone.datetime.fromisoformat(lock['can_change_at'])
        self.assertAlmostEqual(
            (can_change_at - timezone.now()).total_seconds(), timedelta(days=30).total_seconds(), delta=120,
        )
        again = self.choose(self.ids[3:7])
        self.assertEqual(again.status_code, 400, again.data)
        self.assertEqual(self.lock()['kept'], self.ids[:4], 'rotating the editable set must not be possible')

        # Once it has passed she may choose again
        KeepSelection.objects.update(chosen_at=timezone.now() - timedelta(days=31))
        self.assertIsNone(self.lock()['can_change_at'])
        self.assertEqual(self.choose(self.ids[3:7]).status_code, 200)

    def test_a_different_limit_lets_her_choose_again_at_once(self):
        self.assertEqual(self.choose(self.ids[:4]).status_code, 200)
        set_limit(self.alice, F.PERSONAL_ACCOUNTS, 5, mode='raise')
        lock = self.lock()
        self.assertEqual((lock['limit'], lock['pending'], lock['can_change_at']), (5, True, None))
        # What she chose before still counts; the new place goes to the oldest of the rest
        self.assertEqual(lock['kept'], self.ids[:5])
        self.assertEqual(self.choose(self.ids[2:7]).status_code, 200)

    def test_a_choice_must_be_hers_complete_and_real(self):
        cases = {
            'too few': self.ids[:3],
            'too many': self.ids[:5],
            'a duplicate': [self.ids[0]] * 4,
            'an id that does not exist': self.ids[:3] + [new_id()],
            'not ids': ['a', 'b', 'c', 'd'],
        }
        for name, ids in cases.items():
            self.assertEqual(self.choose(ids).status_code, 400, name)
        bob_account = Account.objects.filter(workspace__owner=self.bob).first()
        self.assertEqual(self.choose(self.ids[:3] + [str(bob_account.id)]).status_code, 400)
        archived = Account.objects.create(workspace=self.personal, name='Old', is_archived=True)
        self.assertEqual(self.choose(self.ids[:3] + [str(archived.id)]).status_code, 400)
        self.assertEqual(self.alice_client.put(f'{KEEP}made.up/', {'ids': []}, format='json').status_code, 400)
        self.assertFalse(KeepSelection.objects.exists())

    def test_nobody_else_can_choose_for_her(self):
        self.assertEqual(self.choose(self.ids[:4], client=self.bob_client).status_code, 400)
        Membership.objects.create(workspace=self.personal, user=self.bob, role=Membership.ROLE_ADMIN)
        self.assertEqual(self.choose(self.ids[:4], client=self.bob_client).status_code, 400)
        self.assertFalse(KeepSelection.objects.exists())

    def test_nothing_to_choose_when_everything_fits(self):
        grant_plan(self.alice, 'plus')
        self.assertEqual(self.choose(self.ids[:4]).status_code, 400)

    def test_a_kept_account_that_goes_gives_its_place_to_the_next_oldest(self):
        self.assertEqual(self.choose([self.ids[0], self.ids[1], self.ids[5], self.ids[6]]).status_code, 200)
        self.assertApplied(self.push_one(self.delete('accounts', self.ids[6])))
        lock = self.lock()
        self.assertEqual(lock['kept'], [self.ids[0], self.ids[1], self.ids[2], self.ids[5]])
        self.assertEqual(lock['locked'], sorted([self.ids[3], self.ids[4]]))

    def test_choosing_tells_her_devices_to_refresh(self):
        before = Stamp.current(self.alice.pk)
        self.choose(self.ids[:4])
        self.assertNotEqual(Stamp.current(self.alice.pk), before)

    def test_the_keep_screen_lists_names_and_what_is_kept(self):
        response = self.alice_client.get(KEEP)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        choice = response.data[0]
        self.assertEqual((choice['feature'], choice['noun'], choice['workspace_name']),
                         ('personal.accounts', 'account', 'Personal'))
        self.assertEqual([item['label'] for item in choice['items']], ['Cash'] + [f'Account {n}' for n in range(6)])
        self.assertEqual([item['kept'] for item in choice['items']], [True] * 4 + [False] * 3)
        self.assertEqual(self.bob_client.get(KEEP).data, [])

    def test_staff_can_reset_or_set_the_choice_without_waiting(self):
        rule = rule_for(F.PERSONAL_ACCOUNTS)
        self.choose(self.ids[:4])
        locks.choose(self.alice, rule, self.personal, 4, 30, [a.id for a in self.accounts[3:7]], force=True)
        self.assertEqual(self.lock()['kept'], self.ids[3:7])
        locks.reset(self.alice, rule, self.personal)
        lock = self.lock()
        self.assertEqual((lock['kept'], lock['pending'], lock['can_change_at']), (self.ids[:4], True, None))


class CategoryDowngradeTests(BillingTestCase):
    def test_only_her_own_categories_are_ever_locked(self):
        subscription = grant_plan(self.alice, 'plus')
        results = self.push(self.alice_client, self.personal, *[
            self.upsert('categories', name=f'Mine {n}', kind='expense') for n in range(12)
        ])
        self.assertEqual([r['status'] for r in results], ['applied'] * 12)
        subscription.delete()

        lock = self.entitlements()['locks'][0]
        self.assertEqual((lock['feature'], lock['limit'], len(lock['kept']), len(lock['locked'])),
                         ('personal.custom_categories', 10, 10, 2))
        locked = Category.objects.get(id=lock['locked'][0])
        self.assertFalse(locked.is_default)
        self.assertPlanLimit(self.push_one(self.upsert('categories', str(locked.id), name='Renamed')),
                             F.PERSONAL_CUSTOM_CATEGORIES, 'locked')
        food = Category.objects.get(workspace=self.personal, name='Food')
        self.assertApplied(self.push_one(self.upsert('categories', str(food.id), name='Groceries')))


class WorkspaceDowngradeTests(BillingTestCase):
    """Two businesses on Plus, then back to Free, which allows one."""

    def setUp(self):
        super().setUp()
        self.subscription = grant_plan(self.alice, 'plus')
        self.second = create_workspace(owner=self.alice, name='Second shop')
        self.age([self.shop, self.second])
        self.book = CashBook.objects.create(workspace=self.second, book_name='Till')
        self.demo = create_demo_business(self.alice)
        self.subscription.delete()

    def entry(self, book):
        return self.upsert('entries', cashbook_id=str(book.id), entry_type='cash_in', amount_minor=100,
                           entry_date='2026-10-01')

    def test_the_newer_business_is_read_only_everywhere(self):
        lock = [lock for lock in self.entitlements()['locks'] if lock['feature'] == 'business.workspaces'][0]
        self.assertEqual((lock['scope'], lock['kept'], lock['locked']),
                         ('', [str(self.shop.id)], [str(self.second.id)]))

        self.assertPlanLimit(self.push_one(self.entry(self.book), workspace=self.second),
                             F.BUSINESS_WORKSPACES, 'locked')
        response = self.alice_client.post(BOOKS, {'book_name': 'New', 'workspace': str(self.second.id)})
        self.assertEqual((response.status_code, response.data['reason']), (402, 'locked'))
        response = self.alice_client.post(f'{BOOKS}{self.book.id}/entries/', {
            'entry_type': 'cash_in', 'amount_minor': 100, 'entry_date': '2026-10-01',
        })
        self.assertEqual((response.status_code, response.data['feature']), (402, 'business.workspaces'))
        self.assertFalse(Entry.objects.filter(cashbook=self.book).exists())

        # The business she keeps works as before
        first_book = self.upsert('cashbooks', book_name='Till', currency='BDT')
        self.assertApplied(self.push_one(first_book, workspace=self.shop))

    def test_the_demo_business_is_never_locked_and_never_counted(self):
        demo_book = CashBook.objects.get(workspace=self.demo)
        self.assertApplied(self.push_one(self.entry(demo_book), workspace=self.demo))
        self.assertNotIn(str(self.demo.id), str(self.entitlements()['locks']))

    def test_she_can_keep_the_other_one_or_delete_her_way_out(self):
        response = self.alice_client.put(f'{KEEP}business.workspaces/', {'ids': [str(self.second.id)]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertApplied(self.push_one(self.entry(self.book), workspace=self.second))
        blocked = self.upsert('cashbooks', book_name='Till', currency='BDT')
        self.assertPlanLimit(self.push_one(blocked, workspace=self.shop), F.BUSINESS_WORKSPACES, 'locked')

        # Deleting inside a locked business, and deleting the business, both work
        self.assertEqual(self.alice_client.delete(f'/api/v1/workspaces/{self.second.id}/').status_code, 204)
        self.assertEqual(self.entitlements()['locks'], [])
        self.assertApplied(self.push_one(blocked | {'id': new_id()}, workspace=self.shop))


class CashbookDowngradeTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        subscription = grant_plan(self.alice, 'plus')
        self.books = [CashBook.objects.create(workspace=self.shop, book_name=f'Till {n}') for n in range(3)]
        self.age(self.books)
        self.old_entry = Entry.objects.create(cashbook=self.books[2], amount_minor=100, entry_date='2026-10-01')
        subscription.delete()

    def test_the_third_book_and_everything_in_it_is_read_only(self):
        locked, kept = self.books[2], self.books[0]

        def entry(book):
            return self.upsert('entries', cashbook_id=str(book.id), entry_type='cash_in', amount_minor=100,
                               entry_date='2026-10-01')
        self.assertPlanLimit(self.push_one(entry(locked), workspace=self.shop), F.BUSINESS_CASHBOOKS, 'locked')
        self.assertPlanLimit(
            self.push_one(self.upsert('entries', str(self.old_entry.id), title='Edited'), workspace=self.shop),
            F.BUSINESS_CASHBOOKS, 'locked',
        )
        self.assertPlanLimit(
            self.push_one(self.upsert('cashbooks', str(locked.id), book_name='Renamed'), workspace=self.shop),
            F.BUSINESS_CASHBOOKS, 'locked',
        )
        self.assertPlanLimit(
            self.push_one(self.upsert('entry_categories', cashbook_id=str(locked.id), category_name='New'),
                          workspace=self.shop),
            F.BUSINESS_CASHBOOKS, 'locked',
        )
        for path, body in (
            ('entries/', {'entry_type': 'cash_in', 'amount_minor': 100, 'entry_date': '2026-10-01'}),
            ('categories/', {'category_name': 'New'}),
            ('payment-methods/', {'payment_method_name': 'New'}),
        ):
            response = self.alice_client.post(f'{BOOKS}{locked.id}/{path}', body)
            self.assertEqual((response.status_code, response.data.get('feature')), (402, 'business.cashbooks'), path)
        self.assertEqual(self.alice_client.patch(f'{BOOKS}{locked.id}/', {'book_name': 'Renamed'}).status_code, 402)

        self.assertApplied(self.push_one(entry(kept), workspace=self.shop))
        self.assertEqual(self.alice_client.post(f'{BOOKS}{kept.id}/categories/', {'category_name': 'New'}).status_code, 201)

    def test_entries_in_a_locked_book_can_be_read_and_deleted(self):
        locked = self.books[2]
        self.assertEqual(self.alice_client.get(f'{BOOKS}{locked.id}/entries/').status_code, 200)
        self.assertEqual(self.alice_client.get(f'{BOOKS}{locked.id}/stats/summary/').status_code, 200)
        self.assertApplied(self.push_one(self.delete('entries', self.old_entry.id), workspace=self.shop))
        self.assertEqual(self.alice_client.delete(f'{BOOKS}{locked.id}/').status_code, 200)
        self.assertEqual(self.entitlements()['locks'], [])


class SeatDowngradeTests(BillingTestCase):
    """Business with two staff, then back to Free, which has no seats."""

    def setUp(self):
        super().setUp()
        self.subscription = grant_plan(self.alice, 'business')
        self.book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.bob_seat = Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_ADMIN)
        self.carol = type(self.bob).objects.create_user(email='carol@example.com', password='pass-12345')
        self.carol_seat = Membership.objects.create(workspace=self.shop, user=self.carol, role=Membership.ROLE_ADMIN)
        self.age([self.bob_seat, self.carol_seat])

    def entry(self):
        return self.upsert('entries', cashbook_id=str(self.book.id), entry_type='cash_in', amount_minor=100,
                           entry_date='2026-10-01')

    def test_staff_lose_write_access_but_not_read_access_and_the_owner_loses_nothing(self):
        self.assertApplied(self.push_one(self.entry(), self.bob_client, self.shop))
        self.subscription.delete()

        self.assertPlanLimit(self.push_one(self.entry(), self.bob_client, self.shop), F.TEAM_SEATS, 'locked')
        response = self.bob_client.post(f'{BOOKS}{self.book.id}/entries/', {
            'entry_type': 'cash_in', 'amount_minor': 100, 'entry_date': '2026-10-01',
        })
        self.assertEqual((response.status_code, response.data['feature']), (402, 'team.seats'))
        pulled = self.bob_client.get('/api/v1/sync/pull/', {'workspace': str(self.shop.id), 'since': 0})
        self.assertEqual(len(pulled.data['changes']['entries']), 1)

        self.assertApplied(self.push_one(self.entry(), self.alice_client, self.shop))
        self.assertTrue(Membership.objects.filter(pk=self.bob_seat.pk).exists(), 'nobody is removed')

    def test_the_owner_chooses_who_keeps_a_seat(self):
        self.subscription.delete()
        set_limit(self.alice, F.TEAM_SEATS, 1)
        self.assertApplied(self.push_one(self.entry(), self.bob_client, self.shop))
        self.assertPlanLimit(self.push_one(self.entry(), client_for(self.carol), self.shop), F.TEAM_SEATS, 'locked')

        choices = self.alice_client.get(KEEP).data
        seats = [choice for choice in choices if choice['feature'] == 'team.seats'][0]
        self.assertEqual([item['label'] for item in seats['items']], ['bob@example.com', 'carol@example.com'])
        response = self.alice_client.put(
            f'{KEEP}team.seats/', {'scope': str(self.shop.id), 'ids': [str(self.carol_seat.id)]}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertApplied(self.push_one(self.entry(), client_for(self.carol), self.shop))
        self.assertPlanLimit(self.push_one(self.entry(), self.bob_client, self.shop), F.TEAM_SEATS, 'locked')
        # Staff cannot choose for the owner, even for the workspace they work in
        self.assertEqual(self.bob_client.put(
            f'{KEEP}team.seats/', {'scope': str(self.shop.id), 'ids': [str(self.bob_seat.id)]}, format='json',
        ).status_code, 400)


class GateHelperTests(BillingTestCase):
    def test_check_limit_wants_the_right_kind_of_subject(self):
        with self.assertRaises(TypeError):
            gates.check_limit(self.alice, F.PERSONAL_ACCOUNTS)
        with self.assertRaises(TypeError):
            gates.check_limit(self.shop, F.BUSINESS_WORKSPACES)

    def test_allows_answers_without_refusing(self):
        self.assertFalse(gates.allows(self.shop, F.REPORTS_EXCEL))
        grant_plan(self.alice, 'plus')
        self.assertTrue(gates.allows(self.shop, F.REPORTS_EXCEL))
        self.assertFalse(gates.allows(self.bob, F.REPORTS_EXCEL))

    def test_zero_cannot_be_chosen_from(self):
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_STAFF)
        with self.assertRaisesMessage(Exception, 'does not include'):
            locks.choose(self.alice, rule_for(F.TEAM_SEATS), self.shop, 0, 30, [])
