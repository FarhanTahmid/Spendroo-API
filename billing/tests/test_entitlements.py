from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from billing import entitlements as ent
from billing.catalog.keys import F
from billing.entitlements import explain, for_workspace, resolve
from billing.models import BillingSettings, Campaign, EntitlementOverride, Feature, Plan, Subscription
from billing.offers import grant, revoke
from billing.testing import grant_plan, set_flag, set_limit, set_plan_value
from workspaces.services import create_workspace

from .base import BillingTestCase


class ResolveTests(BillingTestCase):
    def test_a_new_user_is_on_free_without_any_row(self):
        self.assertFalse(Subscription.objects.exists())
        entitlements = resolve(self.alice)
        self.assertEqual((entitlements.plan.code, entitlements.source), ('free', 'default'))
        self.assertIsNone(entitlements.expires_at)
        self.assertEqual(entitlements.limit_of(F.PERSONAL_ACCOUNTS), 4)
        self.assertFalse(entitlements.can(F.BUDGET_MONTH_OVERRIDE))
        self.assertTrue(entitlements.can(F.ADS))

    def test_a_grant_moves_the_user_and_ends_on_time(self):
        subscription = grant_plan(self.alice, 'plus', days=30)
        entitlements = resolve(self.alice)
        self.assertEqual((entitlements.plan.code, entitlements.source), ('plus', 'grant'))
        self.assertEqual(entitlements.expires_at, subscription.current_period_end)
        self.assertIsNone(entitlements.limit_of(F.PERSONAL_ACCOUNTS))
        self.assertTrue(entitlements.value(F.PERSONAL_ACCOUNTS).unlimited)
        self.assertFalse(entitlements.can(F.ADS))

        later = timezone.now() + timedelta(days=31)
        self.assertEqual(resolve(self.alice, now=later).plan.code, 'free')
        # And nobody else moved
        self.assertEqual(resolve(self.bob).plan.code, 'free')

    def test_the_highest_plan_held_wins(self):
        grant_plan(self.alice, 'business', days=5)
        grant_plan(self.alice, 'plus')
        self.assertEqual(resolve(self.alice).plan.code, 'business')
        self.assertEqual(resolve(self.alice, now=timezone.now() + timedelta(days=6)).plan.code, 'plus')

    def test_a_subscription_that_has_not_started_or_was_revoked_does_not_count(self):
        future = grant_plan(self.alice, 'plus')
        Subscription.objects.filter(pk=future.pk).update(starts_at=timezone.now() + timedelta(days=1))
        self.assertEqual(resolve(self.alice).plan.code, 'free')

        current = grant_plan(self.alice, 'business')
        self.assertEqual(resolve(self.alice).plan.code, 'business')
        revoke(current, 'mistake')
        self.assertEqual(resolve(self.alice).plan.code, 'free')
        self.assertIn('revoked: mistake', Subscription.objects.get(pk=current.pk).reason)

    def test_only_a_paid_plan_gets_a_grace_period(self):
        ended = timezone.now() - timedelta(days=1)
        grant(self.alice, Plan.objects.get(code='plus'), until=ended, source=Subscription.SOURCE_STORE)
        grant(self.bob, Plan.objects.get(code='plus'), until=ended, source=Subscription.SOURCE_GRANT)

        paid = resolve(self.alice)
        self.assertEqual((paid.plan.code, paid.in_grace), ('plus', True))
        self.assertEqual(resolve(self.bob).plan.code, 'free')

        after_grace = ended + timedelta(days=BillingSettings.load().grace_days, minutes=1)
        self.assertEqual(resolve(self.alice, now=after_grace).plan.code, 'free')

    def test_a_workspace_runs_on_its_owners_plan(self):
        grant_plan(self.alice, 'business')
        self.assertEqual(for_workspace(self.shop).plan.code, 'business')
        self.assertEqual(for_workspace(create_workspace(owner=self.bob, name='Bob shop')).plan.code, 'free')

    def test_editing_a_plan_applies_to_everyone_on_it_at_once(self):
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 4)
        set_plan_value('free', F.PERSONAL_ACCOUNTS, limit=6)
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 6)
        self.assertEqual(resolve(self.bob).limit_of(F.PERSONAL_ACCOUNTS), 6)
        set_plan_value('free', F.BUDGET_MONTH_OVERRIDE, enabled=True)
        self.assertTrue(resolve(self.alice).can(F.BUDGET_MONTH_OVERRIDE))

    def test_a_feature_the_plan_does_not_mention_is_off(self):
        Feature.objects.get(key=F.PERSONAL_ACCOUNTS.key).plan_values.all().delete()
        value = resolve(self.alice).value(F.PERSONAL_ACCOUNTS)
        self.assertEqual((value.limit, value.allowed), (0, False))
        self.assertFalse(resolve(self.alice).can('made.up_key'))

    def test_a_flag_only_the_admin_knows_reaches_the_user(self):
        labs = Feature.objects.create(key='labs.dark_charts', kind='flag', name='Dark charts')
        Plan.objects.get(code='free').values.create(feature=labs, enabled=True)
        self.assertTrue(resolve(self.alice).can('labs.dark_charts'))
        self.assertTrue(self.entitlements()['features']['labs.dark_charts']['enabled'])


