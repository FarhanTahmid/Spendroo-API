import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def fill_cashbooks(apps, schema_editor):
    EntryBills = apps.get_model('cashbook', 'EntryBills')
    for bill in EntryBills.objects.filter(cashbook__isnull=True).select_related('entry'):
        EntryBills.objects.filter(pk=bill.pk).update(cashbook_id=bill.entry.cashbook_id)


class Migration(migrations.Migration):

    dependencies = [
        ('cashbook', '0010_grant_contributor'),
        ('sync', '0004_no_hard_deletes'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='entrybills',
            options={'ordering': ['created_at'], 'verbose_name': 'Bill of Entry', 'verbose_name_plural': 'Bills of Entries'},
        ),
        migrations.AddField(
            model_name='entrybills',
            name='cashbook',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='bills', to='cashbook.cashbook'),
        ),
        migrations.RunPython(fill_cashbooks, migrations.RunPython.noop),
    ]
