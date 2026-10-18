import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from billing.catalog.keys import F
from billing.models import BillingSettings, FunnelEvent, UsageCounter
from billing.testing import grant_plan, set_limit, set_mode, set_plan_value
from cashbook.models import CashBook, CashBookAdditionalMember, EntryBills
from ledger_personal.models import Account, Budget, Category
from sync.models import SyncMutation
from workspaces.models import Membership, Workspace
from workspaces.services import create_workspace

from .base import BillingTestCase, User, client_for

BOOKS = '/api/v1/cashbooks/'
WORKSPACES = '/api/v1/workspaces/'


class AccountLimitTests(BillingTestCase):
    """Free allows 4 accounts in use; the personal workspace starts with one."""

    def fill(self):
        for number in range(3):
            self.assertApplied(self.push_one(self.account(f'Account {number}')))

    def test_the_fifth_account_is_refused_and_nothing_is_stored(self):
        self.fill()
        mutation = self.account('One too many')
        result = self.push_one(mutation)
        self.assertPlanLimit(result, F.PERSONAL_ACCOUNTS, 'limit')
        self.assertEqual(result['error']['meta'], {
            'feature': 'personal.accounts', 'reason': 'limit', 'limit': 4, 'current': 4,
            'plan': 'free', 'upgrade_to': 'plus',
        })
        self.assertIsNone(result['row'])
        self.assertFalse(Account.all_objects.filter(id=mutation['row_id']).exists())
        self.assertEqual(Account.objects.filter(workspace=self.personal).count(), 4)

    def test_a_refusal_is_remembered_and_noted_once(self):
        self.fill()
        mutation = self.account('One too many')
        first = self.push_one(mutation)
        again = self.push_one(mutation)
        self.assertEqual(first['error'], again['error'])
        self.assertEqual(SyncMutation.objects.get(id=mutation['id']).error_meta['feature'], 'personal.accounts')
        hits = FunnelEvent.objects.filter(kind=FunnelEvent.KIND_LIMIT_HIT)
        self.assertEqual(
            [(hit.user, hit.feature_key, hit.plan_code, hit.enforced) for hit in hits],
            [(self.alice, 'personal.accounts', 'free', True)],
        )

    def test_one_batch_cannot_slip_past_the_limit(self):
        results = self.push(self.alice_client, self.personal, *[self.account(f'A{n}') for n in range(5)])
        self.assertEqual([r['status'] for r in results], ['applied'] * 3 + ['rejected'] * 2)
        self.assertEqual(Account.objects.filter(workspace=self.personal).count(), 4)

    def test_archived_accounts_do_not_count_and_coming_back_needs_room(self):
        self.fill()
        old = self.account('Old bank')
        self.assertPlanLimit(self.push_one(old), F.PERSONAL_ACCOUNTS)

        first = Account.objects.filter(workspace=self.personal).order_by('created_at').first()
        self.assertApplied(self.push_one(self.upsert('accounts', str(first.id), is_archived=True)))
        self.assertApplied(self.push_one(self.account('Replacement')))
        # Full again: the archived one cannot come back until something else goes
        self.assertPlanLimit(
            self.push_one(self.upsert('accounts', str(first.id), is_archived=False)), F.PERSONAL_ACCOUNTS,
        )
        self.assertTrue(Account.objects.get(id=first.id).is_archived)
        # Renaming it while archived is not a matter for the limit
        self.assertApplied(self.push_one(self.upsert('accounts', str(first.id), name='Old cash')))

    def test_an_account_created_archived_takes_no_place(self):
        self.fill()
        self.assertApplied(self.push_one(self.account('Closed long ago', is_archived=True)))

    def test_deleting_always_works_and_frees_a_place(self):
        self.fill()
        gone = Account.objects.filter(workspace=self.personal).order_by('-created_at').first()
        self.assertApplied(self.push_one(self.delete('accounts', gone.id)))
        self.assertApplied(self.push_one(self.account('In its place')))

    def test_a_paid_plan_or_a_raised_limit_lets_it_through(self):
        self.fill()
        self.assertPlanLimit(self.push_one(self.account('Fifth')), F.PERSONAL_ACCOUNTS)
        set_plan_value('free', F.PERSONAL_ACCOUNTS, limit=5)
        self.assertApplied(self.push_one(self.account('Fifth')))
        self.assertPlanLimit(self.push_one(self.account('Sixth')), F.PERSONAL_ACCOUNTS)
        set_limit(self.alice, F.PERSONAL_ACCOUNTS, 6, mode='raise')
        self.assertApplied(self.push_one(self.account('Sixth')))
        grant_plan(self.alice, 'plus')
        for number in range(5):
            self.assertApplied(self.push_one(self.account(f'More {number}')))

    def test_someone_elses_limit_is_their_own(self):
        self.fill()
        bob_personal = Workspace.objects.get(owner=self.bob, kind='personal')
        self.assertApplied(self.push_one(self.account('Bob wallet'), self.bob_client, bob_personal))


