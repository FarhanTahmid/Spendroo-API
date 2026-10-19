"""
Tests that fail when something new is added without deciding how the plan
applies to it, so nothing can be left unpaywalled by forgetting.
"""
import os
import subprocess
import sys

from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

from billing import gates
from billing.catalog.keys import ALL, F, LIMIT, QUOTA
from billing.rules import RULES
from billing.sync_gates import Gate
from sync.registry import TABLES
from workspaces.tests import TENANT_APPS

from .base import BillingTestCase

UNSAFE = {'post', 'put', 'patch', 'delete'}

# How every feature key is enforced. A new key has to be put in one of these.
ENFORCED_BY_THE_SERVER = {
    F.PERSONAL_ACCOUNTS, F.PERSONAL_CUSTOM_CATEGORIES, F.BUDGET_MONTH_OVERRIDE,
    F.RECURRING_TRANSACTIONS, F.REMINDERS_STANDALONE, F.SAVINGS_GOALS,
    F.BUSINESS_WORKSPACES, F.BUSINESS_CASHBOOKS, F.TEAM_SEATS,
    F.REPORTS_EXCEL, F.REPORTS_CUSTOM_RANGE, F.ENTRY_CUSTOM_FIELDS, F.STORAGE_ATTACHMENTS_MB,
    F.INSIGHTS_TREND_MONTHS, F.INSIGHTS_YEAR_REVIEW, F.AUDIT_TRAIL,
}
# Limits on a size (megabytes, months), checked where it is asked for rather than by counting rows
NOT_ROW_COUNTS = {F.STORAGE_ATTACHMENTS_MB, F.INSIGHTS_TREND_MONTHS}
# Metered by gates.take / gates.consume; the features that spend them come later
METERED_FOR_LATER = {F.AI_CREDITS, F.STATEMENT_PDF_PAGES}
# Shown or hidden by the app from data it already holds; nothing reaches the server
DECIDED_IN_THE_APP = {
    F.ADS, F.INSIGHTS_CUSTOM_RANGE,
    F.HOME_WIDGET,
}
# Defined and shown, deliberately not enforced yet
NOT_ENFORCED_YET = {F.DEVICES_MAX}


def api_views(patterns=None, prefix=''):
    """(path, view class) for every class-based view under /api/v1/."""
    for pattern in (get_resolver().url_patterns if patterns is None else patterns):
        path = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from api_views(pattern.url_patterns, path)
        elif isinstance(pattern, URLPattern) and path.startswith('api/v1/'):
            view = getattr(pattern.callback, 'cls', None) or getattr(pattern.callback, 'view_class', None)
            if view is not None:
                yield path, view, getattr(pattern.callback, 'actions', None)


def accepts_writes(view, actions):
    if actions is not None:  # a viewset route: the methods this URL maps
        return bool(UNSAFE & set(actions))
    return any(hasattr(view, method) for method in UNSAFE)


class EveryFeatureIsAccountedForTests(SimpleTestCase):
    def test_every_key_says_how_it_is_enforced(self):
        groups = [ENFORCED_BY_THE_SERVER, METERED_FOR_LATER, DECIDED_IN_THE_APP, NOT_ENFORCED_YET]
        placed = [feature for group in groups for feature in group]
        self.assertEqual(len(placed), len(set(placed)), 'a feature is in two groups')
        missing = set(ALL) - set(placed)
        self.assertFalse(missing, f'Decide how these are enforced: {sorted(f.key for f in missing)}')

    def test_every_counted_limit_has_a_rule_and_every_rule_a_limit(self):
        counted = {
            feature.key for feature in ENFORCED_BY_THE_SERVER
            if feature.kind == LIMIT and feature not in NOT_ROW_COUNTS
        }
        self.assertEqual(set(RULES), counted)

    def test_quotas_are_metered(self):
        self.assertEqual({feature for feature in ALL if feature.kind == QUOTA}, METERED_FOR_LATER)


