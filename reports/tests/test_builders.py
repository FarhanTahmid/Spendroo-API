from datetime import date

from cashbook.models import CashBook
from ledger_personal.models import Account
from reports import periods
from reports.builders import account, business, cashbook, personal
from reports.document import Money, Totals

from .base import ReportTestCase

SEPTEMBER = periods.Period(periods.LAST_MONTH, date(2026, 9, 1), date(2026, 9, 30))


def taka(amount):
    return Money(amount, 'BDT')


class AccountStatementTests(ReportTestCase):
    def test_the_four_figures_add_up_and_count_transfers(self):
        report = account.build(self.cash, self.context())
        # Before October: 1,000 opening + 50,000 pay - 1,200 groceries - 20,000 moved to the bank
        self.assertEqual(report.totals, [Totals('BDT', 2980000, 500000, 75000, 3405000)])
        figures = report.totals[0]
        self.assertEqual(figures.opening_minor + figures.in_minor - figures.out_minor, figures.closing_minor)

    def test_the_statement_runs_from_the_opening_to_the_closing_balance(self):
        statement = self.table(account.build(self.cash, self.context()), 'Statement')
        self.assertEqual(statement.first[-1], taka(2980000))
        self.assertEqual(
            [(row[1], row[2], row[3], row[4], row[5]) for row in statement.rows],
            [
                ('Lunch 50% off', 'Food', None, taka(45000), taka(2935000)),
                ('Rickshaw', 'Uncategorised', None, taka(30000), taka(2905000)),
                ('ATM', 'Transfer from Brac Bank', taka(500000), None, taka(3405000)),
            ],
        )
        self.assertEqual(statement.total[3:], [taka(500000), taka(75000), taka(3405000)])
        self.assertTrue(statement.is_detail)

    def test_spending_is_broken_down_largest_first_with_shares(self):
        spending = self.table(account.build(self.cash, self.context()), 'Spending by category')
        self.assertEqual(spending.rows, [
            ['Food', 1, taka(45000), 0.6],
            ['Uncategorised', 1, taka(30000), 0.4],
        ])
        self.assertEqual(spending.total, ['Total', 2, taka(75000), None])

    def test_a_quiet_period_says_so(self):
        quiet = periods.Period(periods.CUSTOM, date(2020, 1, 1), date(2020, 1, 31))
        report = account.build(self.cash, self.context(quiet))
        self.assertEqual(report.totals, [Totals('BDT', 100000, 0, 0, 100000)])
        self.assertEqual(report.row_count, 0)
        self.assertEqual(self.table(report, 'Statement').empty, 'No transactions in this period.')


class PersonalReportTests(ReportTestCase):
    def test_transfers_are_neither_income_nor_spending(self):
        report = personal.build(self.personal, self.context(SEPTEMBER))
        # 20,000 went from Cash to the bank in September: the total held did not change
        self.assertEqual(report.totals, [Totals(
            'BDT', 100000, 5000000, 120000, 4980000, other_minor=0, other_label='Transfers from other currencies',
        )])
        self.assertEqual((report.in_label, report.out_label), ('Income', 'Spending'))

    def test_each_account_shows_its_own_movement(self):
        accounts = self.table(personal.build(self.personal, self.context(SEPTEMBER)), 'Your accounts')
        self.assertEqual(accounts.rows, [
            ['Cash', 'Cash', taka(100000), taka(5000000), taka(2120000), taka(2980000)],
            ['Brac Bank', 'Bank', taka(0), taka(2000000), None, taka(2000000)],
        ])
        self.assertEqual(accounts.total, ['Total', '', taka(100000), None, None, taka(4980000)])

    def test_income_and_spending_are_listed_without_transfers_which_get_their_own_table(self):
        report = personal.build(self.personal, self.context())
        lines = self.table(report, 'Income and spending')
        self.assertEqual([row[3] for row in lines.rows], ['Lunch 50% off', 'Rickshaw'])
        self.assertEqual(lines.total[-2:], [taka(0), taka(75000)])
        transfers = self.table(report, 'Transfers between your accounts')
        self.assertEqual([row[1:] for row in transfers.rows], [['Brac Bank', 'Cash', 'ATM', taka(500000)]])

    def test_each_currency_is_totalled_on_its_own(self):
        dollars = Account.objects.create(workspace=self.personal, name='Payoneer', kind='bank', currency='USD')
        self.add('income', 25000, '2026-10-06', account=dollars)
        report = personal.build(self.personal, self.context())
        self.assertEqual([totals.currency for totals in report.totals], ['BDT', 'USD'])
        self.assertEqual(report.totals[1], Totals(
            'USD', 0, 25000, 0, 25000, other_minor=0, other_label='Transfers from other currencies',
        ))
        self.assertIn('Each currency is totalled on its own. Nothing is converted.', report.notes)
        self.assertEqual(self.table(report, 'Your accounts (USD)').currency, 'USD')
        # One line cannot add taka to dollars
        self.assertIsNone(self.table(report, 'Income and spending').total)

    def test_an_empty_archived_account_is_left_out(self):
        Account.objects.create(workspace=self.personal, name='Old wallet', is_archived=True)
        accounts = self.table(personal.build(self.personal, self.context()), 'Your accounts')
        self.assertEqual([row[0] for row in accounts.rows], ['Cash', 'Brac Bank'])


