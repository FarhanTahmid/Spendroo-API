"""What is the same for every kind of attachment: judging a file, the signed link, the kinds, the nightly sweep."""
import io
import os
import time
from datetime import date
from unittest import mock

from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import Storage
from django.test import SimpleTestCase, override_settings

from billing.testing import grant_plan
from cashbook.tasks import purge_bill_files
from billing.tests.base import BillingTestCase
from cashbook.bills import BILL
from cashbook.models import CashBook, Entry, EntryBills
from ledger_personal.admin import TransactionReceiptInline
from ledger_personal.models import Account, Transaction, TransactionReceipt
from ledger_personal.queries import Filters
from ledger_personal.receipts import RECEIPT

from PIL import Image

from . import files, kinds, samples, service
from .storage import SALT, bill_storage, link_for, read_link
from .testing import AttachmentStorageMixin
from .views import download_name

MB = samples.MB


class SigningStore(Storage):
    """Stands in for an object store that signs its own links."""

    def __init__(self, **options):
        pass

    def url(self, name, expire=None):
        return f'https://store.example/{name}?expires={expire}'


class PlainStore(SigningStore):
    """An object store that cannot sign: its own url, private by its own rules."""

    def url(self, name):
        return f'https://plain.example/{name}'


class InspectionTests(SimpleTestCase):
    """Files are judged by their bytes; photos come out rewritten, PDFs with anything active are refused."""

    def refused(self, data):
        with self.assertRaises(files.FileRejected) as caught:
            files.inspect(data)
        return caught.exception.code

    def test_only_real_photos_and_pdfs_get_through(self):
        for data in (samples.HTML, samples.EXE, samples.SVG, b'', b'GIF89a' + b'\x00' * 40, samples.ZIP):
            self.assertEqual(self.refused(data), files.TYPE, data[:10])

    def test_a_name_or_claimed_type_proves_nothing(self):
        # An executable called bill.jpg, sent as image/jpeg, is still an executable
        self.assertEqual(self.refused(samples.EXE), files.TYPE)
        # A JPEG head on a file that is not one
        self.assertEqual(self.refused(b'\xff\xd8\xff' + samples.HTML), files.CORRUPT)

    def test_photos_are_rewritten_with_nothing_but_their_pixels(self):
        gps = samples.jpeg(gps=True)
        self.assertIn(b'PhoneMaker', gps)
        self.assertNotIn(b'PhoneMaker', files.inspect(gps).content)
        glued = files.inspect(samples.jpeg() + samples.ZIP).content
        self.assertNotIn(b'PK\x03\x04', glued)
        padded = files.inspect(samples.jpeg(size=MB)).content
        self.assertNotIn(b'CCCC', padded)
        self.assertNotIn(b'<?php', files.inspect(samples.png(text='<?php system($_GET[1]); ?>')).content)

    def test_a_photo_without_see_through_parts_is_stored_as_jpeg(self):
        self.assertEqual(files.inspect(samples.png()).content_type, files.JPEG)
        self.assertEqual(files.inspect(samples.png(alpha=True)).content_type, files.PNG)

    def test_a_decompression_bomb_is_refused_before_it_is_decoded(self):
        self.assertEqual(self.refused(samples.png_bomb()), files.UNSAFE)

    def test_pdfs_that_act_on_their_own_are_refused(self):
        for make in (
            samples.pdf_with_script, samples.pdf_with_open_action, samples.pdf_with_launch,
            samples.pdf_with_attachment, samples.pdf_encrypted, samples.pdf_hidden_script,
        ):
            self.assertEqual(self.refused(make()), files.UNSAFE, make.__name__)

    def test_a_pdf_with_another_file_after_its_end_is_refused(self):
        self.assertEqual(self.refused(samples.pdf() + samples.ZIP), files.UNSAFE)

    def test_broken_and_oversized_pdfs_are_refused(self):
        self.assertEqual(self.refused(samples.pdf()[:-200]), files.CORRUPT)
        self.assertEqual(self.refused(b'%PDF-1.7\n%%EOF'), files.CORRUPT)
        self.assertEqual(self.refused(samples.pdf(files.MAX_PAGES + 1)), files.TOO_LARGE)

    def test_a_clean_pdf_is_kept_as_it_came(self):
        data = samples.pdf(3)
        kept = files.inspect(data)
        self.assertEqual((kept.content, kept.content_type, kept.page_count), (data, files.PDF, 3))

    def test_names_are_for_showing_only(self):
        self.assertEqual(service.clean_name('../../etc/passwd.jpg'), 'passwd.jpg')
        self.assertEqual(service.clean_name('C:\\Users\\me\\bill\x00\n.pdf'), 'bill.pdf')
        self.assertEqual(len(service.clean_name('a' * 500 + '.jpg')), service.NAME_LENGTH)


