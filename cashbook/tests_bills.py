"""Bills attached to entries: what each plan takes, what is refused, who may do what, and where files go."""
import os
import shutil
import tempfile
import time
import uuid
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from billing import usage
from billing.catalog.keys import F
from billing.models import UsageCounter
from billing.testing import grant_plan
from billing.tests.base import BillingTestCase, client_for
from sync.guard import allow_hard_delete
from workspaces.models import Membership, Workspace
from workspaces.tests import TeamTestCase

from . import bill_files, bill_samples as samples, bills
from .bill_storage import LINK_SECONDS, bill_storage
from .models import CashBook, Entry, EntryBills

BOOKS = '/api/v1/cashbooks/'
PULL = '/api/v1/sync/pull/'
PUSH = '/api/v1/sync/push/'
MB = samples.MB


class InspectionTests(SimpleTestCase):
    """Files are judged by their bytes; photos come out rewritten, PDFs with anything active are refused."""

    def refused(self, data):
        with self.assertRaises(bill_files.BillRejected) as caught:
            bill_files.inspect(data)
        return caught.exception.code

    def test_only_real_photos_and_pdfs_get_through(self):
        for data in (samples.HTML, samples.EXE, samples.SVG, b'', b'GIF89a' + b'\x00' * 40, samples.ZIP):
            self.assertEqual(self.refused(data), bill_files.TYPE, data[:10])

    def test_a_name_or_claimed_type_proves_nothing(self):
        # An executable called bill.jpg, sent as image/jpeg, is still an executable
        self.assertEqual(self.refused(samples.EXE), bill_files.TYPE)
        # A JPEG head on a file that is not one
        self.assertEqual(self.refused(b'\xff\xd8\xff' + samples.HTML), bill_files.CORRUPT)

    def test_photos_are_rewritten_with_nothing_but_their_pixels(self):
        gps = samples.jpeg(gps=True)
        self.assertIn(b'PhoneMaker', gps)
        self.assertNotIn(b'PhoneMaker', bill_files.inspect(gps).content)
        glued = bill_files.inspect(samples.jpeg() + samples.ZIP).content
        self.assertNotIn(b'PK\x03\x04', glued)
        padded = bill_files.inspect(samples.jpeg(size=MB)).content
        self.assertNotIn(b'CCCC', padded)
        self.assertNotIn(b'<?php', bill_files.inspect(samples.png(text='<?php system($_GET[1]); ?>')).content)

    def test_a_photo_without_see_through_parts_is_stored_as_jpeg(self):
        self.assertEqual(bill_files.inspect(samples.png()).content_type, bill_files.JPEG)
        self.assertEqual(bill_files.inspect(samples.png(alpha=True)).content_type, bill_files.PNG)

    def test_a_decompression_bomb_is_refused_before_it_is_decoded(self):
        self.assertEqual(self.refused(samples.png_bomb()), bill_files.UNSAFE)

    def test_pdfs_that_act_on_their_own_are_refused(self):
        for make in (
            samples.pdf_with_script, samples.pdf_with_open_action, samples.pdf_with_launch,
            samples.pdf_with_attachment, samples.pdf_encrypted, samples.pdf_hidden_script,
        ):
            self.assertEqual(self.refused(make()), bill_files.UNSAFE, make.__name__)

    def test_a_pdf_with_another_file_after_its_end_is_refused(self):
        self.assertEqual(self.refused(samples.pdf() + samples.ZIP), bill_files.UNSAFE)

    def test_broken_and_oversized_pdfs_are_refused(self):
        self.assertEqual(self.refused(samples.pdf()[:-200]), bill_files.CORRUPT)
        self.assertEqual(self.refused(b'%PDF-1.7\n%%EOF'), bill_files.CORRUPT)
        self.assertEqual(self.refused(samples.pdf(bill_files.MAX_PAGES + 1)), bill_files.TOO_LARGE)

    def test_a_clean_pdf_is_kept_as_it_came(self):
        data = samples.pdf(3)
        kept = bill_files.inspect(data)
        self.assertEqual((kept.content, kept.content_type, kept.page_count), (data, bill_files.PDF, 3))

    def test_names_are_for_showing_only(self):
        self.assertEqual(bills.clean_name('../../etc/passwd.jpg'), 'passwd.jpg')
        self.assertEqual(bills.clean_name('C:\\Users\\me\\bill\x00\n.pdf'), 'bill.pdf')
        self.assertEqual(len(bills.clean_name('a' * 500 + '.jpg')), bills.NAME_LENGTH)