class OverrideTests(BillingTestCase):
    def test_set_and_add(self):
        set_limit(self.alice, F.PERSONAL_ACCOUNTS, 10)
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 10)
        set_limit(self.alice, F.AI_CREDITS, 50, mode=EntitlementOverride.MODE_ADD)
        self.assertEqual(resolve(self.alice).limit_of(F.AI_CREDITS), 65)
        set_flag(self.alice, F.BUDGET_MONTH_OVERRIDE, True)
        self.assertTrue(resolve(self.alice).can(F.BUDGET_MONTH_OVERRIDE))
        self.assertEqual(resolve(self.bob).limit_of(F.PERSONAL_ACCOUNTS), 4)

    def test_unlimited_and_adding_to_unlimited(self):
        set_limit(self.alice, F.PERSONAL_ACCOUNTS, None)
        self.assertTrue(resolve(self.alice).value(F.PERSONAL_ACCOUNTS).unlimited)
        set_limit(self.alice, F.PERSONAL_ACCOUNTS, 3, mode=EntitlementOverride.MODE_ADD)
        self.assertTrue(resolve(self.alice).value(F.PERSONAL_ACCOUNTS).unlimited)

    def test_an_exact_override_can_take_away_too(self):
        grant_plan(self.alice, 'plus')
        set_flag(self.alice, F.BUDGET_MONTH_OVERRIDE, False)
        self.assertFalse(resolve(self.alice).can(F.BUDGET_MONTH_OVERRIDE))
        set_limit(self.alice, F.PERSONAL_ACCOUNTS, 2)
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 2)

    def test_at_least_never_holds_a_user_below_a_better_plan(self):
        """The default kind of override: a boost on Free must not become a cap after upgrading."""
        self.assertEqual(EntitlementOverride._meta.get_field('mode').default, EntitlementOverride.MODE_RAISE)
        raise_to = EntitlementOverride.MODE_RAISE
        set_limit(self.alice, F.PERSONAL_ACCOUNTS, 6, mode=raise_to)
        set_limit(self.alice, F.BUSINESS_WORKSPACES, 1, mode=raise_to)
        set_flag(self.alice, F.BUDGET_MONTH_OVERRIDE, True, mode=raise_to)
        set_flag(self.alice, F.REPORTS_EXCEL, False, mode=raise_to)

        free = resolve(self.alice)
        self.assertEqual(free.limit_of(F.PERSONAL_ACCOUNTS), 6)
        self.assertEqual(free.limit_of(F.BUSINESS_WORKSPACES), 1)
        self.assertTrue(free.can(F.BUDGET_MONTH_OVERRIDE))
        self.assertFalse(free.can(F.REPORTS_EXCEL))

        grant_plan(self.alice, 'plus')
        plus = resolve(self.alice)
        self.assertTrue(plus.value(F.PERSONAL_ACCOUNTS).unlimited)
        self.assertEqual(plus.limit_of(F.BUSINESS_WORKSPACES), 2)
        self.assertTrue(plus.can(F.REPORTS_EXCEL))

    def test_an_override_only_counts_inside_its_window(self):
        override = set_limit(self.alice, F.PERSONAL_ACCOUNTS, 10)
        now = timezone.now()
        EntitlementOverride.objects.filter(pk=override.pk).update(
            starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=2),
        )
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 4)
        self.assertEqual(resolve(self.alice, now=now + timedelta(days=1, hours=1)).limit_of(F.PERSONAL_ACCOUNTS), 10)
        self.assertEqual(resolve(self.alice, now=now + timedelta(days=3)).limit_of(F.PERSONAL_ACCOUNTS), 4)

    def test_an_override_survives_a_plan_change(self):
        set_limit(self.alice, F.BUSINESS_WORKSPACES, 9)
        grant_plan(self.alice, 'plus')
        self.assertEqual(resolve(self.alice).limit_of(F.BUSINESS_WORKSPACES), 9)

    def test_model_validation(self):
        feature = Feature.objects.get(key=F.ADS.key)
        with self.assertRaisesMessage(Exception, 'only be set'):
            EntitlementOverride(user=self.alice, feature=feature, mode='add', reason='x').clean()
        limit = Feature.objects.get(key=F.DEVICES_MAX.key)
        with self.assertRaisesMessage(Exception, 'number to add'):
            EntitlementOverride(user=self.alice, feature=limit, mode='add', reason='x').clean()
        with self.assertRaisesMessage(Exception, 'or tick unlimited'):
            EntitlementOverride(user=self.alice, feature=limit, mode='set', reason='x').clean()


