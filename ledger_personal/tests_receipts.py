"""Receipts of personal transactions: what each plan takes, who may do what, how they go, and how phones see them."""
import os
import uuid

from attachments import files as attachment_files, samples
from attachments.testing import AttachmentStorageMixin
from billing import usage
from billing.catalog.keys import F
from billing.models import UsageCounter
from billing.testing import grant_plan
from billing.tests.base import BillingTestCase, client_for
from cashbook.models import CashBook, Entry
from sync.guard import allow_hard_delete
from workspaces.models import Membership, Workspace

from .models import Account, Category, Transaction, TransactionReceipt

LEDGER = '/api/v1/ledger/'
PULL = '/api/v1/sync/pull/'
MB = samples.MB


class ReceiptTestCase(AttachmentStorageMixin, BillingTestCase):
    """Alice's Cash account with one expense on it, on the Free plan unless a test says otherwise."""

    def setUp(self):
        super().setUp()
        self.cash = Account.objects.get(workspace=self.personal)
        self.lunch = self.spend()

    def spend(self, account=None, kind='expense', amount=-25000, **extra):
        return Transaction.objects.create(
            account=account or self.cash, kind=kind, amount_minor=amount, occurred_on='2026-10-02', **extra,
        )

    def url(self, transaction=None, receipt_id=None, suffix=''):
        base = f'{LEDGER}transactions/{(transaction or self.lunch).id}/receipts/'
        return f'{base}{receipt_id}/{suffix}' if receipt_id else base

    def attach(self, client=None, data=None, name='receipt.jpg', transaction=None, receipt_id=None, **extra):
        payload = {'receipt_file': samples.upload(samples.jpeg() if data is None else data, name)}
        if receipt_id is not None:
            payload['receipt_id'] = str(receipt_id)
        return (client or self.alice_client).post(self.url(transaction), payload, format='multipart', **extra)

    def attached(self, client=None, data=None, **kwargs):
        response = self.attach(client, data, **kwargs)
        self.assertIn(response.status_code, (200, 201), response.data)
        return response.data['receipts'][0]

    def link(self, receipt_id, client=None, transaction=None):
        return (client or self.alice_client).get(self.url(transaction, receipt_id, 'link/'))

    def remove(self, receipt_id, client=None, transaction=None):
        return (client or self.alice_client).delete(self.url(transaction, receipt_id))

    def assertPlanLimit(self, response, feature, limit=None):
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual(response.data['code'], 'plan_limit')
        self.assertEqual(response.data['feature'], str(feature))
        if limit is not None:
            self.assertEqual(response.data['limit'], limit)