class ReadingTests(SimpleTestCase):
    """What is refused before a file is judged at all, and photos that need converting."""

    def test_a_file_that_grows_past_its_cap_while_read_is_refused(self):
        upload = SimpleUploadedFile('r.jpg', samples.jpeg(size=3000))
        upload.size = None  # sent without saying how big it is
        with self.assertRaises(files.FileRejected) as caught:
            files.read_upload(upload, 1000)
        self.assertEqual(caught.exception.code, files.TOO_LARGE)

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(files.FileRejected) as caught:
            files.read_upload(SimpleUploadedFile('r.jpg', b''), 1000)
        self.assertEqual(caught.exception.code, files.CORRUPT)

    def test_a_photo_claiming_more_pixels_than_a_camera_makes_is_refused_from_its_header(self):
        with self.assertRaises(files.FileRejected) as caught:
            files.inspect(samples.png_bomb(8000, 8000))
        self.assertEqual(caught.exception.code, files.UNSAFE)

    def test_a_palette_photo_is_stored_as_a_plain_jpeg(self):
        out = io.BytesIO()
        Image.new('P', (20, 10), 3).save(out, 'PNG')
        kept = files.inspect(out.getvalue())
        self.assertEqual((kept.content_type, kept.width, kept.height), (files.JPEG, 20, 10))


class KindTests(SimpleTestCase):
    """Each app's rows that take files are a kind; the rest is shared."""

    def test_bills_and_receipts_are_both_kinds(self):
        self.assertIs(kinds.get('bill'), BILL)
        self.assertIs(kinds.get('receipt'), RECEIPT)
        self.assertIsNone(kinds.get('nothing'))
        self.assertEqual({kind.name for kind in kinds.every()}, {'bill', 'receipt'})
        self.assertIs(kinds.of_model(TransactionReceipt), RECEIPT)

    def test_the_admin_shows_receipts_and_never_adds_one(self):
        from django.contrib.admin.sites import site
        self.assertFalse(TransactionReceiptInline(Transaction, site).has_add_permission(None))

    def test_a_receipt_filter_narrows_the_list(self):
        self.assertTrue(Filters(has_receipt=False).is_narrowed)
        self.assertFalse(Filters().is_narrowed)

    def test_each_kind_counts_its_files_under_its_own_key(self):
        self.assertEqual(BILL.storage_key('x'), 'bill:x')
        self.assertEqual(RECEIPT.storage_key('x'), 'receipt:x')

    def test_a_download_is_named_as_the_person_named_it_ending_in_what_it_is(self):
        receipt = TransactionReceipt(original_name='Lunch.PNG', content_type=files.JPEG)
        self.assertEqual(download_name(RECEIPT, receipt), 'Lunch.jpg')
        self.assertEqual(download_name(RECEIPT, TransactionReceipt(content_type='odd/type')), 'receipt.bin')


