from django.db import migrations

from sync.guard import guard_deletes
from sync.sql import enable_sync
from workspaces.rls import enable_rls

SYNCED_TABLES = [
    'ledger_personal_transactionreceipt',
]


class Migration(migrations.Migration):

    dependencies = [
        ('ledger_personal', '0015_transaction_receipts'),
        ('sync', '0004_no_hard_deletes'),
    ]

    operations = (
        [operation for table in SYNCED_TABLES for operation in enable_rls(table)]
        + [operation for table in SYNCED_TABLES for operation in enable_sync(table)]
        + [operation for table in SYNCED_TABLES for operation in guard_deletes(table)]
    )