class EnforcementModeTests(BillingTestCase):
    def over_the_limit(self):
        return self.push(self.alice_client, self.personal, *[self.account(f'A{n}') for n in range(5)])

    def test_log_only_allows_everything_and_records_what_it_would_have_refused(self):
        set_mode(BillingSettings.MODE_LOG_ONLY)
        self.assertEqual([r['status'] for r in self.over_the_limit()], ['applied'] * 5)
        hits = FunnelEvent.objects.filter(kind=FunnelEvent.KIND_LIMIT_HIT)
        self.assertEqual([hit.enforced for hit in hits], [False, False])

    def test_off_allows_everything_silently(self):
        set_mode(BillingSettings.MODE_OFF)
        self.assertEqual([r['status'] for r in self.over_the_limit()], ['applied'] * 5)
        self.assertFalse(FunnelEvent.objects.exists())

    def test_enforce_is_the_default(self):
        self.assertEqual(BillingSettings.load().enforcement_mode, BillingSettings.MODE_ENFORCE)
        self.assertEqual([r['status'] for r in self.over_the_limit()], ['applied'] * 3 + ['rejected'] * 2)


class CategoryLimitTests(BillingTestCase):
    """Free allows 10 categories of the user's own, on top of the ones every account starts with."""

    def category(self, name, **data):
        return self.upsert('categories', name=name, kind='expense', **data)

    def test_starting_categories_are_marked_and_do_not_count(self):
        starting = Category.objects.filter(workspace=self.personal)
        self.assertEqual(starting.count(), len(Category.DEFAULT_EXPENSE) + len(Category.DEFAULT_INCOME))
        self.assertTrue(all(category.is_default for category in starting))

        results = self.push(self.alice_client, self.personal, *[self.category(f'Mine {n}') for n in range(11)])
        self.assertEqual([r['status'] for r in results], ['applied'] * 10 + ['rejected'])
        self.assertPlanLimit(results[-1], F.PERSONAL_CUSTOM_CATEGORIES, 'limit')
        self.assertEqual(results[-1]['error']['meta']['limit'], 10)
        self.assertFalse(results[0]['row']['is_default'])

    def test_a_client_cannot_mark_its_own_category_as_a_starting_one(self):
        results = self.push(
            self.alice_client, self.personal,
            *[self.category(f'Mine {n}', is_default=True) for n in range(11)],
        )
        self.assertEqual([r['status'] for r in results], ['applied'] * 10 + ['rejected'])
        self.assertFalse(Category.objects.filter(workspace=self.personal, name__startswith='Mine', is_default=True).exists())

    def test_renaming_or_deleting_a_starting_category_changes_nothing(self):
        food = Category.objects.get(workspace=self.personal, name='Food')
        result = self.push_one(self.upsert('categories', str(food.id), name='Groceries', is_default=False))
        self.assertApplied(result)
        self.assertTrue(result['row']['is_default'])
        self.assertApplied(self.push_one(self.delete('categories', food.id)))
        results = self.push(self.alice_client, self.personal, *[self.category(f'Mine {n}') for n in range(11)])
        self.assertEqual([r['status'] for r in results], ['applied'] * 10 + ['rejected'])