class BillStorageMixin:
    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        storages = {
            **settings.STORAGES,
            'bills': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': self.folder}},
        }
        override = override_settings(STORAGES=storages)
        override.enable()
        self.addCleanup(override.disable)

    def files(self):
        return sorted(
            os.path.relpath(os.path.join(root, name), self.folder)
            for root, _, names in os.walk(self.folder) for name in names
        )

    def used(self, user):
        counter = UsageCounter.objects.filter(user=user, feature_key=str(F.STORAGE_ATTACHMENTS_MB)).first()
        return counter.used if counter else 0

    def new_entry(self, client, book, expected=201):
        response = client.post(
            f'{BOOKS}{book.id}/entries/',
            {'entry_type': 'cash_out', 'amount_minor': 5000, 'entry_date': '2026-10-01'}, format='json',
        )
        self.assertEqual(response.status_code, expected, response.data)
        return response.data.get('id')

    def attach(self, client, book, entry_id, data=None, name='bill.jpg', bill_id=None, **extra):
        payload = {'bill_file': samples.upload(samples.jpeg() if data is None else data, name)}
        if bill_id is not None:
            payload['bill_id'] = str(bill_id)
        return client.post(f'{BOOKS}{book.id}/entries/{entry_id}/add-bill/', payload, format='multipart', **extra)

    def attached(self, client, book, entry_id, data=None, **kwargs):
        response = self.attach(client, book, entry_id, data, **kwargs)
        self.assertIn(response.status_code, (200, 201), response.data)
        return response.data['bills'][0]


class PlanTierTests(BillStorageMixin, BillingTestCase):
    """Free: 10 MB of photos, 2 MB each. Plus: 1 GB, 20 MB each, PDFs too. Business: 10 GB, 50 MB each."""

    def setUp(self):
        super().setUp()
        self.book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.entry = self.new_entry(self.alice_client, self.book)

    def assertPlanLimit(self, response, feature, limit):
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual((response.data['feature'], response.data['limit']), (str(feature), limit))

    def test_the_tiers_are_what_was_agreed(self):
        features = self.entitlements()['features']
        self.assertEqual(features['storage.attachments_mb']['limit'], 10)
        self.assertEqual(features['storage.attachment_file_mb']['limit'], 2)
        self.assertEqual(features['storage.attachment_pdf']['enabled'], False)
        for plan, total, per_file in (('plus', 1024, 20), ('business', 10240, 50)):
            grant_plan(self.alice, plan)
            features = self.entitlements()['features']
            self.assertEqual(features['storage.attachments_mb']['limit'], total, plan)
            self.assertEqual(features['storage.attachment_file_mb']['limit'], per_file, plan)
            self.assertTrue(features['storage.attachment_pdf']['enabled'], plan)

    def test_free_takes_a_photo_up_to_two_megabytes(self):
        bill = self.attached(self.alice_client, self.book, self.entry, samples.jpeg(size=2 * MB - 1000))
        self.assertEqual(bill['content_type'], 'image/jpeg')
        self.assertEqual(self.used(self.alice), bill['size_bytes'])
        self.assertEqual(self.entitlements()['features']['storage.attachments_mb']['used_bytes'], bill['size_bytes'])

        response = self.attach(self.alice_client, self.book, self.entry, samples.jpeg(size=2 * MB + 1000))
        self.assertPlanLimit(response, F.STORAGE_ATTACHMENT_FILE_MB, 2)
        self.assertEqual(response.data['upgrade_to'], 'plus')
        self.assertEqual(EntryBills.objects.count(), 1)

    def test_free_takes_no_pdfs(self):
        response = self.attach(self.alice_client, self.book, self.entry, samples.pdf(), name='bill.pdf')
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual(response.data['feature'], 'storage.attachment_pdf')
        self.assertEqual((self.used(self.alice), self.files()), (0, []))

    def test_free_storage_is_full_at_ten_megabytes(self):
        usage.take(self.alice, F.STORAGE_ATTACHMENTS_MB, 10 * MB - 500, 'test:fill', 10 * MB,
                   period=UsageCounter.LIFETIME)
        response = self.attach(self.alice_client, self.book, self.entry)
        self.assertPlanLimit(response, F.STORAGE_ATTACHMENTS_MB, 10)
        self.assertEqual(self.files(), [])

    def test_plus_takes_pdfs_and_files_up_to_twenty_megabytes(self):
        grant_plan(self.alice, 'plus')
        bill = self.attached(self.alice_client, self.book, self.entry, samples.pdf(2), name='Invoice.pdf')
        self.assertEqual((bill['content_type'], bill['page_count'], bill['original_name']),
                         ('application/pdf', 2, 'Invoice.pdf'))
        self.attached(self.alice_client, self.book, self.entry, samples.jpeg(size=20 * MB - 1000))
        response = self.attach(self.alice_client, self.book, self.entry, samples.jpeg(size=20 * MB + 1000))
        self.assertPlanLimit(response, F.STORAGE_ATTACHMENT_FILE_MB, 20)
        self.assertEqual(response.data['upgrade_to'], 'business')

    def test_business_takes_fifty_megabytes_and_nothing_reads_more(self):
        grant_plan(self.alice, 'business')
        self.attached(self.alice_client, self.book, self.entry, samples.jpeg(size=21 * MB))
        response = self.attach(self.alice_client, self.book, self.entry, samples.jpeg(size=51 * MB))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['code'], bill_files.TOO_LARGE)

    def test_a_body_larger_than_any_bill_is_refused_unread(self):
        response = self.attach(self.alice_client, self.book, self.entry, CONTENT_LENGTH=str(60 * MB))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], bill_files.TOO_LARGE)

    def test_bills_sent_with_a_new_entry_are_checked_too(self):
        url = f'{BOOKS}{self.book.id}/entries/'
        entry = {'entry_type': 'cash_in', 'amount_minor': 5000, 'entry_date': '2026-10-01'}
        refused = self.alice_client.post(url, {**entry, 'bills': samples.upload(samples.HTML)}, format='multipart')
        self.assertEqual((refused.status_code, refused.data['code']), (400, bill_files.TYPE))
        self.assertEqual(Entry.objects.filter(cashbook=self.book).count(), 1)  # only setUp's

        made = self.alice_client.post(url, {**entry, 'bills': samples.upload(samples.jpeg())}, format='multipart')
        self.assertEqual(made.status_code, 201, made.data)
        self.assertEqual(len(made.data['bills']), 1)


