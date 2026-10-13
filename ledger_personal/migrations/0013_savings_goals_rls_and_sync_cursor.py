from django.db import migrations

from sync.sql import enable_sync
from workspaces.rls import enable_rls

SYNCED_TABLES = [
    'ledger_personal_savingsgoal',
]
HISTORY_TABLES = [
    'ledger_personal_savingsgoalevent',
]


class Migration(migrations.Migration):

    dependencies = [
        ('ledger_personal', '0012_savings_goals'),
        ('sync', '0002_server_seq'),
    ]

    operations = (
        [operation for table in SYNCED_TABLES + HISTORY_TABLES for operation in enable_rls(table)]
        + [operation for table in SYNCED_TABLES for operation in enable_sync(table)]
    )
