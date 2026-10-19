import cashbook.bill_storage
import cashbook.models
import django.db.models.deletion
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import migrations, models

from sync.guard import guard_deletes


def move_files_out_of_media(apps, schema_editor):
    """
    Bills were kept under MEDIA_ROOT, which DEBUG serves to anyone. They move
    to the private bills storage under the same names.
    """
    EntryBills = apps.get_model('cashbook', 'EntryBills')
    bills = cashbook.bill_storage.bill_storage
    for name in EntryBills.objects.exclude(bill_file='').values_list('bill_file', flat=True):
        if bills.exists(name) or not default_storage.exists(name):
            continue
        with default_storage.open(name, 'rb') as source:
            bills.save(name, source)
        default_storage.delete(name)


class Migration(migrations.Migration):
    # Apart from 0011: PostgreSQL will not alter a table in the transaction that just filled its new column

    dependencies = [
        ('cashbook', '0011_bill_attachments'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='entrybills',
            name='cashbook',
            field=models.ForeignKey(editable=False, on_delete=django.db.models.deletion.CASCADE, related_name='bills', to='cashbook.cashbook'),
        ),
        migrations.AddField(
            model_name='entrybills',
            name='content_type',
            field=models.CharField(default='', editable=False, max_length=40),
        ),
        migrations.AddField(
            model_name='entrybills',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bills_added', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='entrybills',
            name='original_name',
            field=models.CharField(blank=True, default='', editable=False, max_length=120),
        ),
        migrations.AddField(
            model_name='entrybills',
            name='page_count',
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='entrybills',
            name='width',
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='entrybills',
            name='height',
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name='entrybills',
            name='bill_file',
            field=models.FileField(max_length=300, storage=cashbook.bill_storage.get_bill_storage, upload_to=cashbook.models.get_user_bill_filepath),
        ),
        migrations.RunPython(move_files_out_of_media, migrations.RunPython.noop),
        # Bills now sync (sync/registry.py): phones learn of a removal only from its tombstone
        *guard_deletes('cashbook_entrybills'),
    ]
