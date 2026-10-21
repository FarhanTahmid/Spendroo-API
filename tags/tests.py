import uuid

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from audit.tests import HistoryTestCase
from cashbook.models import CashBook, CashBookAdditionalMember, Entry
from ledger_personal.models import Account, Transaction
from ledger_personal.tests import AccountTestCase
from spendroo.merchant import normalise_merchant, set_merchant, tidy_merchant
from sync.admin import TombstoneOnDeleteAdmin
from sync.registry import TABLES
from sync.tests import SyncTestCase, new_id
from workspaces.models import Membership
from workspaces.services import get_personal_workspace
from .cleaning import MAX_TAGS, clean_tag_ids, tag_ids_matching
from .models import Tag

BOOKS = '/api/v1/cashbooks/'


class MerchantTextTests(SimpleTestCase):
    def test_tidy_trims_and_collapses_spacing(self):
        self.assertEqual(tidy_merchant('  Star   Kabab \n'), 'Star Kabab')
        self.assertEqual(tidy_merchant(None), '')
        self.assertEqual(len(tidy_merchant('x' * 150)), 100)

    def test_the_key_matches_spellings_of_one_merchant(self):
        for spelling in ('Foodpanda', ' FOODPANDA ', 'foodpanda.', 'foodpanda!!'):
            self.assertEqual(normalise_merchant(spelling), 'foodpanda')
        self.assertEqual(normalise_merchant('Food-Panda!'), 'food panda')
        # '&' is part of a name; currency signs are not
        self.assertEqual(normalise_merchant('H&M'), 'h&m')
        self.assertEqual(normalise_merchant('৳ Aarong, Dhanmondi'), 'aarong dhanmondi')
        # Bangla names keep their vowel signs, and full-width letters read as plain ones
        self.assertEqual(normalise_merchant('স্বপ্ন'), 'স্বপ্ন')
        self.assertEqual(normalise_merchant('ＢＫＡＳＨ'), 'bkash')
        self.assertEqual(normalise_merchant(None), '')

    def test_saving_some_fields_keeps_the_key_in_step(self):
        class Row:
            merchant = ' Daraz  '
        row, kwargs = Row(), {'update_fields': ['merchant']}
        set_merchant(row, kwargs)
        self.assertEqual((row.merchant, row.merchant_key), ('Daraz', 'daraz'))
        self.assertEqual(kwargs['update_fields'], {'merchant', 'merchant_key'})
        other = {'update_fields': ['note']}
        set_merchant(row, other)
        self.assertEqual(other['update_fields'], ['note'])
        self.assertEqual(set_merchant(row, {}), None)


class TagCleaningTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.alice = get_user_model().objects.create_user(email='alice@example.com', password='pass-12345')
        self.personal = get_personal_workspace(self.alice)
        self.food = Tag.objects.create(workspace=self.personal, name='Food')
        self.trip = Tag.objects.create(workspace=self.personal, name='Cox trip')

    def test_keeps_live_tags_in_order_once_each(self):
        ids = [str(self.trip.id), str(self.food.id), str(self.trip.id).upper()]
        self.assertEqual(clean_tag_ids(self.personal, ids), [str(self.trip.id), str(self.food.id)])
        self.assertEqual(clean_tag_ids(self.personal, None), [])
        self.assertEqual(clean_tag_ids(self.personal, []), [])

    def test_drops_deleted_unknown_and_foreign_tags(self):
        self.trip.soft_delete()
        from django.contrib.auth import get_user_model
        bob = get_user_model().objects.create_user(email='bob@example.com', password='pass-12345')
        theirs = Tag.objects.create(workspace=get_personal_workspace(bob), name='Food')
        ids = [str(self.food.id), str(self.trip.id), new_id(), str(theirs.id)]
        self.assertEqual(clean_tag_ids(self.personal, ids), [str(self.food.id)])

    def test_refuses_what_is_not_a_list_of_ids(self):
        for value in ('food', {'id': 1}, ['not-an-id'], [None], [str(uuid.uuid4()) for _ in range(MAX_TAGS + 1)]):
            with self.assertRaises(ValidationError, msg=value):
                clean_tag_ids(self.personal, value)
        # Ten different ids are fine, and the same id twice counts once
        self.assertEqual(clean_tag_ids(self.personal, [new_id() for _ in range(MAX_TAGS)]), [])

    def test_search_finds_live_tags_by_part_of_the_name(self):
        self.assertEqual(tag_ids_matching(self.personal, 'COX'), [str(self.trip.id)])
        self.trip.soft_delete()
        self.assertEqual(tag_ids_matching(self.personal, 'cox'), [])
        self.assertEqual(str(self.food), 'Food')


