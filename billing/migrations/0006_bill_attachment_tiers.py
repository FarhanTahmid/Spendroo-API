from django.db import migrations

from billing.seed import seed

STORAGE = 'storage.attachments_mb'
# Free kept no bills at all; it now keeps 10 MB of photos
OLD_FREE, NEW_FREE = 0, 10


def _models(apps):
    return [apps.get_model('billing', name) for name in ('Feature', 'Plan', 'PlanFeature', 'BillingSettings')]


def _bump_catalog(apps):
    # The catalog moved: apps ask for their entitlements again after their next sync
    Stamp = apps.get_model('billing', 'Stamp')
    stamp, created = Stamp.objects.get_or_create(scope='catalog')
    if not created:
        Stamp.objects.filter(pk=stamp.pk).update(version=stamp.version + 1)


def _set_free_storage(apps, old, new):
    """Only a Free row still at the old default moves: a value set in the admin is kept."""
    PlanFeature = apps.get_model('billing', 'PlanFeature')
    PlanFeature.objects.filter(
        plan__code='free', feature__key=STORAGE, limit=old, unlimited=False,
    ).update(limit=new)


def bills_for_every_plan(apps, schema_editor):
    """
    Bills come to every plan, held to a size per file and a total: Free keeps
    10 MB of photos, two megabytes each; the paid plans also take PDFs.
    Seeding adds the two new keys with their defaults.
    """
    _set_free_storage(apps, OLD_FREE, NEW_FREE)
    seed(*_models(apps))
    _bump_catalog(apps)


def bills_for_paid_plans_only(apps, schema_editor):
    Feature = apps.get_model('billing', 'Feature')
    Feature.objects.filter(key__in=['storage.attachment_file_mb', 'storage.attachment_pdf']).delete()
    _set_free_storage(apps, NEW_FREE, OLD_FREE)
    _bump_catalog(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0005_savings_goals_limit'),
    ]

    operations = [
        migrations.RunPython(bills_for_every_plan, bills_for_paid_plans_only),
    ]