class EveryWritePathIsGatedTests(SimpleTestCase):
    def test_every_synced_table_declares_its_plan_gates(self):
        for name, table in TABLES.items():
            self.assertIsNotNone(
                table.plan_gates,
                f"sync table '{name}' does not say what the plan limits. "
                "Set plan_gates in sync/registry.py (NO_GATES if nothing).",
            )
            for gate in table.plan_gates:
                self.assertIsInstance(gate, Gate)

    def test_every_tenant_model_is_reachable_only_through_a_gated_table_or_view(self):
        synced = {table.model for table in TABLES.values()}
        # Written only through cashbook/views/entry_viewset.py, which is gated
        rest_only = {'cashbook.EntryBills', 'cashbook.EntryExtraFields'}
        for app_label in TENANT_APPS:
            for model in apps.get_app_config(app_label).get_models():
                if model._meta.label.endswith('Event'):
                    continue  # history tables, written by triggers
                self.assertTrue(
                    model in synced or model._meta.label in rest_only,
                    f'{model._meta.label} is tenant data with no gated way in. Add it to sync/registry.py '
                    'with plan_gates, or list it here with the gated view that writes it.',
                )

    def test_every_view_that_takes_writes_says_how_the_plan_applies(self):
        seen = 0
        for path, view, actions in api_views():
            if not accepts_writes(view, actions):
                continue
            seen += 1
            gate = getattr(view, 'billing_gate', None)
            self.assertIsNotNone(
                gate,
                f'{view.__name__} ({path}) takes writes and has no billing_gate. Set it to gates.GATED and '
                "check the plan in the view, or to gates.exempt('why no plan limits this').",
            )
            if gate != gates.GATED:
                self.assertEqual(gate[0], 'exempt', view.__name__)
                self.assertTrue(gate[1].strip(), f'{view.__name__} is exempt without a reason')
        self.assertGreater(seen, 10, 'the URL walk found too few views to mean anything')

    def test_no_billing_table_can_be_synced_or_is_tenant_data(self):
        billing_models = set(apps.get_app_config('billing').get_models())
        self.assertFalse(billing_models & {table.model for table in TABLES.values()})
        self.assertNotIn('billing', TENANT_APPS)

    def test_refusals_are_402(self):
        self.assertEqual(gates.PlanLimit.status_code, 402)
        self.assertEqual(gates.PlanLimit.default_code, 'plan_limit')


class BillingEndpointsTests(SimpleTestCase):
    def billing_routes(self):
        return {path: view for path, view, _ in api_views() if path.startswith('api/v1/billing/')}

    def test_no_billing_endpoint_takes_a_user_in_its_url(self):
        for path in self.billing_routes():
            self.assertNotIn('user', path)
            self.assertNotIn('<uuid', path)

    def test_only_these_billing_endpoints_take_writes(self):
        writable = sorted(
            path for path, view in self.billing_routes().items() if accepts_writes(view, None)
        )
        self.assertEqual(writable, [
            'api/v1/billing/dev/simulate/',
            'api/v1/billing/events/',
            'api/v1/billing/keep/<str:feature>/',
            'api/v1/billing/offers/<slug:slug>/claim/',
            'api/v1/billing/promo/redeem/',
        ])

    def test_guessable_endpoints_are_throttled(self):
        from billing.api import views
        rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
        for view in (views.PromoRedeemView, views.OfferClaimView, views.KeepChoiceView, views.FunnelEventView):
            self.assertIn(view.throttle_scope, rates, view.__name__)
        self.assertEqual(rates['promo_redeem'], '10/hour')


class DevToolsAreNotInProductionTests(SimpleTestCase):
    def test_the_route_exists_only_with_the_setting(self):
        import importlib

        from billing.api import urls

        def names():
            return {pattern.name for pattern in urls.urlpatterns}
        try:
            with override_settings(BILLING_DEV_TOOLS=False):
                importlib.reload(urls)
                self.assertNotIn('dev-simulate', names())
            with override_settings(BILLING_DEV_TOOLS=True):
                importlib.reload(urls)
                self.assertIn('dev-simulate', names())
        finally:
            importlib.reload(urls)

    def run_production_settings(self, **extra):
        env = {
            **os.environ, 'PROJECT_ENVIRONMENT': 'production', 'SECRET_KEY': 'test-only',
            'FIELD_ENCRYPTION_KEYS': 'test-only', 'ALLOWED_HOSTS': 'api.example.com', **extra,
        }
        return subprocess.run(
            [sys.executable, '-c', 'import spendroo.settings as s; print("DEV_TOOLS", s.BILLING_DEV_TOOLS)'],
            cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=120,
        )

    def test_production_settings_keep_them_off(self):
        result = self.run_production_settings()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DEV_TOOLS False', result.stdout)

    def test_production_refuses_to_start_with_them_on(self):
        result = self.run_production_settings(BILLING_DEV_TOOLS='true')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('BILLING_DEV_TOOLS must not be set in production', result.stderr)


class SyncCannotTouchBillingTests(BillingTestCase):
    def test_a_push_naming_a_billing_table_is_malformed(self):
        for table in ('subscriptions', 'billing_subscription', 'plans', 'entitlement_overrides', 'usage_counters'):
            result = self.push_one(self.upsert(table, plan='business', user_id=str(self.alice.id)))
            self.assertEqual((result['status'], result['error']['code']), ('rejected', 'malformed'), table)

    def test_a_push_cannot_set_server_owned_columns(self):
        mutation = self.upsert(
            'accounts', name='Wallet', kind='cash', currency='BDT',
            workspace_id=str(self.shop.id), server_seq=999999, version=50, is_locked=False,
        )
        result = self.push_one(mutation)
        self.assertApplied(result)
        self.assertEqual((str(result['row']['workspace_id']), result['row']['version']), (str(self.personal.id), 1))
