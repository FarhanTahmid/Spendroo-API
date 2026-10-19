from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from billing.catalog.keys import F
from billing.testing import set_flag
from billing.tests.base import client_for
from cashbook.models import CashBook, CashBookAdditionalMember, Entry, EntryField
from workspaces.models import Membership
from workspaces.services import create_workspace
from workspaces.tests import TeamTestCase

BOOKS = '/api/v1/cashbooks/'


class HistoryTestCase(TeamTestCase):
    """Alice owns Shop; Carol is staff with edit rights on the till."""

    def setUp(self):
        super().setUp()
        self.till = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.safe = CashBook.objects.create(workspace=self.shop, book_name='Safe')
        CashBookAdditionalMember.objects.create(cashbook=self.till, member=self.carol, role='editor')
        self.food = self.till.entrycategory_set.create(category_name='Food')
        self.rent = self.till.entrycategory_set.create(category_name='Rent')

    def url(self, workspace=None):
        return f'/api/v1/workspaces/{(workspace or self.shop).id}/history/'

    def history(self, client=None, **query):
        response = (client or self.alice_client).get(self.url(), query)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def rows(self, **query):
        return self.history(**query)['results']

    def add_entry(self, client, book=None, **data):
        payload = {'entry_type': 'cash_out', 'amount_minor': 10000, 'entry_date': '2026-10-01', **data}
        response = client.post(f'{BOOKS}{(book or self.till).id}/entries/', payload)
        self.assertEqual(response.status_code, 201, response.data)
        return response.data['id']

    def edit_entry(self, client, entry_id, book=None, **data):
        response = client.patch(f'{BOOKS}{(book or self.till).id}/entries/{entry_id}/', data)
        self.assertEqual(response.status_code, 200, response.data)

    def delete_entry(self, client, entry_id, book=None):
        response = client.delete(f'{BOOKS}{(book or self.till).id}/entries/{entry_id}/')
        self.assertLess(response.status_code, 300, getattr(response, 'data', None))

    def entry_rows(self, **query):
        return [row for row in self.rows(**query) if row['kind'] == 'entry']

    def shift(self, model, days, **match):
        """Move matching history back in time: every write in a test shares one moment."""
        model.pgh_event_model.objects.filter(**match).update(pgh_created_at=timezone.now() - timedelta(days=days))


