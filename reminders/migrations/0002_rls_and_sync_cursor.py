from django.db import migrations

from sync.sql import enable_sync
from workspaces.rls import enable_rls

SYNCED_TABLES = [
    'reminders_rule',
    'reminders_occurrence',
    'reminders_inboxitem',
]


class Migration(migrations.Migration):

    dependencies = [
        ('reminders', '0001_initial'),
        ('sync', '0002_server_seq'),
    ]

    operations = (
        [operation for table in SYNCED_TABLES for operation in enable_rls(table)]
        + [operation for table in SYNCED_TABLES for operation in enable_sync(table)]
    )