class SecurityTests(BillStorageMixin, BillingTestCase):
    """Hostile files leave nothing behind, and stored files are reached only through a signed link."""

    def setUp(self):
        super().setUp()
        grant_plan(self.alice, 'business')
        self.book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.entry = self.new_entry(self.alice_client, self.book)

    def test_hostile_files_are_refused_and_leave_nothing_behind(self):
        cases = [
            (samples.EXE, 'invoice.jpg', bill_files.TYPE),
            (samples.HTML, 'bill.png', bill_files.TYPE),
            (samples.SVG, 'bill.svg', bill_files.TYPE),
            (samples.png_bomb(), 'bill.png', bill_files.UNSAFE),
            (samples.pdf_with_script(), 'bill.pdf', bill_files.UNSAFE),
            (samples.pdf_with_open_action(), 'bill.pdf', bill_files.UNSAFE),
            (samples.pdf_with_launch(), 'bill.pdf', bill_files.UNSAFE),
            (samples.pdf_with_attachment(), 'bill.pdf', bill_files.UNSAFE),
            (samples.pdf_encrypted(), 'bill.pdf', bill_files.UNSAFE),
            (samples.pdf() + samples.ZIP, 'bill.pdf', bill_files.UNSAFE),
            (samples.pdf()[:-200], 'bill.pdf', bill_files.CORRUPT),
        ]
        for data, name, code in cases:
            response = self.attach(self.alice_client, self.book, self.entry, data, name=name)
            self.assertEqual((response.status_code, response.data.get('code')), (400, code), name)
        self.assertEqual((EntryBills.all_objects.count(), self.used(self.alice), self.files()), (0, 0, []))

    def test_the_stored_file_is_named_by_the_server_and_holds_only_pixels(self):
        bill = self.attached(
            self.alice_client, self.book, self.entry, samples.jpeg(gps=True) + samples.ZIP, name='../../etc/passwd.jpg',
        )
        self.assertEqual(bill['original_name'], 'passwd.jpg')
        stored = EntryBills.objects.get(id=bill['id']).bill_file.name
        self.assertTrue(stored.endswith(f'/bills/{bill["id"]}.jpg'), stored)
        self.assertNotIn('..', stored)
        with bill_storage.open(stored, 'rb') as handle:
            content = handle.read()
        self.assertNotIn(b'PhoneMaker', content)
        self.assertNotIn(b'PK\x03\x04', content)
        self.assertNotIn('bill_file', bill)  # the path is never handed out

    def test_bills_are_not_under_the_public_media_folder(self):
        self.assertFalse(os.path.abspath(settings.BILL_FILES_ROOT).startswith(os.path.abspath(settings.MEDIA_ROOT)))

    def link(self, client, bill_id, entry_id=None):
        return client.get(f'{BOOKS}{self.book.id}/entries/{entry_id or self.entry}/bills/{bill_id}/link/')

    def test_a_link_gives_the_file_with_headers_that_keep_a_browser_from_running_it(self):
        bill = self.attached(self.alice_client, self.book, self.entry, samples.png(alpha=True), name='Shop bill.png')
        url = self.link(self.alice_client, bill['id']).data['url']
        response = client_for(self.bob).get(url)  # a link needs no session: whoever holds it, for five minutes
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('Shop bill.png', response['Content-Disposition'])
        self.assertEqual(b''.join(response.streaming_content)[:8], b'\x89PNG\r\n\x1a\n')

    def test_an_expired_or_altered_link_opens_nothing(self):
        bill = self.attached(self.alice_client, self.book, self.entry)
        url = self.link(self.alice_client, bill['id']).data['url']
        later = time.time() + LINK_SECONDS + 5
        with mock.patch('django.core.signing.time.time', return_value=later):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url[:-6] + 'AAAAA/').status_code, 403)
        self.assertEqual(self.client.get('/api/v1/bills/file/not-a-token/').status_code, 403)

    def test_someone_outside_the_business_cannot_reach_a_bill(self):
        bill = self.attached(self.alice_client, self.book, self.entry)
        bob = client_for(self.bob)
        self.assertEqual(self.link(bob, bill['id']).status_code, 404)
        self.assertEqual(bob.delete(f'{BOOKS}{self.book.id}/entries/{self.entry}/remove-bill/{bill["id"]}/').status_code, 404)
        self.assertIn(self.attach(bob, self.book, self.entry).status_code, (403, 404))
        self.assertEqual(self.link(self.alice_client, uuid.uuid4()).status_code, 404)
        self.assertEqual(self.link(self.alice_client, 'not-an-id').status_code, 404)

    def test_a_bill_of_another_entry_is_not_reached_through_this_one(self):
        other = self.new_entry(self.alice_client, self.book)
        bill = self.attached(self.alice_client, self.book, other)
        self.assertEqual(self.link(self.alice_client, bill['id'], entry_id=self.entry).status_code, 404)