class SharedStorageTests(AttachmentStorageMixin, BillingTestCase):
    """Bills and receipts live side by side in one storage, and one sweep looks after both."""

    def setUp(self):
        super().setUp()
        grant_plan(self.alice, 'plus')
        book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        entry = Entry.objects.create(
            cashbook=book, workspace=self.shop, entry_type='cash_out', amount_minor=500, entry_date=date(2026, 10, 1),
        )
        wallet = Account.objects.create(workspace=self.personal, name='Wallet')
        lunch = Transaction.objects.create(
            workspace=self.personal, account=wallet, kind='expense', amount_minor=-500, occurred_on=date(2026, 10, 1),
        )
        self.bill = service.attach(BILL, entry, [(samples.upload(samples.jpeg()), None)], actor=self.alice)[0][0]
        self.receipt = service.attach(
            RECEIPT, lunch, [(samples.upload(samples.pdf(), 'r.pdf'), None)], actor=self.alice,
        )[0][0]

    def test_both_kinds_take_from_one_allowance(self):
        self.assertEqual(self.used(self.alice), self.bill.size_bytes + self.receipt.size_bytes)
        self.assertTrue(str(self.receipt).startswith('Receipt for expense -500'))
        self.assertEqual(EntryBills.objects.count(), 1)
        self.assertTrue(self.receipt.receipt_file.name.endswith(f'/receipts/{self.receipt.id}.pdf'))

    def test_the_sweep_keeps_every_live_file_of_every_kind(self):
        stray = 'stray/old.jpg'
        bill_storage.save(stray, samples.upload(samples.jpeg()))
        old = time.time() - 2 * 3600
        for name in self.files():
            os.utime(os.path.join(self.folder, name), (old, old))
        self.assertEqual(service.purge_files(), [stray])
        self.assertEqual(len(self.files()), 2)

    def test_a_link_names_its_kind_and_one_from_before_receipts_is_a_bills(self):
        url, _ = link_for(RECEIPT, self.receipt, self.alice)
        token = url.rstrip('/').rsplit('/', 1)[1]
        self.assertEqual(read_link(token), ('receipt', str(self.receipt.id), str(self.alice.pk)))
        old = signing.dumps({'b': str(self.bill.id), 'u': str(self.alice.pk)}, salt=SALT, compress=True)
        self.assertEqual(read_link(old)[0], 'bill')
        self.assertEqual(self.client.get(f'/api/v1/bills/file/{old}/').status_code, 200)

    def test_a_link_naming_no_known_kind_opens_nothing(self):
        token = signing.dumps({'k': 'nothing', 'b': str(self.bill.id), 'u': str(self.alice.pk)}, salt=SALT)
        self.assertEqual(self.client.get(f'/api/v1/bills/file/{token}/').status_code, 403)

    def test_a_link_made_for_a_user_since_switched_off_opens_nothing(self):
        url, _ = link_for(BILL, self.bill, self.alice)
        self.alice.is_active = False
        self.alice.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_a_file_gone_from_the_storage_is_not_found(self):
        url, _ = link_for(RECEIPT, self.receipt, self.alice)
        bill_storage.delete(self.receipt.receipt_file.name)
        self.assertEqual(self.client.get(url).status_code, 404)

    def store(self, backend):
        return override_settings(STORAGES={
            'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'bills': {'BACKEND': f'{__name__}.{backend}', 'OPTIONS': {'location': self.folder}},
        })

    def test_an_object_store_signs_its_own_links(self):
        with self.store('SigningStore'):
            url, _ = link_for(RECEIPT, self.receipt, self.alice)
            self.assertEqual(url, f'https://store.example/{self.receipt.receipt_file.name}?expires=300')
        with self.store('PlainStore'):
            url, _ = link_for(BILL, self.bill, self.alice)
            self.assertEqual(url, f'https://plain.example/{self.bill.bill_file.name}')

    def test_an_upload_that_cannot_be_read_is_refused_before_anything_is_counted(self):
        with self.assertRaises(files.FileRejected):
            service.attach(BILL, self.bill.entry, [(SimpleUploadedFile('e.jpg', b''), None)], actor=self.alice)

    def test_the_same_id_stored_by_another_request_at_once_is_given_back(self):
        lunch = self.receipt.transaction
        before = self.used(self.alice)
        with mock.patch('attachments.service.uuid7', return_value=self.receipt.id):
            results = service.attach(RECEIPT, lunch, [(samples.upload(samples.jpeg()), None)], actor=self.alice)
        self.assertEqual(results, [(self.receipt, False)])
        self.assertEqual((self.used(self.alice), len(self.files())), (before, 2))

    def test_an_id_taken_by_another_row_at_once_is_a_conflict(self):
        other = Transaction.objects.create(
            workspace=self.personal, account=self.receipt.transaction.account, kind='expense', amount_minor=-100,
            occurred_on=date(2026, 10, 2),
        )
        with mock.patch('attachments.service.uuid7', return_value=self.receipt.id):
            with self.assertRaises(service.AttachmentConflict):
                service.attach(RECEIPT, other, [(samples.upload(samples.jpeg()), None)], actor=self.alice)
        self.assertEqual(len(self.files()), 2)

    def test_a_store_that_cannot_say_a_files_age_or_delete_it_leaves_it_for_later(self):
        bill_storage.save('stray/old.jpg', samples.upload(samples.jpeg()))
        with mock.patch.object(bill_storage, 'get_modified_time', side_effect=NotImplementedError):
            self.assertEqual(service.purge_files(), [])
        with mock.patch.object(bill_storage, 'delete', side_effect=OSError):
            service.remove(RECEIPT, self.receipt)
            with self.captureOnCommitCallbacks(execute=True):
                service.remove(BILL, self.bill)
        self.assertEqual(len(self.files()), 3)

    def test_the_nightly_task_sweeps(self):
        bill_storage.save('stray/old.jpg', samples.upload(samples.jpeg()))
        old = time.time() - 2 * 3600
        os.utime(os.path.join(self.folder, 'stray/old.jpg'), (old, old))
        purge_bill_files(0)
        self.assertEqual(len(self.files()), 2)