class TagSyncTests(SyncTestCase):
    def tag(self, name='Food', **data):
        return self.upsert('tags', name=name, **data)

    def expense(self, **data):
        cash = Account.objects.get(workspace=self.personal, name='Cash')
        return self.upsert('transactions', account_id=str(cash.id), kind='expense', amount_minor=-5000,
                           occurred_on='2026-10-20', **data)

    def test_tags_come_before_the_rows_that_carry_them(self):
        names = list(TABLES)
        self.assertLess(names.index('tags'), names.index('entries'))
        self.assertLess(names.index('tags'), names.index('transactions'))
        self.assertIsInstance(admin.site._registry[Tag], TombstoneOnDeleteAdmin)

    def test_a_transaction_carries_its_merchant_and_tags_to_another_phone(self):
        food, trip = self.tag('Food', color='#F08A3C'), self.tag('Trip')
        row = self.expense(merchant='  Star   Kabab ', tag_ids=[food['row_id'], trip['row_id']])
        results = self.push(self.alice_client, self.personal, food, trip, row)
        self.assertEqual([r['status'] for r in results], ['applied'] * 3)
        pushed = results[2]['row']
        self.assertEqual((pushed['merchant'], pushed['tag_ids']), ('Star Kabab', [food['row_id'], trip['row_id']]))
        # The key stays on the server
        self.assertNotIn('merchant_key', pushed)
        self.assertEqual(Transaction.objects.get(id=row['row_id']).merchant_key, 'star kabab')

        # A second phone of the same person
        phone = APIClient()
        phone.force_authenticate(self.alice)
        pulled = self.pull(phone, self.personal)
        tags = {str(t['id']): t for t in pulled['changes']['tags']}
        self.assertEqual((tags[food['row_id']]['name'], tags[food['row_id']]['color']), ('Food', '#F08A3C'))
        [transaction] = [t for t in pulled['changes']['transactions'] if str(t['id']) == row['row_id']]
        self.assertEqual((transaction['merchant'], transaction['tag_ids']), ('Star Kabab', [food['row_id'], trip['row_id']]))

        # It edits them; the first phone sees the edit on its next pull
        since = pulled['next_since']
        self.push(phone, self.personal, self.upsert('transactions', row['row_id'], merchant='Star Kabab & Sweets',
                                                    tag_ids=[trip['row_id']]))
        changes = self.pull(self.alice_client, self.personal, since=since)['changes']['transactions']
        self.assertEqual([(c['merchant'], c['tag_ids']) for c in changes], [('Star Kabab & Sweets', [trip['row_id']])])
        self.assertEqual(Transaction.objects.get(id=row['row_id']).merchant_key, 'star kabab & sweets')

    def test_an_old_row_reads_with_no_merchant_and_no_tags(self):
        cash = Account.objects.get(workspace=self.personal, name='Cash')
        old = Transaction.objects.create(account=cash, kind='expense', amount_minor=-100, occurred_on='2026-01-01')
        [row] = [t for t in self.pull(self.alice_client, self.personal)['changes']['transactions'] if str(t['id']) == str(old.id)]
        self.assertEqual((row['merchant'], row['tag_ids']), ('', []))
        # An app that knows nothing of them edits the row and leaves them alone
        tagged = Tag.objects.create(workspace=self.personal, name='Kept')
        old.merchant, old.tag_ids = 'Agora', [str(tagged.id)]
        old.save()
        self.push(self.alice_client, self.personal, self.upsert('transactions', str(old.id), note='Milk'))
        old.refresh_from_db()
        self.assertEqual((old.note, old.merchant, old.tag_ids), ('Milk', 'Agora', [str(tagged.id)]))

    def test_a_tag_deleted_while_the_phone_was_offline_is_dropped_not_refused(self):
        food = Tag.objects.create(workspace=self.personal, name='Food')
        gone = Tag.objects.create(workspace=self.personal, name='Gone')
        gone.soft_delete()
        row = self.expense(tag_ids=[str(gone.id), str(food.id), str(food.id)])
        [result] = self.push(self.alice_client, self.personal, row)
        self.assertEqual((result['status'], result['row']['tag_ids']), ('applied', [str(food.id)]))

        # Later edits that leave the tags alone keep them as they are, a deleted one included
        food.soft_delete()
        [result] = self.push(self.alice_client, self.personal, self.upsert('transactions', row['row_id'], note='x'))
        self.assertEqual(result['row']['tag_ids'], [str(food.id)])
        # Sending the tags again cleans them
        [result] = self.push(self.alice_client, self.personal,
                             self.upsert('transactions', row['row_id'], tag_ids=[str(food.id)]))
        self.assertEqual(result['row']['tag_ids'], [])

    def test_tags_that_are_not_a_list_of_ids_are_invalid(self):
        results = self.push(
            self.alice_client, self.personal,
            self.expense(tag_ids='food'), self.expense(tag_ids=['food']),
            self.expense(tag_ids=[new_id() for _ in range(11)]), self.expense(merchant='x' * 101),
        )
        self.assertEqual([r['error']['code'] for r in results], ['invalid'] * 4)
        # No tags at all reads as none
        [result] = self.push(self.alice_client, self.personal, self.expense(tag_ids=None))
        self.assertEqual((result['status'], result['row']['tag_ids']), ('applied', []))

    def test_another_workspaces_tag_is_dropped(self):
        shop_tag = Tag.objects.create(workspace=self.shop, name='Stock')
        [result] = self.push(self.alice_client, self.personal, self.expense(tag_ids=[str(shop_tag.id)]))
        self.assertEqual(result['row']['tag_ids'], [])

    def test_an_entry_carries_its_party_and_tags(self):
        stock = self.tag('Stock')
        book = self.upsert('cashbooks', book_name='Till', currency='BDT')
        entry = self.upsert('entries', cashbook_id=book['row_id'], entry_type='cash_out', amount_minor=5000,
                            entry_date='2026-10-01', merchant=' Rahim  Traders', tag_ids=[stock['row_id']])
        results = self.push(self.alice_client, self.shop, stock, book, entry)
        self.assertEqual([r['status'] for r in results], ['applied'] * 3)
        self.assertEqual((results[2]['row']['merchant'], results[2]['row']['tag_ids']), ('Rahim Traders', [stock['row_id']]))
        self.assertEqual(Entry.objects.get(id=entry['row_id']).merchant_key, 'rahim traders')
        # Tags are cleaned on an edit that sends them
        gone = Tag.objects.get(id=stock['row_id'])
        gone.soft_delete()
        [edited] = self.push(self.alice_client, self.shop,
                             self.upsert('entries', entry['row_id'], tag_ids=[stock['row_id']]))
        self.assertEqual(edited['row']['tag_ids'], [])