class CashbookReportTests(ReportTestCase):
    def test_the_four_figures(self):
        report = cashbook.build(self.till, self.context())
        self.assertEqual(report.totals, [Totals('BDT', 10000, 50000, 20000, 40000)])
        self.assertEqual(report.subject, 'Till')

    def test_entries_carry_a_running_balance_and_who_entered_them(self):
        entries = self.table(cashbook.build(self.till, self.context()), 'Entries')
        self.assertEqual(entries.first[-1], taka(10000))
        self.assertEqual(entries.rows, [
            [date(2026, 10, 1), 'Counter sales', 'Sales', '', 'alice', taka(50000), None, taka(40000 + 20000)],
            [date(2026, 10, 3), 'Shop rent', 'Rent', 'bKash', 'alice', None, taka(20000), taka(40000)],
        ])
        self.assertEqual(entries.total[-3:], [taka(50000), taka(20000), taka(40000)])

    def test_breakdowns_by_category_and_payment_method(self):
        report = cashbook.build(self.till, self.context())
        self.assertEqual(self.table(report, 'By category').rows, [
            ['Sales', 1, taka(50000), None],
            ['Rent', 1, None, taka(20000)],
        ])
        self.assertEqual(self.table(report, 'By payment method').rows, [
            ['No payment method', 1, taka(50000), None],
            ['bKash', 1, None, taka(20000)],
        ])

    def test_a_deleted_category_reads_as_none(self):
        self.rent.soft_delete()
        report = cashbook.build(self.till, self.context())
        self.assertEqual(self.table(report, 'Entries').rows[1][2], '')
        self.assertIn('Uncategorised', [row[0] for row in self.table(report, 'By category').rows])


class BusinessReportTests(ReportTestCase):
    def test_the_top_adds_up_every_cashbook_and_each_is_then_shown(self):
        report = business.build(self.shop, self.context())
        self.assertEqual(report.totals, [Totals('BDT', 10000, 57000, 20000, 47000)])
        books = self.table(report, 'Cashbooks')
        self.assertEqual(books.rows, [
            ['Till', taka(10000), taka(50000), taka(20000), taka(40000)],
            ['Safe', taka(0), taka(7000), None, taka(7000)],
        ])
        self.assertEqual(books.total, ['Total', taka(10000), taka(57000), taka(20000), taka(47000)])
        self.assertEqual([table.title for table in report.details], ['Till', 'Safe'])
        self.assertEqual(report.row_count, 3)

    def test_cashbooks_in_another_currency_are_kept_apart(self):
        export = CashBook.objects.create(workspace=self.shop, book_name='Export', currency='USD')
        self.entry(export, 'cash_in', 900, '2026-10-05')
        report = business.build(self.shop, self.context())
        self.assertEqual(report.totals, [
            Totals('BDT', 10000, 57000, 20000, 47000), Totals('USD', 0, 900, 0, 900),
        ])
        self.assertEqual(self.table(report, 'By category (USD)').total[2], Money(900, 'USD'))

    def test_in_bangla(self):
        report = business.build(self.shop, self.context(language='bn'))
        self.assertEqual(report.title, 'ব্যবসার রিপোর্ট')
        self.assertEqual(self.table(report, 'ক্যাশবুকগুলো').total[0], 'মোট')