class WhatTheOwnerSeesTests(HistoryTestCase):
    def test_a_staff_member_adding_editing_and_deleting_an_entry(self):
        entry_id = self.add_entry(self.carol_client, title='Lunch', category=str(self.food.id))
        self.edit_entry(self.carol_client, entry_id, amount_minor=12500, category=str(self.rent.id))
        self.delete_entry(self.carol_client, entry_id)

        deleted, edited, created = self.entry_rows()
        self.assertEqual([deleted['action'], edited['action'], created['action']], ['deleted', 'updated', 'created'])
        for row in (deleted, edited, created):
            self.assertEqual(row['actor'], {'id': str(self.carol.id), 'name': '', 'email': 'carol@example.com'})
            self.assertEqual(row['cashbook'], {'id': str(self.till.id), 'name': 'Till'})
            self.assertEqual((row['subject'], row['object_id']), ('Lunch', entry_id))
        self.assertEqual(
            sorted(edited['changes'], key=lambda c: c['field']),
            [{'field': 'amount_minor', 'before': 10000, 'after': 12500},
             {'field': 'category', 'before': 'Food', 'after': 'Rent'}],
        )
        self.assertEqual((edited['amount_minor'], edited['currency'], edited['entry_type']), (12500, 'BDT', 'cash_out'))
        self.assertEqual(created['changes'], [])

    def test_each_changed_field_value_is_a_change_of_its_own_by_name(self):
        invoice = EntryField.objects.create(cashbook=self.till, name='Invoice no', kind='text')
        party = EntryField.objects.create(cashbook=self.till, name='Party', kind='choice', options=['Rahim', 'Karim'],
                                          position=1)
        entry_id = self.add_entry(self.carol_client, title='Stock')
        entry = Entry.objects.get(id=entry_id)
        entry.custom_values = {str(invoice.id): '1041', str(party.id): 'Rahim'}
        entry.save()
        entry.custom_values = {str(invoice.id): '1042', str(party.id): 'Rahim'}
        entry.save()
        # A field deleted later keeps its name in what happened before
        invoice.soft_delete()

        rows = self.entry_rows()
        self.assertEqual(rows[0]['changes'], [
            {'field': 'custom_value', 'name': 'Invoice no', 'kind': 'text', 'before': '1041', 'after': '1042'},
        ])
        self.assertEqual(rows[1]['changes'], [
            {'field': 'custom_value', 'name': 'Invoice no', 'kind': 'text', 'before': None, 'after': '1041'},
            {'field': 'custom_value', 'name': 'Party', 'kind': 'choice', 'before': None, 'after': 'Rahim'},
        ])

    def test_fields_of_a_book_are_in_the_history_but_not_their_order(self):
        field = EntryField.objects.create(cashbook=self.till, name='Party', kind='text')
        field.position = 3
        field.save()
        field.name = 'Supplier'
        field.save()
        rows = [row for row in self.rows() if row['kind'] == 'field']
        self.assertEqual([(row['action'], row['subject']) for row in rows], [('updated', 'Supplier'), ('created', 'Party')])
        self.assertEqual(rows[0]['changes'], [{'field': 'name', 'before': 'Party', 'after': 'Supplier'}])

    def test_a_restored_entry(self):
        entry = Entry.objects.create(cashbook=self.till, entry_type='cash_in', amount_minor=500, entry_date='2026-10-01')
        entry.soft_delete()
        entry.restore()
        self.assertEqual([row['action'] for row in self.entry_rows()], ['restored', 'deleted', 'created'])

    def test_saves_that_change_nothing_an_owner_sees_are_left_out(self):
        entry_id = self.add_entry(self.alice_client)
        entry = Entry.objects.get(id=entry_id)
        entry.save()  # moves version and updated_at only
        self.assertEqual([row['action'] for row in self.entry_rows()], ['created'])

    def test_a_new_books_ready_made_categories_are_not_listed_one_by_one(self):
        response = self.alice_client.post(BOOKS, {'book_name': 'Online', 'workspace': str(self.shop.id)})
        self.assertEqual(response.status_code, 201, response.data)
        kinds = [(row['kind'], row['subject']) for row in self.rows()]
        self.assertIn(('cashbook', 'Online'), kinds)
        self.assertNotIn(('category', 'Housing'), kinds)
        self.assertIn(('category', 'Food'), kinds)  # one someone chose

    def test_access_and_team_changes(self):
        response = self.alice_client.patch(
            f'/api/v1/workspaces/{self.shop.id}/members/{self.staff.id}/', {'role': 'viewer'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        member = next(row for row in self.rows() if row['kind'] == 'member' and row['action'] == 'updated')
        self.assertEqual((member['subject'], member['role']), ('carol@example.com', 'viewer'))
        self.assertEqual(member['changes'], [{'field': 'role', 'before': 'staff', 'after': 'viewer'}])
        self.assertEqual(member['actor']['email'], 'alice@example.com')
        self.assertIsNone(member['cashbook'])
        # Becoming a viewer took Carol's grant on the till away
        access = next(row for row in self.rows() if row['kind'] == 'access' and row['action'] == 'deleted')
        self.assertEqual((access['subject'], access['cashbook']['name']), ('carol@example.com', 'Till'))

    def test_people_who_left_keep_their_name(self):
        self.carol.first_name = 'Carol'
        self.carol.save()
        self.add_entry(self.carol_client)
        self.alice_client.delete(f'/api/v1/workspaces/{self.shop.id}/members/{self.staff.id}/')
        self.assertEqual(self.entry_rows()[0]['actor']['name'], 'Carol')

    def test_a_change_with_nobody_behind_it_has_no_actor(self):
        Entry.objects.create(cashbook=self.till, entry_type='cash_in', amount_minor=500, entry_date='2026-10-01')
        self.assertIsNone(self.entry_rows()[0]['actor'])

    def test_a_synced_change_records_who_pushed_it(self):
        entry = self.upsert(
            'entries', cashbook_id=str(self.till.id), entry_type='cash_in', amount_minor=5000, entry_date='2026-10-01',
        )
        self.assertApplied(self.push(self.carol_client, self.shop, entry)[0])
        self.assertApplied(self.push(self.carol_client, self.shop, self.upsert('entries', entry['row_id'], amount_minor=700))[0])
        edited, created = self.entry_rows()
        self.assertEqual((edited['actor']['email'], created['actor']['email']), ('carol@example.com',) * 2)
        self.assertEqual(edited['changes'], [{'field': 'amount_minor', 'before': 5000, 'after': 700}])


class NarrowingTests(HistoryTestCase):
    def test_by_cashbook_leaves_out_other_books_and_the_team(self):
        Membership.objects.create(workspace=self.shop, user=get_user_model().objects.create_user(email='erin@example.com', password='pass-12345'), role='staff')
        self.add_entry(self.alice_client, title='In till')
        self.add_entry(self.alice_client, book=self.safe, title='In safe')
        rows = self.rows(cashbook=self.safe.id)
        self.assertTrue(rows)
        self.assertEqual({row['cashbook']['name'] for row in rows}, {'Safe'})
        self.assertNotIn('member', {row['kind'] for row in rows})

    def test_by_person(self):
        self.add_entry(self.alice_client, title='Mine')
        self.add_entry(self.carol_client, title='Hers')
        rows = self.rows(person=self.carol.id)
        self.assertEqual([(row['kind'], row['subject']) for row in rows], [('entry', 'Hers')])

    def test_by_entry(self):
        one = self.add_entry(self.alice_client, title='One')
        self.add_entry(self.alice_client, title='Two')
        self.edit_entry(self.alice_client, one, title='One!')
        rows = self.rows(entry=one)
        self.assertEqual([(row['action'], row['subject']) for row in rows], [('updated', 'One!'), ('created', 'One')])

    def test_by_dates(self):
        old = self.add_entry(self.alice_client, title='Old')
        self.shift(Entry, 10, pgh_obj_id=old)
        self.add_entry(self.alice_client, title='New')
        since = (timezone.now() - timedelta(days=2)).isoformat()
        until = (timezone.now() - timedelta(days=5)).isoformat()
        self.assertEqual([row['subject'] for row in self.entry_rows(**{'from': since})], ['New'])
        self.assertEqual([row['subject'] for row in self.entry_rows(to=until)], ['Old'])
        response = self.alice_client.get(self.url(), {'from': since, 'to': until})
        self.assertEqual(response.status_code, 400)


class PagingTests(HistoryTestCase):
    def test_pages_cover_everything_once_even_at_one_instant(self):
        for n in range(7):
            self.add_entry(self.alice_client, title=f'#{n}', book=self.till if n % 2 else self.safe)
        everything = self.rows(limit=100)
        seen, cursor = [], None
        while True:
            page = self.history(limit=3, **({'cursor': cursor} if cursor else {}))
            self.assertLessEqual(len(page['results']), 3)
            seen += page['results']
            cursor = page['next']
            if cursor is None:
                break
        self.assertEqual([row['id'] for row in seen], [row['id'] for row in everything])
        self.assertEqual(len({row['id'] for row in seen}), len(seen))

    def test_newest_first(self):
        old = self.add_entry(self.alice_client, title='Old')
        self.shift(Entry, 3, pgh_obj_id=old)
        self.add_entry(self.alice_client, title='New')
        self.assertEqual([row['subject'] for row in self.entry_rows()], ['New', 'Old'])

    def test_a_page_full_of_hidden_saves_still_reaches_what_is_behind_them(self):
        entry_id = self.add_entry(self.alice_client, title='Under')
        self.shift(Entry, 1, pgh_obj_id=entry_id)
        entry = Entry.objects.get(id=entry_id)
        for _ in range(8):
            entry.save()
        page = self.history(limit=2, entry=entry_id)
        self.assertEqual([row['action'] for row in page['results']], ['created'])
        self.assertIsNone(page['next'])

    def test_a_made_up_cursor_is_refused(self):
        self.assertEqual(self.alice_client.get(self.url(), {'cursor': 'nonsense'}).status_code, 400)


class WhoMayLookTests(HistoryTestCase):
    def test_only_the_owner(self):
        for client in (self.bob_client, self.carol_client, self.dan_client):
            response = client.get(self.url())
            self.assertEqual((response.status_code, response.data['code']), (403, 'owner_only'))

    def test_strangers_find_nothing(self):
        stranger = client_for(get_user_model().objects.create_user(email='eve@example.com', password='pass-12345'))
        self.assertEqual(stranger.get(self.url()).status_code, 404)

    def test_only_a_business(self):
        response = self.alice_client.get(self.url(self.personal))
        self.assertEqual((response.status_code, response.data['code']), (400, 'not_a_business'))

    def test_the_owners_plan_must_have_it(self):
        set_flag(self.alice, F.AUDIT_TRAIL, False)
        response = self.alice_client.get(self.url())
        self.assertEqual((response.status_code, response.data['code']), (402, 'plan_limit'))
        self.assertEqual(response.data['feature'], 'history.audit_trail')

    def test_another_business_never_shows(self):
        other = create_workspace(owner=self.alice, name='Other shop')
        book = CashBook.objects.create(workspace=other, book_name='Elsewhere')
        self.add_entry(self.alice_client, book=book, title='Not here')
        self.assertNotIn('Not here', [row['subject'] for row in self.rows()])
        self.assertNotIn('Elsewhere', [(row['cashbook'] or {}).get('name') for row in self.rows()])
