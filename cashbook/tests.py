from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from billing.testing import grant_plan
from workspaces.models import Membership
from workspaces.services import create_workspace, get_personal_workspace
from .models import CashBook, Entry, EntryCategory
from spendroo.money import format_money, to_major

User = get_user_model()

BOOKS = '/api/v1/cashbooks/'


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


class MoneyTests(TestCase):
    def test_minor_units_convert_by_currency_exponent(self):
        self.assertEqual(to_major(12345, 'BDT'), Decimal('123.45'))
        self.assertEqual(to_major(12345, 'JPY'), Decimal('12345'))
        self.assertEqual(to_major(12345, 'KWD'), Decimal('12.345'))
        self.assertEqual(format_money(123450, 'BDT'), 'BDT 1,234.50')
        self.assertEqual(format_money(-500, 'JPY'), 'JPY -500')


class CashBookTestCase(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(email='alice@example.com', password='pass-12345')
        self.bob = User.objects.create_user(email='bob@example.com', password='pass-12345')
        self.alice_client = client_for(self.alice)
        self.bob_client = client_for(self.bob)
        # These tests are about cashbooks, roles and reports, not about what a
        # plan allows (billing/tests cover that), so the owner gets everything
        grant_plan(self.alice, 'business')

    def create_book(self, client, **data):
        response = client.post(BOOKS, {'book_name': 'Shop cash', **data})
        self.assertEqual(response.status_code, 201, response.data)
        return CashBook.objects.get(id=response.data['data']['id'])

    def add_entry(self, client, book, **data):
        payload = {'entry_type': 'cash_in', 'amount_minor': 10000, 'entry_date': '2026-10-01', **data}
        return client.post(f'{BOOKS}{book.id}/entries/', payload)


class CashBookAPITests(CashBookTestCase):
    def test_book_defaults_to_personal_workspace_and_its_currency(self):
        book = self.create_book(self.alice_client)
        self.assertEqual(book.workspace, get_personal_workspace(self.alice))
        self.assertEqual(book.currency, 'BDT')
        self.assertEqual(book.created_by, self.alice)
        self.assertEqual(book.entrycategory_set.count(), len(EntryCategory.GENERAL_CATEGORIES))
        self.assertEqual(book.entrycategory_set.first().workspace_id, book.workspace_id)

    def test_duplicate_name_rejected_within_workspace_only(self):
        self.create_book(self.alice_client)
        self.assertEqual(self.alice_client.post(BOOKS, {'book_name': 'shop CASH'}).status_code, 400)
        # Same name in another workspace is fine
        shop = create_workspace(owner=self.alice, name='Shop')
        self.create_book(self.alice_client, workspace=str(shop.id))

    def test_currency_is_fixed_after_creation(self):
        book = self.create_book(self.alice_client, currency='USD')
        response = self.alice_client.patch(f'{BOOKS}{book.id}/', {'currency': 'BDT'})
        self.assertEqual(response.status_code, 400)

    def test_entries_use_integer_minor_units(self):
        book = self.create_book(self.alice_client, currency='USD')
        response = self.add_entry(self.alice_client, book, amount_minor=12550)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['amount_minor'], 12550)
        self.assertEqual(response.data['currency'], 'USD')
        self.assertEqual(response.data['source'], 'manual')
        self.add_entry(self.alice_client, book, entry_type='cash_out', amount_minor=2550)

        balance = self.alice_client.get(f'{BOOKS}{book.id}/balance/').data
        self.assertEqual(balance, {
            'currency': 'USD', 'balance_minor': 10000,
            'total_cash_in_minor': 12550, 'total_cash_out_minor': 2550,
        })

    def test_fractional_and_non_positive_amounts_rejected(self):
        book = self.create_book(self.alice_client)
        for amount in ('10.50', 0, -5):
            response = self.add_entry(self.alice_client, book, amount_minor=amount)
            self.assertEqual(response.status_code, 400, amount)

    def test_entry_delete_is_a_tombstone(self):
        book = self.create_book(self.alice_client)
        entry_id = self.add_entry(self.alice_client, book).data['id']

        response = self.alice_client.delete(f'{BOOKS}{book.id}/entries/{entry_id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['updated_balance_minor'], 0)

        entry = Entry.all_objects.get(id=entry_id)
        self.assertIsNotNone(entry.deleted_at)
        self.assertEqual(entry.version, 2)
        self.assertEqual(self.alice_client.get(f'{BOOKS}{book.id}/entries/').data['results'], [])

    def test_entry_update_bumps_version(self):
        book = self.create_book(self.alice_client)
        entry_id = self.add_entry(self.alice_client, book).data['id']
        response = self.alice_client.patch(f'{BOOKS}{book.id}/entries/{entry_id}/', {'amount_minor': 500})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['version'], 2)
        self.assertEqual(response.data['amount_minor'], 500)

    def test_book_delete_is_a_tombstone(self):
        book = self.create_book(self.alice_client)
        self.assertEqual(self.alice_client.delete(f'{BOOKS}{book.id}/').status_code, 200)
        self.assertEqual(self.alice_client.get(BOOKS).data['results'], [])
        self.assertTrue(CashBook.all_objects.filter(id=book.id).exists())

    def test_deleted_category_is_restored_on_recreate(self):
        book = self.create_book(self.alice_client)
        url = f'{BOOKS}{book.id}/categories/'
        category_id = self.alice_client.post(url, {'category_name': 'Rent'}).data['category']['id']
        self.assertEqual(self.alice_client.delete(f'{url}{category_id}/').status_code, 200)
        response = self.alice_client.post(url, {'category_name': 'Rent'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['category']['id'], category_id)

    def test_stats(self):
        book = self.create_book(self.alice_client)
        category = book.entrycategory_set.get(category_name='Food')
        self.add_entry(self.alice_client, book, entry_type='cash_out', amount_minor=2500, category=str(category.id))
        stats = f'{BOOKS}{book.id}/stats/'

        summary = self.alice_client.get(f'{stats}summary/').data
        self.assertEqual(summary['balance_minor'], -2500)
        self.assertEqual(summary['currency'], 'BDT')

        breakdown = self.alice_client.get(f'{stats}category-breakdown/').data['categories']
        self.assertEqual(breakdown[0]['cash_out_minor'], 2500)

        analytics = self.alice_client.get(f'{stats}analytics/').data
        self.assertEqual(analytics['summary']['total_cash_out_minor'], 2500)
        self.assertEqual(analytics['category_spending']['values'], [2500])


class CrossTenantTests(CashBookTestCase):
    """Bob must never see or touch anything in Alice's workspace."""

    def setUp(self):
        super().setUp()
        self.book = self.create_book(self.alice_client)
        self.entry_id = self.add_entry(self.alice_client, self.book).data['id']
        self.category = self.book.entrycategory_set.first()
        self.url = f'{BOOKS}{self.book.id}/'

    def assertDenied(self, response):
        self.assertIn(response.status_code, (403, 404), response.data if hasattr(response, 'data') else None)

    def test_foreign_book_is_invisible(self):
        self.assertEqual(self.bob_client.get(BOOKS).data['results'], [])
        self.assertDenied(self.bob_client.get(self.url))
        self.assertDenied(self.bob_client.patch(self.url, {'book_name': 'Hijacked'}))
        self.assertDenied(self.bob_client.delete(self.url))
        self.assertDenied(self.bob_client.get(f'{self.url}balance/'))
        self.assertDenied(self.bob_client.post(f'{self.url}add_member/', {'member': str(self.bob.id), 'role': 'admin'}))
        self.assertDenied(self.bob_client.delete(f'{BOOKS}bulk-delete/', {'ids': [str(self.book.id)]}, format='json'))
        self.book.refresh_from_db()
        self.assertEqual(self.book.book_name, 'Shop cash')
        self.assertIsNone(self.book.deleted_at)

    def test_foreign_entries_are_unreachable(self):
        entries = f'{self.url}entries/'
        self.assertDenied(self.bob_client.get(entries))
        self.assertDenied(self.add_entry(self.bob_client, self.book))
        self.assertDenied(self.bob_client.get(f'{entries}{self.entry_id}/'))
        self.assertDenied(self.bob_client.patch(f'{entries}{self.entry_id}/', {'amount_minor': 1}))
        self.assertDenied(self.bob_client.delete(f'{entries}{self.entry_id}/'))
        entry = Entry.objects.get(id=self.entry_id)
        self.assertEqual(entry.amount_minor, 10000)

    def test_foreign_categories_methods_and_stats_are_unreachable(self):
        for path in ('categories/', 'payment-methods/', 'stats/summary/', 'stats/analytics/',
                     'stats/category-breakdown/', 'stats/payment-method-breakdown/'):
            self.assertDenied(self.bob_client.get(f'{self.url}{path}'))
        self.assertDenied(self.bob_client.post(f'{self.url}categories/', {'category_name': 'X'}))
        self.assertDenied(self.bob_client.delete(f'{self.url}categories/{self.category.id}/'))

    def test_cannot_attach_foreign_category_to_own_entry(self):
        bob_book = self.create_book(self.bob_client)
        response = self.add_entry(self.bob_client, bob_book, category=str(self.category.id))
        self.assertEqual(response.status_code, 400)

    def test_cannot_create_book_in_foreign_workspace(self):
        response = self.bob_client.post(BOOKS, {'book_name': 'Mine now', 'workspace': str(self.book.workspace_id)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CashBook.objects.filter(workspace=self.book.workspace).count(), 1)


class RoleTests(CashBookTestCase):
    def setUp(self):
        super().setUp()
        self.shop = create_workspace(owner=self.alice, name='Shop')
        self.till = self.create_book(self.alice_client, book_name='Till', workspace=str(self.shop.id))
        self.safe = self.create_book(self.alice_client, book_name='Safe', workspace=str(self.shop.id))

    def test_staff_sees_only_granted_books(self):
        response = self.alice_client.post(
            f'{BOOKS}{self.till.id}/add_member/', {'member': str(self.bob.id), 'role': 'editor'}
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.shop.role_of(self.bob), Membership.ROLE_STAFF)

        names = [b['book_name'] for b in self.bob_client.get(BOOKS).data['results']]
        self.assertEqual(names, ['Till'])
        self.assertEqual(self.add_entry(self.bob_client, self.till).status_code, 201)
        self.assertIn(self.add_entry(self.bob_client, self.safe).status_code, (403, 404))
        # An editor cannot manage categories or delete the book
        self.assertEqual(self.bob_client.post(f'{BOOKS}{self.till.id}/categories/', {'category_name': 'X'}).status_code, 403)
        self.assertEqual(self.bob_client.delete(f'{BOOKS}{self.till.id}/').status_code, 403)

    def test_removed_staff_loses_access(self):
        grant_id = self.alice_client.post(
            f'{BOOKS}{self.till.id}/add_member/', {'member': str(self.bob.id), 'role': 'editor'}
        ).data['data']['id']
        self.assertEqual(self.alice_client.delete(f'{BOOKS}{self.till.id}/members/{grant_id}/').status_code, 200)
        self.assertEqual(self.bob_client.get(BOOKS).data['results'], [])
        self.assertIn(self.bob_client.get(f'{BOOKS}{self.till.id}/entries/').status_code, (403, 404))

    def test_workspace_viewer_reads_every_book_but_cannot_write(self):
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_VIEWER)
        self.assertEqual(len(self.bob_client.get(BOOKS).data['results']), 2)
        self.assertEqual(self.bob_client.get(f'{BOOKS}{self.safe.id}/entries/').status_code, 200)
        self.assertEqual(self.add_entry(self.bob_client, self.safe).status_code, 403)
        self.assertEqual(self.bob_client.patch(f'{BOOKS}{self.safe.id}/', {'book_name': 'X'}).status_code, 403)

    def test_workspace_admin_has_full_access_except_delete(self):
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_ADMIN)
        self.assertEqual(self.add_entry(self.bob_client, self.safe).status_code, 201)
        self.assertEqual(self.bob_client.post(f'{BOOKS}{self.safe.id}/categories/', {'category_name': 'X'}).status_code, 201)
        self.create_book(self.bob_client, book_name='Bank', workspace=str(self.shop.id))
        self.assertEqual(self.bob_client.delete(f'{BOOKS}{self.safe.id}/').status_code, 403)

    def test_workspace_filter(self):
        self.create_book(self.alice_client, book_name='Home')
        response = self.alice_client.get(f'{BOOKS}?workspace={self.shop.id}')
        self.assertEqual(sorted(b['book_name'] for b in response.data['results']), ['Safe', 'Till'])