class CampaignPlanTests(BillingTestCase):
    def campaign(self, **fields):
        now = timezone.now()
        defaults = {
            'slug': 'eid', 'name': 'Eid', 'title': 'Eid gift', 'is_active': True,
            'starts_at': now - timedelta(hours=1), 'ends_at': now + timedelta(days=2),
            'kind': Campaign.BENEFIT_PLAN, 'benefit_plan': Plan.objects.get(code='plus'), 'benefit_days': 7,
            'auto_apply': True,
        }
        return Campaign.objects.create(**{**defaults, **fields})

    def test_a_campaign_that_applies_by_itself_lifts_its_audience_while_it_runs(self):
        campaign = self.campaign()
        entitlements = resolve(self.alice)
        self.assertEqual((entitlements.plan.code, entitlements.source), ('plus', 'campaign'))
        self.assertEqual((entitlements.expires_at, entitlements.campaign_slug), (campaign.ends_at, 'eid'))
        self.assertEqual(resolve(self.alice, now=campaign.ends_at + timedelta(seconds=1)).plan.code, 'free')
        self.assertFalse(Subscription.objects.exists(), 'lifting everyone must not write a row per user')

        Campaign.objects.filter(pk=campaign.pk).update(is_active=False)
        self.assertEqual(resolve(self.alice).plan.code, 'free')

    def test_it_never_lowers_a_plan_and_respects_the_audience(self):
        grant_plan(self.alice, 'business')
        campaign = self.campaign(audience=Campaign.AUDIENCE_USERS)
        campaign.audience_users.add(self.bob)
        self.assertEqual(resolve(self.alice).plan.code, 'business')
        self.assertEqual(resolve(self.bob).plan.code, 'plus')

    def test_audience_by_plan_and_sign_up_date(self):
        campaign = self.campaign(audience=Campaign.AUDIENCE_PLANS)
        campaign.audience_plans.add(Plan.objects.get(code='free'))
        self.assertEqual(resolve(self.alice).plan.code, 'plus')

        Campaign.objects.filter(pk=campaign.pk).update(joined_after=timezone.now() + timedelta(days=1))
        self.assertEqual(resolve(self.alice).plan.code, 'free')
        Campaign.objects.filter(pk=campaign.pk).update(
            joined_after=None, joined_before=self.alice.date_joined - timedelta(seconds=1),
        )
        self.assertEqual(resolve(self.alice).plan.code, 'free')


class ExplainTests(BillingTestCase):
    def test_every_value_says_where_it_came_from(self):
        grant_plan(self.alice, 'plus')
        set_limit(self.alice, F.BUSINESS_WORKSPACES, 7)
        entitlements, rows = explain(self.alice)
        by_key = {row['key']: row for row in rows}
        self.assertEqual(entitlements.plan.code, 'plus')
        self.assertEqual(by_key[F.BUSINESS_WORKSPACES.key]['value'], '7')
        self.assertEqual(len(by_key[F.BUSINESS_WORKSPACES.key]['steps']), 2)
        self.assertIn('plan plus: 2', by_key[F.BUSINESS_WORKSPACES.key]['steps'][0])
        self.assertIn('override', by_key[F.BUSINESS_WORKSPACES.key]['steps'][1])
        self.assertEqual(by_key[F.PERSONAL_ACCOUNTS.key]['value'], 'unlimited')

    def test_command(self):
        grant_plan(self.alice, 'business', days=3)
        out = StringIO()
        call_command('billing_explain', self.alice.email, stdout=out)
        self.assertIn('Business (from grant', out.getvalue())
        self.assertIn('team.seats', out.getvalue())

    def test_grant_command(self):
        call_command('billing_grant', self.alice.email, 'plus', '--days', '10', stdout=StringIO())
        self.assertEqual(resolve(self.alice).plan.code, 'plus')
        with self.assertRaisesMessage(Exception, 'No active plan'):
            call_command('billing_grant', self.alice.email, 'gold', stdout=StringIO())
        with self.assertRaisesMessage(Exception, 'No user'):
            call_command('billing_grant', 'nobody@example.com', 'plus', stdout=StringIO())


class RequestCacheTests(BillingTestCase):
    def test_nothing_is_remembered_outside_a_request(self):
        self.assertEqual(resolve(self.alice).plan.code, 'free')
        Subscription.objects.create(user=self.alice, plan=Plan.objects.get(code='plus'), source='grant')
        self.assertEqual(resolve(self.alice).plan.code, 'plus')

    def test_one_request_resolves_once_and_forget_clears_it(self):
        token = ent.begin_request_cache()
        try:
            first = resolve(self.alice)
            Subscription.objects.create(user=self.alice, plan=Plan.objects.get(code='plus'), source='grant')
            self.assertIs(resolve(self.alice), first)
            ent.forget()
            self.assertEqual(resolve(self.alice).plan.code, 'plus')
        finally:
            ent.end_request_cache(token)
        self.assertIsNone(ent._request_cache.get())
