from django.db import migrations

from billing.seed import seed

KEY = 'insights.business_advanced'


def _models(apps):
    return [apps.get_model('billing', name) for name in ('Feature', 'Plan', 'PlanFeature', 'BillingSettings')]


def _bump_catalog(apps):
    # The catalog moved: apps ask for their entitlements again after their next sync
    Stamp = apps.get_model('billing', 'Stamp')
    stamp, created = Stamp.objects.get_or_create(scope='catalog')
    if not created:
        Stamp.objects.filter(pk=stamp.pk).update(version=stamp.version + 1)


def add_business_insights(apps, schema_editor):
    """The advanced charts on a business and its books come with the Business plan."""
    seed(*_models(apps))
    _bump_catalog(apps)


def remove_business_insights(apps, schema_editor):
    apps.get_model('billing', 'Feature').objects.filter(key=KEY).delete()
    _bump_catalog(apps)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0006_bill_attachment_tiers'),
    ]

    operations = [
        migrations.RunPython(add_business_insights, remove_business_insights),
    ]
