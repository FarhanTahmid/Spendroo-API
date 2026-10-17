from django.db import migrations

from sync.guard import CREATE_FUNCTION, DROP_FUNCTION, guard_deletes

# Every table in sync/registry.py. A new synced table adds guard_deletes() in its own migration
SYNCED_TABLES = [
    'cashbook_cashbook', 'cashbook_entrycategory', 'cashbook_paymentmethod', 'cashbook_entry',
    'cashbook_cashbookadditionalmember', 'ledger_personal_account', 'ledger_personal_savingsgoal',
    'ledger_personal_category', 'reminders_rule', 'reminders_occurrence', 'ledger_personal_transaction',
    'ledger_personal_budget', 'reminders_inboxitem',
]


class Migration(migrations.Migration):
    dependencies = [
        ('sync', '0003_syncmutation_error_meta'),
        ('cashbook', '0009_category_color'),
        ('ledger_personal', '0013_savings_goals_rls_and_sync_cursor'),
        ('reminders', '0003_goals'),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_FUNCTION, reverse_sql=DROP_FUNCTION),
        *[operation for table in SYNCED_TABLES for operation in guard_deletes(table)],
    ]
