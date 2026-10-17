import random
import uuid
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from identity.models import AppUser
from ledger_personal.models import Account, Budget, Category, Transaction
from sync.guard import tombstone
from workspaces.services import get_personal_workspace

# (name, kind, opening balance in major units)
ACCOUNTS = [
    ('Brac Bank', 'bank', 60000),
    ('bKash', 'mobile_money', 3000),
    ('City Bank Card', 'card', 0),
    ('Emergency Savings', 'savings', 40000),
]
CASH_OPENING = 8000  # the default Cash account, created with the workspace

# category: (account, low, high, per-month count, notes)
EXPENSES = {
    'Food': ('Cash', 120, 650, 22, ['Lunch', 'Dinner', 'Tea & snacks', 'Groceries', 'Street food', 'Breakfast']),
    'Transport': ('bKash', 40, 450, 14, ['Rickshaw', 'Uber', 'Bus fare', 'CNG', 'Pathao ride']),
    'Shopping': ('City Bank Card', 600, 4500, 3, ['Clothes', 'Daraz order', 'Shoes', 'Household items']),
    'Entertainment': ('City Bank Card', 300, 1800, 3, ['Cinema', 'Netflix', 'Dinner out', 'Games']),
    'Health': ('Cash', 200, 2200, 1, ['Pharmacy', 'Doctor visit', 'Lab test']),
    'Gifts & Donations': ('bKash', 200, 2000, 1, ['Friend birthday', 'Zakat', 'Mosque donation']),
    'Education': ('Brac Bank', 500, 3000, 1, ['Online course', 'Books']),
}
# category: (account, day of month, amount, note)
MONTHLY_BILLS = [
    ('Housing', 'Brac Bank', 5, 18000, 'House rent'),
    ('Utilities', 'bKash', 8, 1450, 'Electricity bill'),
    ('Utilities', 'bKash', 9, 700, 'Gas bill'),
    ('Utilities', 'bKash', 10, 400, 'Water bill'),
    ('Mobile & Internet', 'bKash', 12, 1100, 'Broadband'),
    ('Mobile & Internet', 'bKash', 14, 349, 'Mobile data pack'),
]
BUDGETS = {
    'Food': 12000, 'Transport': 4000, 'Housing': 18000, 'Utilities': 3000, 'Health': 2500,
    'Education': 3000, 'Shopping': 6000, 'Entertainment': 3000, 'Mobile & Internet': 1600,
    'Gifts & Donations': 2500,
}