class PlanTierTests(ReceiptTestCase):
    """The same allowance business bills use: Free 10 MB of photos, 2 MB each; Plus PDFs and 20 MB each."""

    def test_free_takes_a_photo_and_counts_it(self):
        receipt = self.attached(data=samples.jpeg(size=2 * MB - 1000), name='Lunch.jpg')
        self.assertEqual((receipt['content_type'], receipt['original_name']), ('image/jpeg', 'Lunch.jpg'))
        self.assertEqual(str(receipt['transaction_id']), str(self.lunch.id))
        self.assertEqual(self.used(self.alice), receipt['size_bytes'])
        self.assertEqual(self.entitlements()['features']['storage.attachments_mb']['used_bytes'], receipt['size_bytes'])

    def test_free_refuses_a_photo_over_two_megabytes_the_way_every_plan_limit_does(self):
        response = self.attach(data=samples.jpeg(size=2 * MB + 1000))
        self.assertPlanLimit(response, F.STORAGE_ATTACHMENT_FILE_MB, 2)
        self.assertEqual(response.data['upgrade_to'], 'plus')
        self.assertEqual((TransactionReceipt.all_objects.count(), self.files()), (0, []))

    def test_free_takes_no_pdfs(self):
        response = self.attach(data=samples.pdf(), name='receipt.pdf')
        self.assertPlanLimit(response, F.STORAGE_ATTACHMENT_PDF)
        self.assertEqual((self.used(self.alice), self.files()), (0, []))

    def test_a_full_allowance_refuses_another_receipt(self):
        usage.take(self.alice, F.STORAGE_ATTACHMENTS_MB, 10 * MB - 500, 'test:fill', 10 * MB,
                   period=UsageCounter.LIFETIME)
        self.assertPlanLimit(self.attach(), F.STORAGE_ATTACHMENTS_MB, 10)
        self.assertEqual(self.files(), [])

    def test_receipts_and_business_bills_share_one_allowance(self):
        book = CashBook.objects.create(workspace=self.shop, book_name='Till')
        entry = Entry.objects.create(cashbook=book, entry_type='cash_out', amount_minor=500, entry_date='2026-10-01')
        bill = self.alice_client.post(
            f'/api/v1/cashbooks/{book.id}/entries/{entry.id}/add-bill/',
            {'bill_file': samples.upload(samples.jpeg(size=6 * MB // 4))}, format='multipart',
        )
        self.assertEqual(bill.status_code, 201, bill.data)
        receipt = self.attached(data=samples.jpeg(size=6 * MB // 4))
        self.assertEqual(self.used(self.alice), bill.data['bills'][0]['size_bytes'] + receipt['size_bytes'])

    def test_plus_takes_pdfs(self):
        grant_plan(self.alice, 'plus')
        receipt = self.attached(data=samples.pdf(2), name='Invoice.pdf')
        self.assertEqual((receipt['content_type'], receipt['page_count']), ('application/pdf', 2))
        stored = TransactionReceipt.objects.get(id=receipt['id']).receipt_file.name
        self.assertEqual(
            stored, f'workspaces/{self.personal.id}/transactions/{self.lunch.id}/receipts/{receipt["id"]}.pdf',
        )

    def test_several_files_in_one_request_are_all_kept(self):
        response = self.alice_client.post(self.url(), {
            'receipt_file': [samples.upload(samples.jpeg(), 'a.jpg'), samples.upload(samples.png(), 'b.png')],
        }, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual([r['original_name'] for r in response.data['receipts']], ['a.jpg', 'b.png'])


class RefusalTests(ReceiptTestCase):
    """What is not a receipt, or not where one may go, is refused and leaves nothing behind."""

    def test_hostile_files_are_refused_with_the_codes_the_app_words(self):
        grant_plan(self.alice, 'business')
        cases = [
            (samples.EXE, 'receipt.jpg', attachment_files.TYPE),
            (samples.png_bomb(), 'receipt.png', attachment_files.UNSAFE),
            (samples.pdf_with_script(), 'receipt.pdf', attachment_files.UNSAFE),
            (samples.pdf()[:-200], 'receipt.pdf', attachment_files.CORRUPT),
        ]
        for data, name, code in cases:
            response = self.attach(data=data, name=name)
            self.assertEqual((response.status_code, response.data.get('code')), (400, code), name)
        self.assertEqual((TransactionReceipt.all_objects.count(), self.used(self.alice), self.files()), (0, 0, []))

    def test_a_body_larger_than_any_file_is_refused_unread(self):
        response = self.attach(CONTENT_LENGTH=str(60 * MB))
        self.assertEqual((response.status_code, response.data['code']), (400, attachment_files.TOO_LARGE))

    def test_a_request_without_a_file_or_with_a_bad_id_is_refused(self):
        self.assertEqual(self.alice_client.post(self.url(), {}, format='multipart').status_code, 400)
        self.assertEqual(self.attach(receipt_id='nope').status_code, 400)
        two = self.alice_client.post(self.url(), {
            'receipt_file': [samples.upload(samples.jpeg()), samples.upload(samples.jpeg())],
            'receipt_id': str(uuid.uuid4()),
        }, format='multipart')
        self.assertEqual(two.status_code, 400)

    def test_transfers_and_goal_moves_take_no_receipt(self):
        bank = Account.objects.create(workspace=self.personal, name='Bank', kind='bank')
        group = uuid.uuid4()
        out = self.spend(kind='transfer', amount=-100, transfer_group_id=group)
        self.spend(account=bank, kind='transfer', amount=100, transfer_group_id=group)
        response = self.attach(transaction=out)
        self.assertEqual((response.status_code, response.data['code']), (400, 'receipt_not_allowed'))

    def test_income_takes_one_too(self):
        salary = self.spend(kind='income', amount=500000, category=Category.objects.get(
            workspace=self.personal, name='Salary'))
        self.assertEqual(str(self.attached(transaction=salary)['transaction_id']), str(salary.id))

    def test_an_account_locked_by_a_downgrade_takes_nothing_new_but_gives_receipts_back(self):
        plus = grant_plan(self.alice, 'plus')
        kept = self.attached()
        accounts = [self.cash] + [Account.objects.create(workspace=self.personal, name=f'Wallet {n}') for n in range(4)]
        self.age(accounts)
        plus.delete()
        locked = self.spend(account=accounts[-1])
        self.assertPlanLimit(self.attach(transaction=locked), F.PERSONAL_ACCOUNTS)
        # A kept account carries on, and removing is never refused
        self.assertEqual(self.attach().status_code, 201)
        self.assertEqual(self.remove(kept['id']).status_code, 204)


class AccessTests(ReceiptTestCase):
    """The personal ledger is for those who run the workspace; anyone else is told the transaction does not exist."""

    def test_someone_from_outside_reaches_nothing(self):
        receipt = self.attached()
        bob = client_for(self.bob)
        self.assertEqual(self.attach(bob).status_code, 404)
        self.assertEqual(self.link(receipt['id'], bob).status_code, 404)
        self.assertEqual(self.remove(receipt['id'], bob).status_code, 404)
        self.assertEqual(TransactionReceipt.objects.count(), 1)

    def test_a_viewer_of_the_workspace_reaches_nothing_either(self):
        Membership.objects.create(workspace=self.personal, user=self.bob, role=Membership.ROLE_VIEWER)
        receipt = self.attached()
        self.assertEqual(self.link(receipt['id'], self.bob_client).status_code, 404)
        url = self.link(receipt['id']).data['url']
        # A link made for Alice opens for whoever holds it; one made for Bob would not
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_a_receipt_of_another_transaction_is_not_reached_through_this_one(self):
        other = self.spend()
        receipt = self.attached(transaction=other)
        self.assertEqual(self.link(receipt['id']).status_code, 404)
        self.assertEqual(self.remove(receipt['id']).status_code, 404)
        self.assertEqual(self.link(uuid.uuid4()).status_code, 404)

    def test_a_deleted_transaction_takes_no_receipt(self):
        self.lunch.soft_delete()
        self.assertEqual(self.attach().status_code, 404)

    def test_the_link_gives_the_file_and_stops_once_its_holder_loses_the_workspace(self):
        membership = Membership.objects.create(workspace=self.personal, user=self.bob, role=Membership.ROLE_ADMIN)
        receipt = self.attached(data=samples.png(alpha=True), name='Shop receipt.png')
        url = self.link(receipt['id'], self.bob_client).data['url']
        response = self.client.get(url)
        self.assertEqual((response.status_code, response['Content-Type']), (200, 'image/png'))
        self.assertIn('Shop receipt.png', response['Content-Disposition'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        membership.role = Membership.ROLE_VIEWER
        membership.save()
        self.assertEqual(self.client.get(url).status_code, 404)


class RemovalTests(ReceiptTestCase):
    """However a receipt goes, the owner gets the storage back at once and the file leaves after the commit."""

    def setUp(self):
        super().setUp()
        with self.captureOnCommitCallbacks(execute=True):
            self.receipt = self.attached()
        self.assertEqual(len(self.files()), 1)

    def assertGone(self):
        self.assertEqual((self.used(self.alice), self.files()), (0, []))
        self.assertIsNotNone(TransactionReceipt.all_objects.get(id=self.receipt['id']).deleted_at)

    def test_removing_the_receipt(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.remove(self.receipt['id']).status_code, 204)
        self.assertGone()

    def test_deleting_the_transaction_from_a_phone(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertApplied(self.push_one(self.delete('transactions', self.lunch.id)))
        self.assertGone()

    def test_removing_the_whole_workspace(self):
        with self.captureOnCommitCallbacks(execute=True):
            with allow_hard_delete():
                Workspace.all_objects.get(pk=self.personal.pk).delete()
        self.assertEqual((self.used(self.alice), self.files()), (0, []))

    def test_the_admin_removes_one_as_the_app_does(self):
        from django.contrib.admin.sites import site
        admin = site._registry[TransactionReceipt]
        receipt = TransactionReceipt.objects.get(id=self.receipt['id'])
        self.assertFalse(admin.has_add_permission(None))
        self.assertFalse(admin.has_change_permission(None, receipt))
        with self.captureOnCommitCallbacks(execute=True):
            admin.delete_model(None, receipt)
        self.assertGone()

    def test_the_admin_removes_several_as_the_app_does(self):
        from django.contrib.admin.sites import site
        with self.captureOnCommitCallbacks(execute=True):
            site._registry[TransactionReceipt].delete_queryset(None, TransactionReceipt.objects.all())
        self.assertGone()

    def test_resetting_the_demo_gives_the_storage_back(self):
        from django.core.management import call_command
        with self.captureOnCommitCallbacks(execute=True):
            call_command('seed_demo', self.alice.username, '--reset', '--days', '3', stdout=open(os.devnull, 'w'))
        self.assertGone()


class RetryAndSyncTests(ReceiptTestCase):
    """A phone retries with the id it made; receipts reach phones by sync and cannot be written that way."""

    def test_sending_the_same_receipt_again_stores_and_counts_it_once(self):
        receipt_id = uuid.uuid4()
        first = self.attach(receipt_id=receipt_id)
        again = self.attach(receipt_id=receipt_id)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.data['receipts'][0]['id'], str(receipt_id))
        self.assertEqual(TransactionReceipt.objects.count(), 1)
        self.assertEqual(self.used(self.alice), first.data['receipts'][0]['size_bytes'])

    def test_an_id_belonging_to_another_transaction_is_refused(self):
        receipt_id = uuid.uuid4()
        self.attached(receipt_id=receipt_id)
        response = self.attach(transaction=self.spend(), receipt_id=receipt_id)
        self.assertEqual((response.status_code, response.data['code']), (409, 'bill_id_taken'))

    def pulled(self):
        response = self.alice_client.get(PULL, {'workspace': str(self.personal.id), 'since': 0})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['changes'].get('transaction_receipts', [])

    def test_phones_pull_receipts_and_their_removal(self):
        receipt = self.attached(name='Lunch.jpg')
        row = next(row for row in self.pulled() if str(row['id']) == receipt['id'])
        self.assertEqual(str(row['transaction_id']), str(self.lunch.id))
        self.assertEqual((row['content_type'], row['original_name'], row['deleted_at']), ('image/jpeg', 'Lunch.jpg', None))
        self.assertNotIn('receipt_file', row)
        self.remove(receipt['id'])
        row = next(row for row in self.pulled() if str(row['id']) == receipt['id'])
        self.assertIsNotNone(row['deleted_at'])

    def test_phones_cannot_write_receipts_by_sync(self):
        receipt = self.attached()
        results = self.push(
            self.alice_client, self.personal,
            self.upsert('transaction_receipts', transaction_id=str(self.lunch.id), size_bytes=1),
            self.upsert('transaction_receipts', receipt['id'], size_bytes=1),
            self.delete('transaction_receipts', receipt['id']),
        )
        self.assertEqual([result['status'] for result in results], ['rejected'] * 3)
        stored = TransactionReceipt.objects.get(id=receipt['id'])
        self.assertEqual((stored.size_bytes, stored.deleted_at), (receipt['size_bytes'], None))


class ReceiptFilterTests(ReceiptTestCase):
    """The account's list narrows to transactions with a receipt, or without one, and says which have one."""

    def rows(self, **params):
        response = self.alice_client.get(f'{LEDGER}accounts/{self.cash.id}/transactions/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['results']

    def test_with_and_without_a_receipt(self):
        bare = self.spend(note='No receipt')
        removed = self.spend(note='Receipt removed')
        self.attached()
        self.assertEqual(self.remove(self.attached(transaction=removed)['id'], transaction=removed).status_code, 204)
        self.assertEqual([row['id'] for row in self.rows(receipt='1')], [str(self.lunch.id)])
        self.assertEqual({row['id'] for row in self.rows(receipt='0')}, {str(bare.id), str(removed.id)})
        self.assertEqual({row['id']: row['has_receipt'] for row in self.rows()}[str(self.lunch.id)], True)
        summary = self.alice_client.get(f'{LEDGER}accounts/{self.cash.id}/summary/', {'receipt': '1'})
        self.assertEqual(summary.data['count'], 1)

    def test_anything_else_is_refused(self):
        response = self.alice_client.get(f'{LEDGER}accounts/{self.cash.id}/transactions/', {'receipt': 'yes'})
        self.assertEqual(response.status_code, 400)
