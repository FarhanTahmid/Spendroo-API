from django.db import migrations

from sync.guard import guard_deletes
from sync.sql import enable_sync
from workspaces.rls import enable_rls

SYNCED_TABLES = [
    'cashbook_entryfield',
]
HISTORY_TABLES = [
    'cashbook_entryfieldevent',
]


class Migration(migrations.Migration):

    dependencies = [
        ('cashbook', '0013_entry_fields'),
        ('sync', '0004_no_hard_deletes'),
    ]

    operations = (
        [operation for table in SYNCED_TABLES + HISTORY_TABLES for operation in enable_rls(table)]
        + [operation for table in SYNCED_TABLES for operation in enable_sync(table)]
        + [operation for table in SYNCED_TABLES for operation in guard_deletes(table)]
    )