class BudgetFlagTests(BillingTestCase):
    def budget(self, month=None):
        food = Category.objects.get(workspace=self.personal, name='Food')
        return self.upsert('budgets', category_id=str(food.id), amount_minor=800000, currency='BDT', month=month)

    def test_free_has_recurring_budgets_but_not_one_month_overrides(self):
        self.assertApplied(self.push_one(self.budget()))
        override = self.budget('2026-11-01')
        result = self.push_one(override)
        self.assertPlanLimit(result, F.BUDGET_MONTH_OVERRIDE, 'feature')
        self.assertEqual(result['error']['meta']['upgrade_to'], 'plus')
        self.assertFalse(Budget.all_objects.filter(id=override['row_id']).exists())

    def test_plus_has_overrides_and_they_turn_read_only_afterwards(self):
        subscription = grant_plan(self.alice, 'plus')
        override = self.budget('2026-11-01')
        self.assertApplied(self.push_one(override))

        subscription.delete()
        edit = self.upsert('budgets', override['row_id'], amount_minor=900000)
        self.assertPlanLimit(self.push_one(edit), F.BUDGET_MONTH_OVERRIDE)
        self.assertEqual(Budget.objects.get(id=override['row_id']).amount_minor, 800000)
        # It is still there, and can be removed
        self.assertApplied(self.push_one(self.delete('budgets', override['row_id'])))


class CashbookLimitTests(BillingTestCase):
    """Free allows 2 cashbooks in each workspace."""

    def book(self, name):
        return self.upsert('cashbooks', book_name=name, currency='BDT')

    def test_sync_counts_each_workspace_on_its_own(self):
        results = self.push(self.alice_client, self.shop, *[self.book(f'Till {n}') for n in range(3)])
        self.assertEqual([r['status'] for r in results], ['applied', 'applied', 'rejected'])
        self.assertPlanLimit(results[2], F.BUSINESS_CASHBOOKS, 'limit')
        self.assertApplied(self.push_one(self.book('Home book')))

    def test_rest_create_is_refused_with_402(self):
        for number in range(2):
            response = self.alice_client.post(BOOKS, {'book_name': f'Till {number}', 'workspace': str(self.shop.id)})
            self.assertEqual(response.status_code, 201, response.data)
        response = self.alice_client.post(BOOKS, {'book_name': 'Till 3', 'workspace': str(self.shop.id)})
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual(
            (response.data['code'], response.data['feature'], response.data['limit'], response.data['upgrade_to']),
            ('plan_limit', 'business.cashbooks', 2, 'plus'),
        )
        self.assertEqual(CashBook.objects.filter(workspace=self.shop).count(), 2)
        self.assertTrue(FunnelEvent.objects.filter(kind='limit_hit', feature_key='business.cashbooks').exists())

    def test_a_team_member_is_judged_on_the_owners_plan(self):
        # Bob pays for Business himself; in Alice's shop that changes nothing
        grant_plan(self.bob, 'business')
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_ADMIN)
        set_limit(self.alice, F.TEAM_SEATS, 1)
        results = self.push(self.bob_client, self.shop, *[self.book(f'Till {n}') for n in range(3)])
        self.assertEqual([r['status'] for r in results], ['applied', 'applied', 'rejected'])
        self.assertEqual(results[2]['error']['meta']['plan'], 'free')


