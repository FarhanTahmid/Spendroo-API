"""A book's own fields: what a field may be, which values fit, who sets them up, and how they sync."""
import uuid

from django.core.exceptions import ValidationError

from workspaces.tests import TeamTestCase

from . import custom_fields
from .models import CashBook, CashBookAdditionalMember, Entry, EntryField

BOOKS = '/api/v1/cashbooks/'
PULL = '/api/v1/sync/pull/'
PUSH = '/api/v1/sync/push/'


class FieldTestCase(TeamTestCase):
    """Alice owns Shop (Business). Carol is staff, granted the till as each test needs."""

    def setUp(self):
        super().setUp()
        self.till = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.safe = CashBook.objects.create(workspace=self.shop, book_name='Safe')
        self.invoice = self.field('Invoice no', 'text')
        self.weight = self.field('Weight', 'number', position=1)
        self.due = self.field('Due', 'date', position=2)
        self.party = self.field('Party', 'choice', options=['Rahim', 'Karim'], position=3)

    def field(self, name, kind, book=None, **data):
        return EntryField.objects.create(cashbook=book or self.till, name=name, kind=kind, **data)

    def push(self, client, *mutations):
        response = client.post(PUSH, {'workspace': str(self.shop.id), 'mutations': list(mutations)}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['results']

    def push_one(self, mutation, client=None):
        return self.push(client or self.alice_client, mutation)[0]

    def upsert(self, table, row_id=None, **data):
        return {'id': str(uuid.uuid4()), 'table': table, 'op': 'upsert', 'row_id': row_id or str(uuid.uuid4()),
                'data': data}

    def new_entry(self, **data):
        return self.upsert('entries', cashbook_id=str(self.till.id), entry_type='cash_in', amount_minor=5000,
                           entry_date='2026-10-01', **data)

    def assertStatus(self, result, status, code=None):
        self.assertEqual(result['status'], status, result)
        if code:
            self.assertEqual(result['error']['code'], code, result)

    def grant(self, role):
        CashBookAdditionalMember.objects.create(cashbook=self.till, member=self.carol, role=role)


class FieldRulesTests(FieldTestCase):
    def check(self, field):
        field.full_clean(exclude=['workspace'])
        return field

    def test_names_are_tidied_and_unique_in_a_book_whatever_the_case(self):
        self.assertEqual(self.check(EntryField(cashbook=self.till, name='  Due   date ', kind='date')).name, 'Due date')
        with self.assertRaises(ValidationError):
            self.check(EntryField(cashbook=self.till, name='invoice NO', kind='text'))
        # Another book, or a name freed by deleting its field, is fine
        self.check(EntryField(cashbook=self.safe, name='Invoice no', kind='text'))
        self.invoice.soft_delete()
        self.check(EntryField(cashbook=self.till, name='Invoice no', kind='text'))

    def test_only_a_choice_has_options_and_it_needs_one(self):
        with self.assertRaises(ValidationError):
            self.check(EntryField(cashbook=self.till, name='Note', kind='text', options=['a']))
        with self.assertRaises(ValidationError):
            self.check(EntryField(cashbook=self.till, name='Size', kind='choice', options=[' ', '']))
        with self.assertRaises(ValidationError):
            self.check(EntryField(cashbook=self.till, name='Size', kind='choice', options=[str(n) for n in range(31)]))
        size = self.check(EntryField(cashbook=self.till, name='Size', kind='choice', options=[' Small ', 'small', 'Big']))
        self.assertEqual(size.options, ['Small', 'Big'])

    def test_a_deleted_book_takes_its_fields(self):
        self.till.soft_delete()
        self.assertFalse(EntryField.objects.filter(cashbook=self.till).exists())
        self.assertEqual(EntryField.all_objects.filter(cashbook=self.till).count(), 4)


class ValueTests(FieldTestCase):
    def test_each_kind_takes_only_what_fits(self):
        fits = {
            self.invoice: ['INV-1042', 'x' * 500],
            self.weight: ['0', '12', '-3.5', '1200.25', '999999999999999'],
            self.due: ['2026-11-01'],
            self.party: ['Rahim'],
        }
        refused = {
            self.invoice: ['x' * 501, 'two\nlines'],
            self.weight: ['1,200', '১২', '1e3', '01', '1.50', '.5', 'abc', '1.'],
            self.due: ['2026-13-01', '1 Nov 2026', '2026-1-1'],
            self.party: ['rahim', 'Someone'],
        }
        for field, values in fits.items():
            for value in values:
                self.assertIsNone(custom_fields.problem(field, value), (field.kind, value))
        for field, values in refused.items():
            for value in values:
                self.assertIsNotNone(custom_fields.problem(field, value), (field.kind, value))

    def test_blank_values_are_no_values_and_anything_but_text_is_refused(self):
        self.assertEqual(custom_fields.cleaned_shape({'a': ' ', 'b': None, 'c': 'x'}), {'c': 'x'})
        for wrong in ([], {'a': 3}, {'a': ['x']}, 'text'):
            with self.assertRaises(ValidationError):
                custom_fields.cleaned_shape(wrong)

    def test_only_changed_values_are_checked(self):
        key = str(self.party.id)
        old = {key: 'Rahim'}
        self.party.options = ['Karim']
        self.party.save()
        # Kept as it was: fine, even though Rahim is no longer offered
        custom_fields.check_values(self.till, {**old, str(self.invoice.id): 'INV-1'}, old)
        with self.assertRaises(ValidationError):
            custom_fields.check_values(self.till, {key: 'Rahim'}, {})
        # Clearing a value is never checked
        custom_fields.check_values(self.till, {}, old)

    def test_a_value_must_be_for_a_live_field_of_the_same_book(self):
        other = self.field('Invoice no', 'text', book=self.safe)
        for key in (str(other.id), 'not-an-id'):
            with self.assertRaises(ValidationError):
                custom_fields.check_values(self.till, {key: 'x'}, {})
        self.invoice.soft_delete()
        with self.assertRaises(ValidationError):
            custom_fields.check_values(self.till, {str(self.invoice.id): 'x'}, {})

    def test_a_new_entry_keeps_only_values_that_fit(self):
        self.invoice.soft_delete()
        values = {str(self.invoice.id): 'INV-1', str(self.weight.id): 'heavy', str(self.party.id): 'Karim'}
        self.assertEqual(custom_fields.drop_invalid(self.till, values), {str(self.party.id): 'Karim'})


class SyncTests(FieldTestCase):
    def test_fields_and_values_reach_every_phone_that_sees_the_book(self):
        Entry.objects.create(cashbook=self.till, entry_type='cash_in', amount_minor=100, entry_date='2026-10-01',
                             custom_values={str(self.invoice.id): 'INV-1'})
        self.grant('viewer')
        changes = self.carol_client.get(PULL, {'workspace': str(self.shop.id), 'since': 0}).data['changes']
        self.assertEqual([row['name'] for row in changes['entry_fields']], ['Invoice no', 'Weight', 'Due', 'Party'])
        self.assertEqual(changes['entry_fields'][3]['options'], ['Rahim', 'Karim'])
        self.assertEqual(changes['entries'][0]['custom_values'], {str(self.invoice.id): 'INV-1'})
        # Fields of a book Carol was not given stay away
        self.field('Lot', 'text', book=self.safe)
        changes = self.carol_client.get(PULL, {'workspace': str(self.shop.id), 'since': 0}).data['changes']
        self.assertNotIn('Lot', [row['name'] for row in changes['entry_fields']])

    def test_who_may_set_up_fields(self):
        field = self.upsert('entry_fields', cashbook_id=str(self.till.id), name='Lot', kind='text')
        self.grant('editor')
        self.assertStatus(self.push_one(field, self.carol_client), 'rejected', 'forbidden')
        CashBookAdditionalMember.objects.filter(member=self.carol).update(role='admin')
        self.assertStatus(self.push_one({**field, 'id': str(uuid.uuid4())}, self.carol_client), 'applied')
        self.assertStatus(self.push_one(self.upsert('entry_fields', cashbook_id=str(self.till.id), name='X', kind='text'),
                                        self.dan_client), 'rejected', 'forbidden')

    def test_a_field_keeps_its_kind_and_its_book(self):
        self.assertStatus(self.push_one(self.upsert('entry_fields', str(self.invoice.id), kind='number')),
                          'rejected', 'immutable')
        self.assertStatus(self.push_one(self.upsert('entry_fields', str(self.invoice.id), cashbook_id=str(self.safe.id))),
                          'rejected', 'immutable')
        self.assertStatus(self.push_one(self.upsert('entry_fields', str(self.invoice.id), name='Bill no')), 'applied')

    def test_the_last_field_pinned_is_the_one_shown_on_the_list(self):
        self.push_one(self.upsert('entry_fields', str(self.invoice.id), show_in_list=True))
        self.push_one(self.upsert('entry_fields', str(self.party.id), show_in_list=True))
        pinned = list(EntryField.objects.filter(cashbook=self.till, show_in_list=True).values_list('name', flat=True))
        self.assertEqual(pinned, ['Party'])
        # The field that lost its pin goes out again, so other phones hear of it
        self.invoice.refresh_from_db()
        self.assertGreater(self.invoice.version, 2)

    def test_a_book_holds_twenty_fields(self):
        for number in range(16):
            self.field(f'Extra {number}', 'text', position=4 + number)
        result = self.push_one(self.upsert('entry_fields', cashbook_id=str(self.till.id), name='One more', kind='text'))
        self.assertStatus(result, 'rejected', 'invalid')

    def test_entries_carry_values_and_changes_are_checked(self):
        entry = self.new_entry(custom_values={str(self.weight.id): '12.5', str(self.party.id): 'Rahim'})
        self.assertStatus(self.push_one(entry), 'applied')
        self.assertStatus(self.push_one(self.upsert('entries', entry['row_id'],
                                                    custom_values={str(self.weight.id): 'twelve'})),
                          'rejected', 'invalid')
        self.assertStatus(self.push_one(self.upsert('entries', entry['row_id'], custom_values=[])), 'rejected', 'invalid')
        result = self.push_one(self.upsert('entries', entry['row_id'], custom_values={str(self.weight.id): '13'}))
        self.assertStatus(result, 'applied')
        self.assertEqual(Entry.objects.get(id=entry['row_id']).custom_values, {str(self.weight.id): '13'})

    def test_a_new_entry_from_an_offline_phone_is_kept_when_its_field_has_gone(self):
        self.invoice.soft_delete()
        entry = self.new_entry(custom_values={str(self.invoice.id): 'INV-1', str(self.weight.id): '3'})
        self.assertStatus(self.push_one(entry), 'applied')
        self.assertEqual(Entry.objects.get(id=entry['row_id']).custom_values, {str(self.weight.id): '3'})

    def test_an_old_value_never_stops_an_entry_being_edited(self):
        entry = self.new_entry(custom_values={str(self.party.id): 'Rahim'})
        self.push_one(entry)
        self.party.soft_delete()
        self.assertStatus(self.push_one(self.upsert('entries', entry['row_id'], amount_minor=6000)), 'applied')

    def test_a_contributor_fills_values_on_a_new_entry(self):
        self.grant('contributor')
        entry = self.new_entry(custom_values={str(self.invoice.id): 'INV-9'})
        self.assertStatus(self.push_one(entry, self.carol_client), 'applied')
        self.assertStatus(self.push_one(self.upsert('entries', entry['row_id'], custom_values={}), self.carol_client),
                          'rejected', 'forbidden')


class RestTests(FieldTestCase):
    def entries(self, book=None):
        return f'{BOOKS}{(book or self.till).id}/entries/'

    def test_values_in_and_out_with_the_books_fields(self):
        payload = {'entry_type': 'cash_in', 'amount_minor': 5000, 'entry_date': '2026-10-01',
                   'custom_values': {str(self.invoice.id): 'INV-1'}}
        created = self.alice_client.post(self.entries(), payload, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        detail = self.alice_client.get(f"{self.entries()}{created.data['id']}/").data
        self.assertEqual(detail['custom_values'], {str(self.invoice.id): 'INV-1'})
        self.assertEqual([field['name'] for field in detail['fields_of_book']], ['Invoice no', 'Weight', 'Due', 'Party'])

        bad = {**payload, 'custom_values': {str(self.weight.id): 'heavy'}}
        self.assertEqual(self.alice_client.post(self.entries(), bad, format='json').status_code, 400)
        # A form sends the map as JSON text
        form = {**payload, 'custom_values': '{"%s": "7"}' % self.weight.id}
        self.assertEqual(self.alice_client.post(self.entries(), form).status_code, 201)

    def test_the_old_free_form_fields_are_gone(self):
        entry = Entry.objects.create(cashbook=self.till, entry_type='cash_in', amount_minor=100, entry_date='2026-10-01')
        response = self.alice_client.post(f'{self.entries()}{entry.id}/add-extra-field/', {'field_name': 'a'})
        self.assertEqual(response.status_code, 404)

