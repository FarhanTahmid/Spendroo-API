from django.db import migrations

from billing.seed import seed

KEY = 'goals.savings'
# plan code -> goals in use at one time (None: unlimited)
LIMITS = {'free': 1, 'plus': None, 'business': None}


def _as_limit(limit):
    return {'enabled': False, 'limit': limit, 'unlimited': limit is None}


def goals_become_a_limit(apps, schema_editor):
    """
    Savings goals were a flag: Free had none. Free now gets one goal and the
    paid plans any number, so the flag turns into a limit. The old on/off
    values say nothing about a number, so every plan's row is written anew;
    seeding alone would keep them, as it only adds what is missing.
    """
    Feature = apps.get_model('billing', 'Feature')
    Plan = apps.get_model('billing', 'Plan')
    PlanFeature = apps.get_model('billing', 'PlanFeature')
    EntitlementOverride = apps.get_model('billing', 'EntitlementOverride')

    feature = Feature.objects.filter(key=KEY).first()
    if feature is not None:
        Feature.objects.filter(pk=feature.pk).update(
            kind='limit', description='Goals in use at one time. Archived goals do not count.',
        )
        for plan in Plan.objects.filter(code__in=LIMITS):
            PlanFeature.objects.update_or_create(
                plan=plan, feature=feature, defaults=_as_limit(LIMITS[plan.code]),
            )
        # A plan nobody wrote down here keeps what it had: on means any number
        others = PlanFeature.objects.filter(feature=feature).exclude(plan__code__in=LIMITS)
        for row in others:
            PlanFeature.objects.filter(pk=row.pk).update(**_as_limit(None if row.enabled else 0))
        # A user given the flag gets any number; one denied it gets none
        for override in EntitlementOverride.objects.filter(feature=feature):
            EntitlementOverride.objects.filter(pk=override.pk).update(
                **_as_limit(None if override.enabled else 0), mode='set' if not override.enabled else override.mode,
            )

    # A fresh database gets the feature here, already a limit
    seed(Feature, Plan, PlanFeature, apps.get_model('billing', 'BillingSettings'))
    # The catalog moved: apps ask for their entitlements again after their next sync
    Stamp = apps.get_model('billing', 'Stamp')
    stamp, created = Stamp.objects.get_or_create(scope='catalog')
    if not created:
        Stamp.objects.filter(pk=stamp.pk).update(version=stamp.version + 1)


def goals_become_a_flag(apps, schema_editor):
    Feature = apps.get_model('billing', 'Feature')
    PlanFeature = apps.get_model('billing', 'PlanFeature')
    EntitlementOverride = apps.get_model('billing', 'EntitlementOverride')
    feature = Feature.objects.filter(key=KEY).first()
    if feature is None:
        return
    Feature.objects.filter(pk=feature.pk).update(kind='flag', description='')
    for model in (PlanFeature, EntitlementOverride):
        for row in model.objects.filter(feature=feature):
            # One goal on Free was the new allowance; before, Free had none
            enabled = row.unlimited
            model.objects.filter(pk=row.pk).update(enabled=enabled, limit=None, unlimited=False)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0004_standalone_reminders'),
    ]

    operations = [
        migrations.RunPython(goals_become_a_limit, goals_become_a_flag),
    ]