class WorkspaceLimitTests(BillingTestCase):
    """Free allows one business; Alice already has her shop."""

    def test_a_second_business_is_refused_with_402_never_403(self):
        response = self.alice_client.post(WORKSPACES, {'name': 'Second shop'})
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual(response.data['code'], 'plan_limit')
        self.assertEqual((response.data['feature'], response.data['limit'], response.data['current']),
                         ('business.workspaces', 1, 1))
        self.assertEqual(Workspace.objects.filter(owner=self.alice, kind='business').count(), 1)

    def test_bob_can_still_make_his_first(self):
        self.assertEqual(self.bob_client.post(WORKSPACES, {'name': 'Bob shop'}).status_code, 201)
        self.assertEqual(self.bob_client.post(WORKSPACES, {'name': 'Bob shop 2'}).status_code, 402)

    def test_the_demo_business_does_not_count(self):
        self.assertEqual(self.bob_client.post(f'{WORKSPACES}demo/').status_code, 201)
        self.assertEqual(self.bob_client.post(WORKSPACES, {'name': 'Bob shop'}).status_code, 201)

    def test_retrying_a_create_that_succeeded_is_not_refused(self):
        workspace_id = '0192f3a1-7b2c-7d4e-8f00-123456789abc'
        first = self.bob_client.post(WORKSPACES, {'id': workspace_id, 'name': 'Bob shop'})
        again = self.bob_client.post(WORKSPACES, {'id': workspace_id, 'name': 'Bob shop'})
        self.assertEqual((first.status_code, again.status_code), (201, 200))

    def test_plus_allows_two_and_a_deleted_business_frees_its_place(self):
        grant_plan(self.alice, 'plus')
        self.assertEqual(self.alice_client.post(WORKSPACES, {'name': 'Second shop'}).status_code, 201)
        self.assertEqual(self.alice_client.post(WORKSPACES, {'name': 'Third shop'}).status_code, 402)
        self.assertEqual(self.alice_client.delete(f'{WORKSPACES}{self.shop.id}/').status_code, 204)
        self.assertEqual(self.alice_client.post(WORKSPACES, {'name': 'Third shop'}).status_code, 201)


class SeatLimitTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.book = CashBook.objects.create(workspace=self.shop, book_name='Till')

    def test_a_cashbook_grant_never_takes_a_seat(self):
        # Seats are taken by invitations (workspaces/tests.py); a grant only reaches people already on the team
        grant_plan(self.alice, 'business')
        response = self.alice_client.post(
            f'{BOOKS}{self.book.id}/add_member/', {'member': str(self.bob.id), 'role': 'editor'},
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(CashBookAdditionalMember.objects.filter(member=self.bob).exists())
        self.assertFalse(Membership.objects.filter(workspace=self.shop, user=self.bob).exists())


class CustomFieldTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.entries = f'{BOOKS}{self.book.id}/entries/'
        self.entry = {'entry_type': 'cash_in', 'amount_minor': 5000, 'entry_date': '2026-10-01'}

    def test_custom_fields_are_business_only_on_every_way_in(self):
        created = self.alice_client.post(self.entries, self.entry, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        entry_id = created.data['id']
        field = {'field_name': 'Invoice', 'field_value': 'INV-7'}

        grant_plan(self.alice, 'plus')
        with_fields = {**self.entry, 'extra_fields_data': [field]}
        self.assertEqual(self.alice_client.post(self.entries, with_fields, format='json').status_code, 402)
        self.assertEqual(
            self.alice_client.patch(f'{self.entries}{entry_id}/', {'extra_fields_data': [field]}, format='json').status_code,
            402,
        )
        response = self.alice_client.post(f'{self.entries}{entry_id}/add-extra-field/', field, format='json')
        self.assertEqual((response.status_code, response.data['feature']), (402, 'entries.custom_fields'))

        grant_plan(self.alice, 'business')
        self.assertEqual(self.alice_client.post(self.entries, with_fields, format='json').status_code, 201)
        self.assertEqual(
            self.alice_client.post(f'{self.entries}{entry_id}/add-extra-field/', field, format='json').status_code, 201,
        )


class AttachmentStorageTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        settings = override_settings(MEDIA_ROOT=self.media)
        settings.enable()
        self.addCleanup(settings.disable)
        self.book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.entries = f'{BOOKS}{self.book.id}/entries/'
        self.entry = {'entry_type': 'cash_in', 'amount_minor': 5000, 'entry_date': '2026-10-01'}

    def bill(self, size=2048, name='bill.pdf'):
        return SimpleUploadedFile(name, b'x' * size, content_type='application/pdf')

    def used(self):
        counter = UsageCounter.objects.filter(user=self.alice, feature_key='storage.attachments_mb').first()
        return counter.used if counter else 0

    def test_free_has_no_attachments_and_the_entry_is_not_half_saved(self):
        response = self.alice_client.post(self.entries, {**self.entry, 'bills': self.bill()}, format='multipart')
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual(response.data['feature'], 'storage.attachments_mb')
        self.assertFalse(self.book.entry_set.exists())
        self.assertEqual(self.used(), 0)
        # Without a file the entry is fine
        self.assertEqual(self.alice_client.post(self.entries, self.entry, format='json').status_code, 201)

    def test_plus_counts_what_is_stored_and_gives_it_back_on_removal(self):
        grant_plan(self.alice, 'plus')
        response = self.alice_client.post(self.entries, {**self.entry, 'bills': self.bill(2048)}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        entry_id = response.data['id']
        self.assertEqual(self.used(), 2048)
        self.assertEqual(self.entitlements()['features']['storage.attachments_mb']['used_bytes'], 2048)

        added = self.alice_client.post(
            f'{self.entries}{entry_id}/add-bill/', {'bill_file': self.bill(1000)}, format='multipart',
        )
        self.assertEqual(added.status_code, 201, added.data)
        self.assertEqual(self.used(), 3048)
        bill = EntryBills.objects.get(size_bytes=1000)
        self.assertEqual(self.alice_client.delete(f'{self.entries}{entry_id}/remove-bill/{bill.id}/').status_code, 200)
        self.assertEqual(self.used(), 2048)

    def test_the_file_that_does_not_fit_is_refused_together_with_the_ones_before_it(self):
        grant_plan(self.alice, 'plus')
        set_limit(self.alice, F.STORAGE_ATTACHMENTS_MB, 1)
        entry_id = self.alice_client.post(self.entries, self.entry, format='json').data['id']
        megabyte = 1024 * 1024
        response = self.alice_client.post(
            f'{self.entries}{entry_id}/add-bill/',
            {'bill_file': [self.bill(megabyte - 10, 'a.pdf'), self.bill(100, 'b.pdf')]}, format='multipart',
        )
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual(self.used(), 0)
        self.assertFalse(EntryBills.objects.exists())
        self.assertEqual(self.alice_client.post(
            f'{self.entries}{entry_id}/add-bill/', {'bill_file': self.bill(megabyte - 10)}, format='multipart',
        ).status_code, 201)


class SizeLimitTests(BillingTestCase):
    """A limit on how much is asked for at once (months of a trend), not on a count of rows."""

    def test_within_the_limit_passes_and_past_it_is_refused(self):
        from billing import gates
        gates.check_within(self.personal, F.INSIGHTS_TREND_MONTHS, 6)
        with self.assertRaises(gates.PlanLimit) as refused:
            gates.check_within(self.personal, F.INSIGHTS_TREND_MONTHS, 7)
        self.assertEqual(
            {key: refused.exception.meta[key] for key in ('feature', 'reason', 'limit', 'current')},
            {'feature': 'insights.trend_months', 'reason': 'limit', 'limit': 6, 'current': 7},
        )
        grant_plan(self.alice, 'plus')
        gates.check_within(self.personal, F.INSIGHTS_TREND_MONTHS, 24)

    def test_unlimited_passes_anything(self):
        from billing import gates
        set_limit(self.alice, F.INSIGHTS_TREND_MONTHS, None)
        gates.check_within(self.alice, F.INSIGHTS_TREND_MONTHS, 10_000)
        self.assertIsNone(gates.limit_in_force(self.alice, F.INSIGHTS_TREND_MONTHS))

    def test_the_limit_in_force_follows_the_enforcement_mode(self):
        from billing import gates
        self.assertEqual(gates.limit_in_force(self.personal, F.INSIGHTS_TREND_MONTHS), 6)
        for mode in (BillingSettings.MODE_LOG_ONLY, BillingSettings.MODE_OFF):
            set_mode(mode)
            self.assertIsNone(gates.limit_in_force(self.personal, F.INSIGHTS_TREND_MONTHS), mode)
            gates.check_within(self.personal, F.INSIGHTS_TREND_MONTHS, 24)