class EditHistoryTests(CashBookTestCase):
    def test_every_change_is_kept_with_its_author(self):
        book = self.create_book(self.alice_client)
        entry_id = self.add_entry(self.alice_client, book, amount_minor=10000).data['id']
        self.alice_client.patch(f'{BOOKS}{book.id}/entries/{entry_id}/', {'amount_minor': 500})
        self.alice_client.delete(f'{BOOKS}{book.id}/entries/{entry_id}/')

        events = list(Entry.pgh_event_model.objects.filter(pgh_obj_id=entry_id).order_by('pgh_id'))
        self.assertEqual([e.pgh_label for e in events], ['insert', 'update', 'update'])
        self.assertEqual([e.amount_minor for e in events], [10000, 500, 500])
        self.assertIsNotNone(events[2].deleted_at)
        for event in events:
            self.assertEqual(event.pgh_context.metadata['user'], str(self.alice.id))

    def test_history_is_written_by_the_database_not_the_orm(self):
        book = self.create_book(self.alice_client)
        entry_id = self.add_entry(self.alice_client, book).data['id']
        # A bulk update skips save() and signals; the trigger still records it
        Entry.objects.filter(id=entry_id).update(amount_minor=777)
        self.assertEqual(Entry.pgh_event_model.objects.filter(pgh_obj_id=entry_id).latest('pgh_id').amount_minor, 777)
