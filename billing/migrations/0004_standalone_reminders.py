from django.db import migrations

from billing.seed import seed


def add_reminders_limit(apps, schema_editor):
    """Plain reminders get a limit of their own; recurring transactions stay a plan feature."""
    seed(
        apps.get_model('billing', 'Feature'), apps.get_model('billing', 'Plan'),
        apps.get_model('billing', 'PlanFeature'), apps.get_model('billing', 'BillingSettings'),
    )
    # The catalog moved: apps ask for their entitlements again after their next sync
    Stamp = apps.get_model('billing', 'Stamp')
    stamp, created = Stamp.objects.get_or_create(scope='catalog')
    if not created:
        Stamp.objects.filter(pk=stamp.pk).update(version=stamp.version + 1)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_report_flags'),
    ]

    operations = [
        migrations.RunPython(add_reminders_limit, migrations.RunPython.noop),
    ]
