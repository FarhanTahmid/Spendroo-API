from django.db import migrations

from billing.seed import seed

# old key -> (new key, name, description)
RENAMED = {
    'cashbook.report_export': (
        'reports.excel', 'Reports as Excel', 'Download any report as an Excel workbook. PDF is always included.',
    ),
    'cashbook.report_range': (
        'reports.custom_range', 'Custom reports',
        'A report between two chosen dates, narrowed to chosen kinds of transaction and categories.',
    ),
}


def rename_report_flags(apps, schema_editor):
    """
    Reports now cover personal mode too, and a PDF is for every plan. The two
    cashbook flags become the two report flags, keeping what each plan and each
    overridden user was given.
    """
    Feature = apps.get_model('billing', 'Feature')
    for old, (new, name, description) in RENAMED.items():
        if Feature.objects.filter(key=new).exists():
            # A database made after the rename: 0002 already added the new key
            Feature.objects.filter(key=old).delete()
        else:
            Feature.objects.filter(key=old).update(key=new, name=name, description=description)
    seed(
        Feature, apps.get_model('billing', 'Plan'),
        apps.get_model('billing', 'PlanFeature'), apps.get_model('billing', 'BillingSettings'),
    )
    # The catalog moved: apps ask for their entitlements again after their next sync
    Stamp = apps.get_model('billing', 'Stamp')
    stamp, created = Stamp.objects.get_or_create(scope='catalog')
    if not created:
        Stamp.objects.filter(pk=stamp.pk).update(version=stamp.version + 1)


def restore_cashbook_flags(apps, schema_editor):
    Feature = apps.get_model('billing', 'Feature')
    for old, (new, _, _) in RENAMED.items():
        Feature.objects.filter(key=new).update(key=old)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0002_seed_catalog'),
    ]

    operations = [
        migrations.RunPython(rename_report_flags, restore_cashbook_flags),
    ]