class AccessTests(BillStorageMixin, TeamTestCase):
    """Viewers look; someone who only adds attaches to their own entries; editors do everything."""

    def setUp(self):
        super().setUp()
        self.till = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.safe = CashBook.objects.create(workspace=self.shop, book_name='Safe')
        self.owners_entry = self.new_entry(self.alice_client, self.till)

    def grant(self, user, role, book=None):
        response = self.alice_client.post(
            f'{BOOKS}{(book or self.till).id}/add_member/', {'member': str(user.id), 'role': role},
        )
        self.assertEqual(response.status_code, 201, response.data)

    def remove_url(self, entry_id, bill_id, book=None):
        return f'{BOOKS}{(book or self.till).id}/entries/{entry_id}/remove-bill/{bill_id}/'

    def test_a_contributor_attaches_to_their_own_entries_only(self):
        self.grant(self.carol, 'contributor')
        own = self.new_entry(self.carol_client, self.till)
        bill = self.attached(self.carol_client, self.till, own)
        self.assertEqual(EntryBills.objects.get(id=bill['id']).created_by, self.carol)
        self.assertEqual(self.attach(self.carol_client, self.till, self.owners_entry).status_code, 403)
        # Never takes one away, not even from their own entry
        self.assertEqual(self.carol_client.delete(self.remove_url(own, bill['id'])).status_code, 403)
        # Counted on the owner's plan, not the contributor's
        self.assertEqual((self.used(self.alice), self.used(self.carol)), (bill['size_bytes'], 0))

    def test_an_editor_attaches_and_removes_on_any_entry(self):
        self.grant(self.carol, 'editor')
        bill = self.attached(self.carol_client, self.till, self.owners_entry)
        self.assertEqual(self.carol_client.delete(self.remove_url(self.owners_entry, bill['id'])).status_code, 200)

    def test_a_viewer_opens_bills_but_changes_nothing(self):
        bill = self.attached(self.alice_client, self.till, self.owners_entry)
        link = self.dan_client.get(f'{BOOKS}{self.till.id}/entries/{self.owners_entry}/bills/{bill["id"]}/link/')
        self.assertEqual(link.status_code, 200)
        self.assertEqual(self.client.get(link.data['url']).status_code, 200)
        self.assertEqual(self.attach(self.dan_client, self.till, self.owners_entry).status_code, 403)
        self.assertEqual(self.dan_client.delete(self.remove_url(self.owners_entry, bill['id'])).status_code, 403)

    def test_staff_without_the_book_see_none_of_its_bills(self):
        self.grant(self.carol, 'editor', book=self.safe)
        bill = self.attached(self.alice_client, self.till, self.owners_entry)
        response = self.carol_client.get(f'{BOOKS}{self.till.id}/entries/{self.owners_entry}/bills/{bill["id"]}/link/')
        self.assertIn(response.status_code, (403, 404))

    def test_a_link_stops_working_once_its_holder_leaves_the_business(self):
        self.grant(self.carol, 'viewer')
        bill = self.attached(self.alice_client, self.till, self.owners_entry)
        url = self.carol_client.get(
            f'{BOOKS}{self.till.id}/entries/{self.owners_entry}/bills/{bill["id"]}/link/',
        ).data['url']
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.remove(self.alice_client, self.staff).status_code, 204)
        self.assertEqual(self.client.get(url).status_code, 404)


