from datetime import date
from unittest import mock

from django.utils import timezone

from cashbook.models import CashBook, Entry, EntryCategory, PaymentMethod
from ledger_personal.tests import AccountTestCase
from reports import periods
from reports.builders.common import Context
from reports.labels import Words
from reports.narrow import EVERYTHING

TODAY = date(2026, 10, 12)
OCTOBER = periods.Period(periods.THIS_MONTH, date(2026, 10, 1), date(2026, 10, 31))
REPORTS = '/api/v1/reports/'


class ReportTestCase(AccountTestCase):
    """
    Alice on the Free plan, on 12 October 2026, with the two months of
    AccountTestCase.seed() in her personal accounts and a shop with two cashbooks.
    """
    plan = None

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(timezone, 'localdate', return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seed()

        self.till = CashBook.objects.create(workspace=self.shop, book_name='Till', created_by=self.alice)
        self.safe = CashBook.objects.create(workspace=self.shop, book_name='Safe', created_by=self.alice)
        self.sales = EntryCategory.objects.create(cashbook=self.till, category_name='Sales')
        self.rent = EntryCategory.objects.create(cashbook=self.till, category_name='Rent')
        self.bkash = PaymentMethod.objects.create(cashbook=self.till, payment_method_name='bKash')
        self.entry(self.till, 'cash_in', 10000, '2026-09-20', title='Opening cash')
        self.entry(self.till, 'cash_in', 50000, '2026-10-01', title='Counter sales', category=self.sales)
        self.entry(self.till, 'cash_out', 20000, '2026-10-03', title='Shop rent', category=self.rent,
                   payment_method=self.bkash)
        self.entry(self.safe, 'cash_in', 7000, '2026-10-04', title='Deposit')

    def entry(self, book, entry_type, amount, day, **fields):
        return Entry.objects.create(
            cashbook=book, created_by=self.alice, entry_type=entry_type, amount_minor=amount, entry_date=day, **fields,
        )

    def context(self, period=OCTOBER, language='en', user=None, narrow=EVERYTHING):
        return Context(
            user=user or self.alice, period=period, words=Words(language),
            reference='SR-20261012-TEST22', generated_at=timezone.now(), narrow=narrow,
        )

    def table(self, report, title):
        return next(table for table in report.tables if table.title == title)

    def urls(self):
        return {
            'personal': f'{REPORTS}personal/',
            'account': f'{REPORTS}accounts/{self.cash.id}/',
            'cashbook': f'{REPORTS}cashbooks/{self.till.id}/',
            'business': f'{REPORTS}businesses/{self.shop.id}/',
        }