class TagPermissionTests(SyncTestCase):
    """In a business: who sees, makes and changes the workspace's tags."""

    def setUp(self):
        super().setUp()
        self.till = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.stock = Tag.objects.create(workspace=self.shop, name='Stock')
        self.carol = self.member('carol@example.com', Membership.ROLE_STAFF)
        self.dan = self.member('dan@example.com', Membership.ROLE_VIEWER)

    def member(self, email, role):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(email=email, password='pass-12345')
        Membership.objects.create(workspace=self.shop, user=user, role=role)
        client = APIClient()
        client.force_authenticate(user)
        return user, client

    def statuses(self, client, *mutations):
        return [r['error']['code'] if r['status'] == 'rejected' else r['status']
                for r in self.push(client, self.shop, *mutations)]

    def test_everyone_in_the_workspace_sees_its_tags(self):
        for _, client in (self.carol, self.dan):
            names = [t['name'] for t in self.pull(client, self.shop)['changes']['tags']]
            self.assertEqual(names, ['Stock'])

    def test_staff_who_may_add_entries_make_tags_but_do_not_change_them(self):
        carol, client = self.carol
        # No book of her own yet: nothing to tag
        self.assertEqual(self.statuses(client, self.upsert('tags', name='Rush')), ['forbidden'])
        grant = CashBookAdditionalMember.objects.create(cashbook=self.till, member=carol, role='viewer')
        self.assertEqual(self.statuses(client, self.upsert('tags', name='Rush')), ['forbidden'])
        grant.role = 'contributor'
        grant.save()
        self.assertEqual(self.statuses(
            client,
            self.upsert('tags', name='Rush'),
            self.upsert('tags', str(self.stock.id), name='Goods'),
            self.delete('tags', str(self.stock.id)),
        ), ['applied', 'forbidden', 'forbidden'])

    def test_viewers_only_read_and_managers_do_everything(self):
        _, viewer = self.dan
        self.assertEqual(self.statuses(viewer, self.upsert('tags', name='Rush')), ['forbidden'])
        self.assertEqual(self.statuses(
            self.alice_client,
            self.upsert('tags', str(self.stock.id), name='Goods', color='#3B9EE5'),
            self.delete('tags', str(self.stock.id)),
        ), ['applied', 'applied'])
        stock = Tag.all_objects.get(id=self.stock.id)
        self.assertEqual((stock.name, stock.color, stock.deleted_at is not None), ('Goods', '#3B9EE5', True))

    def test_a_tag_needs_a_name_and_a_real_colour(self):
        self.assertEqual(self.statuses(
            self.alice_client, self.upsert('tags', name=''), self.upsert('tags', name='x', color='red'),
            self.upsert('tags', name='x' * 41),
        ), ['invalid'] * 3)

    def test_the_admin_leaves_a_tombstone(self):
        model_admin = admin.site._registry[Tag]
        model_admin.delete_queryset(None, Tag.objects.filter(id=self.stock.id))
        self.assertIsNotNone(Tag.all_objects.get(id=self.stock.id).deleted_at)


class EntryApiTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.till = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.stock = Tag.objects.create(workspace=self.shop, name='Stock')
        self.mine = Tag.objects.create(workspace=self.personal, name='Mine')

    def test_entries_take_and_show_a_party_and_tags(self):
        url = f'{BOOKS}{self.till.id}/entries/'
        response = self.alice_client.post(url, {
            'entry_type': 'cash_out', 'amount_minor': 900, 'entry_date': '2026-10-01',
            'merchant': ' Karim  Store', 'tag_ids': [str(self.stock.id), str(self.mine.id)],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        entry = Entry.objects.get(id=response.data['id'])
        self.assertEqual((entry.merchant, entry.merchant_key, entry.tag_ids), ('Karim Store', 'karim store', [str(self.stock.id)]))

        listed = self.alice_client.get(url).data['results'][0]
        self.assertEqual((listed['merchant'], listed['tag_ids']), ('Karim Store', [str(self.stock.id)]))
        detail = self.alice_client.get(f'{url}{entry.id}/').data
        self.assertEqual((detail['merchant'], detail['tag_ids']), ('Karim Store', [str(self.stock.id)]))

        # Changing the party leaves the tags alone
        response = self.alice_client.patch(f'{url}{entry.id}/', {'merchant': 'Karim & Sons'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        entry.refresh_from_db()
        self.assertEqual((entry.merchant_key, entry.tag_ids), ('karim & sons', [str(self.stock.id)]))

        response = self.alice_client.patch(f'{url}{entry.id}/', {'tag_ids': ['nope']}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('tag_ids', response.data)


class TransactionSearchTests(AccountTestCase):
    def test_search_finds_a_merchant_or_a_tag_name(self):
        trip = Tag.objects.create(workspace=self.personal, name='Cox trip')
        gone = Tag.objects.create(workspace=self.personal, name='Gone')
        Transaction.objects.create(account=self.cash, kind='expense', amount_minor=-500, occurred_on='2026-10-01',
                                   note='Dinner', merchant='Star Kabab', tag_ids=[str(trip.id)])
        Transaction.objects.create(account=self.cash, kind='expense', amount_minor=-700, occurred_on='2026-10-02',
                                   note='Bus', tag_ids=[str(gone.id)])
        gone.soft_delete()
        self.assertEqual(self.notes(q='kabab'), ['Dinner'])
        self.assertEqual(self.notes(q='cox'), ['Dinner'])
        # A deleted tag is not searched by its name
        self.assertEqual(self.notes(q='gone'), [])
        row = self.rows()['results'][-1]
        self.assertEqual((row['merchant'], row['tag_ids']), ('Star Kabab', [str(trip.id)]))


class EntryHistoryTests(HistoryTestCase):
    def test_a_change_of_party_and_tags_reads_by_name(self):
        rush = Tag.objects.create(workspace=self.shop, name='Rush')
        stock = Tag.objects.create(workspace=self.shop, name='Stock')
        entry_id = self.add_entry(self.carol_client, title='Rice')
        response = self.carol_client.patch(
            f'{BOOKS}{self.till.id}/entries/{entry_id}/',
            {'merchant': 'Rahim', 'tag_ids': [str(rush.id), str(stock.id)]}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        # A tag gone since still reads by its name
        stock.soft_delete()
        edited = self.entry_rows()[0]
        self.assertEqual(
            sorted(edited['changes'], key=lambda c: c['field']),
            [{'field': 'merchant', 'before': '', 'after': 'Rahim'},
             {'field': 'tags', 'before': [], 'after': ['Rush', 'Stock']}],
        )