class RemovalTests(BillStorageMixin, BillingTestCase):
    """However a bill goes, the owner gets the storage back at once and the file leaves after the commit."""

    def setUp(self):
        super().setUp()
        grant_plan(self.alice, 'plus')
        self.book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.entry = self.new_entry(self.alice_client, self.book)
        with self.captureOnCommitCallbacks(execute=True):
            self.bill = self.attached(self.alice_client, self.book, self.entry)
        self.assertEqual(len(self.files()), 1)
        self.assertGreater(self.used(self.alice), 0)

    def assertGone(self):
        self.assertEqual(self.used(self.alice), 0)
        self.assertEqual(self.files(), [])
        self.assertIsNotNone(EntryBills.all_objects.get(id=self.bill['id']).deleted_at)

    def test_removing_the_bill(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.alice_client.delete(
                f'{BOOKS}{self.book.id}/entries/{self.entry}/remove-bill/{self.bill["id"]}/',
            )
        self.assertEqual(response.status_code, 200)
        self.assertGone()

    def test_deleting_the_entry(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.alice_client.delete(f'{BOOKS}{self.book.id}/entries/{self.entry}/').status_code, 200)
        self.assertGone()

    def test_deleting_the_entry_from_a_phone(self):
        mutation = {'id': str(uuid.uuid4()), 'table': 'entries', 'op': 'delete', 'row_id': str(self.entry)}
        with self.captureOnCommitCallbacks(execute=True):
            response = self.alice_client.post(
                PUSH, {'workspace': str(self.shop.id), 'mutations': [mutation]}, format='json',
            )
        self.assertEqual(response.data['results'][0]['status'], 'applied', response.data)
        self.assertGone()

    def test_deleting_the_book(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.alice_client.delete(f'{BOOKS}{self.book.id}/').status_code, 200)
        self.assertGone()

    def test_removing_the_whole_business(self):
        with self.captureOnCommitCallbacks(execute=True):
            with allow_hard_delete():
                Workspace.all_objects.get(pk=self.shop.pk).delete()
        self.assertEqual(self.used(self.alice), 0)
        self.assertEqual(self.files(), [])

    def test_a_removed_bill_gives_back_its_storage_once(self):
        bill = EntryBills.objects.get(id=self.bill['id'])
        with self.captureOnCommitCallbacks(execute=True):
            bills.remove(bill)
            bills.discard(EntryBills.all_objects.filter(id=bill.id))
            Entry.objects.get(id=self.entry).soft_delete()
        self.assertGone()

    def test_files_left_behind_are_swept_up(self):
        bill_storage.save('stray/left-behind.jpg', samples.upload(samples.jpeg()))
        self.assertEqual(bills.purge_files(), [])  # too new: maybe an upload still under way
        stray = os.path.join(self.folder, 'stray/left-behind.jpg')
        old = time.time() - 2 * 3600
        os.utime(stray, (old, old))
        call_command('purge_bill_files', stdout=open(os.devnull, 'w'))
        self.assertEqual(len(self.files()), 1)  # the live bill stays
        self.assertFalse(os.path.exists(stray))


class RetryAndSyncTests(BillStorageMixin, TeamTestCase):
    """A phone retries with the id it made; bills reach phones by sync and cannot be written that way."""

    def setUp(self):
        super().setUp()
        self.till = CashBook.objects.create(workspace=self.shop, book_name='Till')
        self.safe = CashBook.objects.create(workspace=self.shop, book_name='Safe')
        self.entry = self.new_entry(self.alice_client, self.till)

    def test_sending_the_same_bill_again_stores_and_counts_it_once(self):
        bill_id = uuid.uuid4()
        first = self.attach(self.alice_client, self.till, self.entry, bill_id=bill_id)
        again = self.attach(self.alice_client, self.till, self.entry, bill_id=bill_id)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.data['bills'][0]['id'], str(bill_id))
        self.assertEqual(EntryBills.objects.count(), 1)
        self.assertEqual(self.used(self.alice), first.data['bills'][0]['size_bytes'])

    def test_a_bill_id_belonging_to_another_entry_is_refused(self):
        bill_id = uuid.uuid4()
        self.attached(self.alice_client, self.till, self.entry, bill_id=bill_id)
        other = self.new_entry(self.alice_client, self.till)
        response = self.attach(self.alice_client, self.till, other, bill_id=bill_id)
        self.assertEqual((response.status_code, response.data['code']), (409, 'bill_id_taken'))
        bad = self.attach(self.alice_client, self.till, other, bill_id='nope')
        self.assertEqual(bad.status_code, 400)

    def pulled(self, client):
        response = client.get(PULL, {'workspace': str(self.shop.id), 'since': 0})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['changes'].get('entry_bills', [])

    def test_phones_pull_bills_of_the_books_they_see(self):
        bill = self.attached(self.alice_client, self.till, self.entry, samples.pdf(2), name='Invoice.pdf')
        row = next(row for row in self.pulled(self.alice_client) if str(row['id']) == bill['id'])
        self.assertEqual(str(row['entry_id']), str(self.entry))
        self.assertEqual(str(row['cashbook_id']), str(self.till.id))
        self.assertEqual((row['content_type'], row['page_count'], row['original_name']),
                         ('application/pdf', 2, 'Invoice.pdf'))
        self.assertNotIn('bill_file', row)

        self.alice_client.post(f'{BOOKS}{self.safe.id}/add_member/', {'member': str(self.carol.id), 'role': 'editor'})
        self.assertEqual(self.pulled(self.carol_client), [])

    def test_a_removal_reaches_phones_as_a_tombstone(self):
        bill = self.attached(self.alice_client, self.till, self.entry)
        self.alice_client.delete(f'{BOOKS}{self.till.id}/entries/{self.entry}/remove-bill/{bill["id"]}/')
        row = next(row for row in self.pulled(self.alice_client) if str(row['id']) == bill['id'])
        self.assertIsNotNone(row['deleted_at'])

    def test_phones_cannot_write_bills_by_sync(self):
        bill = self.attached(self.alice_client, self.till, self.entry)
        mutations = [
            {'id': str(uuid.uuid4()), 'table': 'entry_bills', 'op': 'upsert', 'row_id': str(uuid.uuid4()),
             'data': {'entry_id': str(self.entry), 'size_bytes': 1}},
            {'id': str(uuid.uuid4()), 'table': 'entry_bills', 'op': 'upsert', 'row_id': bill['id'],
             'data': {'size_bytes': 1}},
            {'id': str(uuid.uuid4()), 'table': 'entry_bills', 'op': 'delete', 'row_id': bill['id']},
        ]
        response = self.alice_client.post(
            PUSH, {'workspace': str(self.shop.id), 'mutations': mutations}, format='json',
        )
        self.assertEqual([result['status'] for result in response.data['results']], ['rejected'] * 3)
        stored = EntryBills.objects.get(id=bill['id'])
        self.assertEqual((stored.size_bytes, stored.deleted_at), (bill['size_bytes'], None))