class Command(BaseCommand):
    help = 'Fill a user\'s personal workspace with ~2 months of demo accounts, transactions and budgets.'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('--days', type=int, default=61, help='How far back to start (default 61).')
        parser.add_argument('--seed', type=int, default=42, help='Random seed, for repeatable data.')
        parser.add_argument('--reset', action='store_true',
                            help='Delete the workspace\'s transactions, budgets and non-default accounts first. '
                                 'They are tombstoned, so signed-in phones drop them on their next sync.')

    def handle(self, *args, username, days, seed, reset, **options):
        user = AppUser.objects.filter(username__iexact=username).first()
        if user is None:
            raise CommandError(f'No user named {username}.')
        workspace = get_personal_workspace(user)
        rng = random.Random(seed)
        today = timezone.localdate()
        start = today - timedelta(days=days)

        with transaction.atomic():
            if Transaction.objects.filter(workspace=workspace).exists() and not reset:
                raise CommandError('This workspace already has transactions. Use --reset to replace them.')
            if reset:
                # Never a hard delete: a phone learns a row is gone only from its tombstone
                tombstone(Transaction.objects.filter(workspace=workspace))
                tombstone(Budget.objects.filter(workspace=workspace))
                tombstone(Account.objects.filter(workspace=workspace).exclude(name='Cash'))

            cash = Account.objects.filter(workspace=workspace, name='Cash').first() or Account.objects.create(
                workspace=workspace, name='Cash', kind='cash', currency=workspace.default_currency)
            cash.opening_balance_minor = CASH_OPENING * 100
            cash.save()
            accounts = {'Cash': cash}
            for name, kind, opening in ACCOUNTS:
                accounts[name] = Account.objects.create(
                    workspace=workspace, name=name, kind=kind, currency=workspace.default_currency,
                    opening_balance_minor=opening * 100)
            categories = {c.name: c for c in Category.objects.filter(workspace=workspace)}

            rows = []

            def add(kind, account, day, amount, category=None, note='', group=None, source='manual'):
                sign = 1 if kind == 'income' or (kind == 'transfer' and amount > 0) else -1
                rows.append(Transaction(
                    workspace=workspace, account=accounts[account], category=categories.get(category),
                    kind=kind, amount_minor=sign * abs(amount) * 100, currency=accounts[account].currency,
                    transfer_group_id=group, occurred_on=day, note=note, source=source))

            def transfer(day, src, dst, amount, note):
                group = uuid.uuid4()
                add('transfer', src, day, -amount, note=note, group=group)
                add('transfer', dst, day, amount, note=note, group=group)

            first = date(start.year, start.month, 1)
            months = []
            while first <= today:
                months.append(first)
                first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)

            for month in months:
                def on(dom):
                    day = month.replace(day=min(dom, 28))
                    return day if start <= day <= today else None

                if day := on(1):
                    add('income', 'Brac Bank', day, 65000, 'Salary', 'Monthly salary')
                if day := on(18):
                    add('income', 'bKash', day, rng.randrange(4000, 12000, 500), 'Business', 'Freelance project')
                if day := on(22):
                    add('income', 'Cash', day, rng.choice([1500, 2000, 3000]), 'Gift', 'Eid/family gift')
                if day := on(27):
                    add('income', 'Brac Bank', day, rng.randrange(300, 900, 50), 'Other income', 'Savings interest')
                if day := on(2):
                    transfer(day, 'Brac Bank', 'Emergency Savings', 10000, 'Monthly savings')
                if day := on(3):
                    transfer(day, 'Brac Bank', 'bKash', 8000, 'Top up bKash')
                if day := on(6):
                    transfer(day, 'Brac Bank', 'Cash', 6000, 'ATM withdrawal')
                if day := on(20):
                    transfer(day, 'Brac Bank', 'Cash', 4000, 'ATM withdrawal')
                if day := on(25):
                    transfer(day, 'Brac Bank', 'City Bank Card', 14000, 'Card bill payment')
                for category, account, dom, amount, note in MONTHLY_BILLS:
                    if day := on(dom):
                        add('expense', account, day, amount, category, note)

            span = (today - start).days + 1
            for category, (account, low, high, per_month, notes) in EXPENSES.items():
                for _ in range(round(per_month * span / 30)):
                    day = start + timedelta(days=rng.randrange(span))
                    amount = round(rng.uniform(low, high) / 10) * 10
                    add('expense', account, day, amount, category, rng.choice(notes))

            # Occasional bank-side and mobile-money purchases and a few auto-captured ones
            for _ in range(8):
                day = start + timedelta(days=rng.randrange(span))
                add('expense', rng.choice(['Brac Bank', 'bKash']), day, rng.randrange(300, 2500, 50),
                    rng.choice(['Food', 'Shopping', 'Other']), 'SMS captured payment', source='sms')

            Transaction.objects.bulk_create(rows)

            for name, amount in BUDGETS.items():
                Budget.objects.create(workspace=workspace, category=categories[name], amount_minor=amount * 100)
            # One month with a tighter food budget, to show an override
            Budget.objects.create(
                workspace=workspace, category=categories['Food'], amount_minor=10000 * 100,
                month=months[-1])

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {len(rows)} transactions, {len(ACCOUNTS)} accounts and {len(BUDGETS) + 1} budgets '
            f'for {user.username} ({start} to {today}).'))
