from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ledger_personal.models import Account

from .. import offers, roles, usage
from ..catalog.keys import F
from ..entitlements import forget, resolve
from ..models import (
    BillingSettings, Campaign, EntitlementOverride, Feature, FunnelEvent, KeepSelection, Plan, PlanFeature,
    PromoCode, Subscription, UsageCounter,
)
from ..testing import grant_plan
from .base import BillingTestCase

User = get_user_model()


class AdminTestCase(BillingTestCase):
    """A superuser, someone in each staff role, and staff with no role at all."""

    def setUp(self):
        super().setUp()
        roles.ensure_roles()
        self.root = User.objects.create_superuser(email='root@example.com', password='pass-12345')
        self.manager = self.staff('manager@example.com', roles.BILLING_ADMIN)
        self.support = self.staff('support@example.com', roles.SUPPORT)
        self.nobody = self.staff('nobody@example.com')
        self.admin = self.signed_in(self.root)

    def staff(self, email, group=None):
        user = User.objects.create_user(email=email, password='pass-12345', is_staff=True)
        if group:
            user.groups.add(Group.objects.get(name=group))
        return user

    def signed_in(self, user):
        client = Client()
        client.force_login(user)
        return client

    def billing_page(self, user=None):
        return reverse('admin:billing_user', args=[(user or self.alice).pk])

    def plan(self, code):
        return Plan.objects.get(code=code)

    def value(self, plan_code, feature):
        return PlanFeature.objects.get(plan__code=plan_code, feature__key=feature.key)

    def matrix_data(self, **changes):
        """The grid as the page would post it unchanged, then with these cells replaced."""
        data = {}
        for row in PlanFeature.objects.select_related('feature'):
            name = f'v-{row.plan_id}-{row.feature_id}'
            if row.feature.kind == 'flag':
                if row.enabled:
                    data[name] = 'on'
            else:
                data[name] = '∞' if row.unlimited else str(row.limit or 0)
        for name, value in changes.items():
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
        return data

    def cell(self, plan_code, feature):
        row = self.value(plan_code, feature)
        return f'v-{row.plan_id}-{row.feature_id}'


class DashboardTests(AdminTestCase):
    def test_home_shows_the_business(self):
        grant_plan(self.alice, 'plus', days=3)
        FunnelEvent.objects.create(user=self.bob, kind=FunnelEvent.KIND_LIMIT_HIT, feature_key=F.PERSONAL_ACCOUNTS.key)
        response = self.admin.get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        billing = response.context['billing']
        counts = {row['plan'].code: row['count'] for row in billing['plans']}
        self.assertEqual(counts['plus'], 1)
        self.assertEqual(counts['free'], billing['total_users'] - 1)
        self.assertEqual(billing['paying'], 1)
        self.assertEqual(billing['ending_count'], 1)
        self.assertEqual(billing['hits'][0]['feature_key'], F.PERSONAL_ACCOUNTS.key)
        self.assertContains(response, 'alice@example.com')
        self.assertContains(response, 'Edit what plans give')

    def test_home_warns_when_limits_are_not_enforced(self):
        settings = BillingSettings.load()
        settings.enforcement_mode = BillingSettings.MODE_LOG_ONLY
        settings.save()
        response = self.admin.get(reverse('admin:index'))
        self.assertContains(response, 'Plan limits are not being enforced')

    def test_signing_in_at_the_login_page_itself_lands_on_the_dashboard(self):
        response = Client().post(reverse('admin:login'), {'username': 'root@example.com', 'password': 'pass-12345'})
        self.assertRedirects(response, reverse('admin:index'))

    def test_staff_without_a_role_see_no_numbers(self):
        response = self.signed_in(self.nobody).get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('billing', response.context)

    def test_every_list_opens(self):
        grant_plan(self.alice, 'plus', days=3)
        usage.take(self.alice, F.AI_CREDITS.key, 1, 'admin-test', limit=15)
        for model in (
            'feature', 'plan', 'product', 'subscription', 'entitlementoverride', 'keepselection', 'campaign',
            'promocode', 'promoredemption', 'campaignclaim', 'funnelevent', 'usagecounter', 'usageevent',
            'billingevent',
        ):
            response = self.admin.get(reverse(f'admin:billing_{model}_changelist'))
            self.assertEqual(response.status_code, 200, model)
        for name in ('identity_appuser', 'workspaces_workspace', 'cashbook_cashbook', 'ledger_personal_account',
                     'audit_crudlog', 'notifications_emaillog', 'auth_group'):
            response = self.admin.get(reverse(f'admin:{name}_changelist'))
            self.assertEqual(response.status_code, 200, name)

    def test_change_pages_open(self):
        subscription = grant_plan(self.alice, 'plus', days=3)
        pages = [
            reverse('admin:billing_plan_change', args=[self.plan('free').pk]),
            reverse('admin:billing_plan_add'),
            reverse('admin:billing_feature_change', args=[Feature.objects.get(key=F.AI_CREDITS.key).pk]),
            reverse('admin:billing_subscription_change', args=[subscription.pk]),
            reverse('admin:billing_subscription_add'),
            reverse('admin:billing_campaign_add'),
            reverse('admin:billing_promocode_add'),
            reverse('admin:billing_entitlementoverride_add'),
            reverse('admin:billing_billingsettings_change', args=[1]),
            reverse('admin:billing_plan_history'),
            reverse('admin:identity_appuser_change', args=[self.alice.pk]),
            reverse('admin:identity_appuser_add'),
        ]
        for page in pages:
            self.assertEqual(self.admin.get(page).status_code, 200, page)

    def test_settings_list_goes_straight_to_the_one_row(self):
        response = self.admin.get(reverse('admin:billing_billingsettings_changelist'))
        self.assertRedirects(response, reverse('admin:billing_billingsettings_change', args=[1]))

    def test_plan_preview_shows_what_the_app_gets(self):
        response = self.admin.get(reverse('admin:billing_billingsettings_preview_plans', args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'personal.accounts')


class MatrixTests(AdminTestCase):
    url = property(lambda self: reverse('admin:billing_plan_matrix'))

    def test_shows_every_plan_and_feature(self):
        response = self.admin.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['grid']), Feature.objects.count())
        self.assertEqual([plan.code for plan in response.context['plans']], ['free', 'plus', 'business'])

    def test_saving_unchanged_changes_nothing(self):
        before = PlanFeature.pgh_event_model.objects.count()
        response = self.admin.post(self.url, self.matrix_data(), follow=True)
        self.assertContains(response, 'Nothing was changed.')
        self.assertEqual(PlanFeature.pgh_event_model.objects.count(), before)

    def test_a_new_number_reaches_users_at_once(self):
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 4)
        data = self.matrix_data(**{self.cell('free', F.PERSONAL_ACCOUNTS): '6'})
        response = self.admin.post(self.url, data, follow=True)
        self.assertContains(response, '1 value(s) changed')
        forget()
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 6)
        self.assertEqual(self.entitlements()['features'][F.PERSONAL_ACCOUNTS.key]['limit'], 6)

    def test_unlimited_and_flags(self):
        data = self.matrix_data(**{
            self.cell('free', F.PERSONAL_ACCOUNTS): '∞',
            self.cell('free', F.REPORTS_EXCEL): 'on',
            self.cell('plus', F.HOME_WIDGET): None,
        })
        self.admin.post(self.url, data)
        self.assertTrue(self.value('free', F.PERSONAL_ACCOUNTS).unlimited)
        self.assertTrue(self.value('free', F.REPORTS_EXCEL).enabled)
        self.assertFalse(self.value('plus', F.HOME_WIDGET).enabled)

    def test_one_bad_cell_saves_nothing(self):
        data = self.matrix_data(**{
            self.cell('free', F.PERSONAL_ACCOUNTS): '9',
            self.cell('free', F.BUSINESS_CASHBOOKS): 'lots',
        })
        response = self.admin.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nothing was saved.')
        self.assertContains(response, 'lots')  # what was typed is still there to fix
        self.assertEqual(self.value('free', F.PERSONAL_ACCOUNTS).limit, 4)

    def test_negative_numbers_are_refused(self):
        data = self.matrix_data(**{self.cell('free', F.PERSONAL_ACCOUNTS): '-1'})
        self.assertContains(self.admin.post(self.url, data), 'Nothing was saved.')
        self.assertEqual(self.value('free', F.PERSONAL_ACCOUNTS).limit, 4)

    def test_support_can_look_but_not_save(self):
        support = self.signed_in(self.support)
        response = support.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_edit'])
        data = self.matrix_data(**{self.cell('free', F.PERSONAL_ACCOUNTS): '99'})
        self.assertEqual(support.post(self.url, data).status_code, 403)
        self.assertEqual(self.value('free', F.PERSONAL_ACCOUNTS).limit, 4)

    def test_billing_admin_can_save(self):
        data = self.matrix_data(**{self.cell('free', F.PERSONAL_ACCOUNTS): '5'})
        self.signed_in(self.manager).post(self.url, data)
        self.assertEqual(self.value('free', F.PERSONAL_ACCOUNTS).limit, 5)

    def test_staff_without_a_role_are_refused(self):
        self.assertEqual(self.signed_in(self.nobody).get(self.url).status_code, 403)

    def test_signed_out_goes_to_login(self):
        response = Client().get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_app_users_cannot_open_the_admin(self):
        client = Client()
        client.force_login(self.alice)
        for url in (self.url, self.billing_page(), reverse('admin:index')):
            response = client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn('login', response['Location'])


class PlanAdminTests(AdminTestCase):
    def test_duplicate_makes_a_hidden_copy(self):
        plus = self.plan('plus')
        response = self.admin.post(reverse('admin:billing_plan_duplicate', args=[plus.pk]))
        copy = Plan.objects.get(code='plus-copy')
        self.assertRedirects(response, reverse('admin:billing_plan_change', args=[copy.pk]))
        self.assertFalse(copy.is_public)
        self.assertFalse(copy.is_default)
        self.assertEqual(copy.values.count(), plus.values.count())
        self.assertTrue(copy.values.get(feature__key=F.PERSONAL_ACCOUNTS.key).unlimited)

    def test_support_cannot_duplicate(self):
        response = self.signed_in(self.support).post(reverse('admin:billing_plan_duplicate', args=[self.plan('plus').pk]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Plan.objects.filter(code='plus-copy').exists())

    def test_a_plan_in_use_or_the_default_cannot_be_deleted(self):
        grant_plan(self.alice, 'plus')
        for code in ('plus', 'free'):
            response = self.admin.post(reverse('admin:billing_plan_delete', args=[self.plan(code).pk]), {'post': 'yes'})
            self.assertEqual(response.status_code, 403, code)
            self.assertTrue(Plan.objects.filter(code=code).exists())

    def test_system_features_cannot_be_deleted(self):
        feature = Feature.objects.get(key=F.PERSONAL_ACCOUNTS.key)
        response = self.admin.post(reverse('admin:billing_feature_delete', args=[feature.pk]), {'post': 'yes'})
        self.assertEqual(response.status_code, 403)

    def test_restore_previews_then_puts_values_back(self):
        row = self.value('free', F.PERSONAL_ACCOUNTS)
        row.limit = 9
        row.save()
        # A test runs in one transaction, so every history entry carries the same time. Spread them out
        now = timezone.now()
        events = PlanFeature.pgh_event_model.objects.filter(pgh_obj_id=row.pk)
        events.filter(limit=4).update(pgh_created_at=now - timedelta(minutes=10))
        events.filter(limit=9).update(pgh_created_at=now - timedelta(minutes=1))
        before = (now - timedelta(minutes=5)).astimezone(timezone.get_current_timezone())
        url = reverse('admin:billing_plan_restore', args=[self.plan('free').pk])
        moment = {'moment_0': before.strftime('%Y-%m-%d'), 'moment_1': before.strftime('%H:%M:%S')}
        response = self.admin.post(url, moment)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['preview'])
        self.assertEqual(self.value('free', F.PERSONAL_ACCOUNTS).limit, 9)  # a preview changes nothing
        response = self.admin.post(url, {**moment, 'confirm': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.value('free', F.PERSONAL_ACCOUNTS).limit, 4)

    def test_restore_refuses_the_future(self):
        url = reverse('admin:billing_plan_restore', args=[self.plan('free').pk])
        later = timezone.now() + timedelta(days=2)
        response = self.admin.post(url, {
            'moment_0': later.strftime('%Y-%m-%d'), 'moment_1': '00:00:00', 'confirm': '1',
        })
        self.assertContains(response, 'That is in the future.')


class UserBillingTests(AdminTestCase):
    def post(self, client, **data):
        return client.post(self.billing_page(), data)

    def test_page_shows_plan_and_reasons(self):
        response = self.admin.get(self.billing_page())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['entitlements'].plan.code, 'free')
        self.assertContains(response, 'this is the plan everyone starts on')
        self.assertContains(response, 'personal.accounts')

    def test_unknown_user_is_404(self):
        url = reverse('admin:billing_user', args=['00000000-0000-0000-0000-000000000000'])
        self.assertEqual(self.admin.get(url).status_code, 404)

    def test_grant(self):
        response = self.post(self.admin, action='grant', **{
            'grant-plan': self.plan('plus').pk, 'grant-days': '', 'grant-reason': 'Beta tester',
        })
        self.assertRedirects(response, self.billing_page())
        subscription = Subscription.objects.get(user=self.alice)
        self.assertEqual(subscription.plan.code, 'plus')
        self.assertEqual(subscription.source, Subscription.SOURCE_GRANT)
        self.assertEqual(subscription.created_by, self.root)
        self.assertEqual(subscription.reason, 'Beta tester')
        self.assertIsNone(subscription.current_period_end)
        self.assertEqual(resolve(self.alice).plan.code, 'plus')

    def test_grant_needs_a_reason(self):
        response = self.post(self.admin, action='grant', **{'grant-plan': self.plan('plus').pk, 'grant-reason': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Subscription.objects.exists())

    def test_support_grants_up_to_thirty_days(self):
        support = self.signed_in(self.support)
        plus = self.plan('plus').pk
        for days in ('', '31', '365'):
            response = self.post(support, action='grant', **{
                'grant-plan': plus, 'grant-days': days, 'grant-reason': 'Asked nicely',
            })
            self.assertEqual(response.status_code, 200, days)
            self.assertFalse(Subscription.objects.exists(), days)
        response = self.post(support, action='grant', **{
            'grant-plan': plus, 'grant-days': '30', 'grant-reason': 'Lost a week to a bug',
        })
        self.assertEqual(response.status_code, 302)
        subscription = Subscription.objects.get(user=self.alice)
        self.assertLessEqual(subscription.current_period_end, timezone.now() + timedelta(days=30))

    def test_override_raises_one_limit(self):
        feature = Feature.objects.get(key=F.PERSONAL_ACCOUNTS.key)
        response = self.post(self.admin, action='override', **{
            'override-feature': feature.pk, 'override-mode': EntitlementOverride.MODE_RAISE,
            'override-limit': '7', 'override-reason': 'Accountant',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 7)
        self.assertEqual(resolve(self.bob).limit_of(F.PERSONAL_ACCOUNTS), 4)
        override = EntitlementOverride.objects.get(user=self.alice)
        self.assertEqual(override.created_by, self.root)

        response = self.post(self.admin, action='remove_override', id=override.pk)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(resolve(self.alice).limit_of(F.PERSONAL_ACCOUNTS), 4)

    def test_override_of_another_user_cannot_be_removed_from_this_page(self):
        override = EntitlementOverride.objects.create(
            user=self.bob, feature=Feature.objects.get(key=F.PERSONAL_ACCOUNTS.key), limit=9, reason='x',
        )
        self.assertEqual(self.post(self.admin, action='remove_override', id=override.pk).status_code, 404)
        self.assertTrue(EntitlementOverride.objects.filter(pk=override.pk).exists())

    def test_credits_top_up(self):
        feature = Feature.objects.get(key=F.AI_CREDITS.key)
        response = self.post(self.admin, action='credits', **{
            'credits-feature': feature.pk, 'credits-amount': '50', 'credits-reason': 'Apology',
        })
        self.assertEqual(response.status_code, 302)
        counter = UsageCounter.objects.get(user=self.alice, feature_key=F.AI_CREDITS.key)
        self.assertEqual(counter.bonus, 50)
        self.assertContains(self.admin.get(self.billing_page()), 'includes 50 extra')

    def test_revoke(self):
        subscription = grant_plan(self.alice, 'business')
        self.assertEqual(self.post(self.admin, action='revoke', id=subscription.pk).status_code, 302)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.STATUS_REVOKED)
        self.assertEqual(resolve(self.alice).plan.code, 'free')

    def test_locked_rows_are_listed_and_the_choice_can_be_cleared(self):
        subscription = grant_plan(self.alice, 'plus')
        accounts = [
            Account.objects.create(workspace=self.personal, name=f'Account {number}', kind='cash', currency='BDT')
            for number in range(6)
        ]
        self.age(accounts)
        offers.revoke(subscription, 'test')
        response = self.admin.get(self.billing_page())
        keep = [item for item in response.context['keep'] if item['feature'] == F.PERSONAL_ACCOUNTS]
        self.assertEqual(len(keep), 1)
        self.assertTrue(keep[0]['pending'])
        self.assertEqual(len(keep[0]['kept']), 4)
        # Six made here, and the one every personal workspace starts with
        self.assertEqual(len(keep[0]['locked']), Account.objects.filter(workspace=self.personal).count() - 4)

        chosen = [str(account.pk) for account in accounts[2:]]
        response = self.alice_client.put(
            f'/api/v1/billing/keep/{F.PERSONAL_ACCOUNTS.key}/', {'scope': str(self.personal.pk), 'ids': chosen}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(KeepSelection.objects.filter(user=self.alice).exists())

        response = self.post(self.signed_in(self.support), action='reset_keep', feature=F.PERSONAL_ACCOUNTS.key, scope=keep[0]['scope'])
        self.assertEqual(response.status_code, 302)
        self.assertFalse(KeepSelection.objects.filter(user=self.alice).exists())

    def test_support_sees_tenant_rows_only_for_the_count(self):
        """Row-level security hides tenant rows from staff; the billing page still has to count them."""
        subscription = grant_plan(self.alice, 'plus')
        for number in range(5):
            Account.objects.create(workspace=self.personal, name=f'Account {number}', kind='cash', currency='BDT')
        offers.revoke(subscription, 'test')
        response = self.signed_in(self.support).get(self.billing_page())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(item['feature'] == F.PERSONAL_ACCOUNTS for item in response.context['keep']))

    def test_staff_without_a_role_can_do_nothing(self):
        nobody = self.signed_in(self.nobody)
        self.assertEqual(nobody.get(self.billing_page()).status_code, 403)
        response = self.post(nobody, action='grant', **{
            'grant-plan': self.plan('business').pk, 'grant-days': '5', 'grant-reason': 'me',
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Subscription.objects.exists())

    def test_user_page_links_to_billing(self):
        response = self.admin.get(reverse('admin:identity_appuser_billing', args=[self.alice.pk]))
        self.assertRedirects(response, self.billing_page())


class BulkGrantTests(AdminTestCase):
    def choose(self, client, users):
        return client.post(reverse('admin:identity_appuser_changelist'), {
            'action': 'grant_plan_to_selected', '_selected_action': [str(user.pk) for user in users],
        })

    def test_grants_to_everyone_selected(self):
        response = self.choose(self.admin, [self.alice, self.bob])
        self.assertRedirects(response, reverse('admin:billing_bulk_grant'), fetch_redirect_response=False)
        page = self.admin.get(reverse('admin:billing_bulk_grant'))
        self.assertContains(page, 'alice@example.com')
        response = self.admin.post(reverse('admin:billing_bulk_grant'), {
            'plan': self.plan('plus').pk, 'days': '14', 'reason': 'Launch week',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Subscription.objects.filter(plan__code='plus', reason='Launch week').count(), 2)
        self.assertEqual(resolve(self.bob).plan.code, 'plus')
        # The selection is used once
        self.assertEqual(self.admin.get(reverse('admin:billing_bulk_grant')).status_code, 302)
        self.assertEqual(Subscription.objects.count(), 2)

    def test_nothing_selected_goes_back(self):
        response = self.admin.get(reverse('admin:billing_bulk_grant'))
        self.assertRedirects(response, reverse('admin:identity_appuser_changelist'))

    def test_support_is_held_to_thirty_days_here_too(self):
        support = self.signed_in(self.support)
        self.choose(support, [self.alice])
        response = support.post(reverse('admin:billing_bulk_grant'), {
            'plan': self.plan('plus').pk, 'days': '90', 'reason': 'x',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Subscription.objects.exists())

    def test_staff_without_a_role_do_not_get_the_action(self):
        nobody = self.signed_in(self.nobody)
        self.choose(nobody, [self.alice])
        self.assertEqual(nobody.get(reverse('admin:billing_bulk_grant')).status_code, 403)
        self.assertFalse(Subscription.objects.exists())


class OfferAdminTests(AdminTestCase):
    def generate(self, client, **data):
        return client.post(reverse('admin:billing_promocode_generate'), {
            'count': '5', 'note': 'Meetup', 'kind': PromoCode.BENEFIT_PLAN, 'plan': self.plan('plus').pk,
            'days': '30', 'uses_per_code': '1', **data,
        })

    def test_generate_codes_then_redeem_one(self):
        response = self.generate(self.admin, valid_days='10')
        self.assertEqual(response.status_code, 302)
        codes = list(PromoCode.objects.filter(note='Meetup'))
        self.assertEqual(len(codes), 5)
        self.assertEqual(len({code.code for code in codes}), 5)
        self.assertTrue(all(len(code.code) >= 10 and code.created_by == self.root for code in codes))
        response = self.alice_client.post('/api/v1/billing/promo/redeem/', {'code': codes[0].code}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(resolve(self.alice).plan.code, 'plus')

    def test_the_generate_page_names_its_button(self):
        response = self.admin.get(reverse('admin:billing_promocode_generate'))
        self.assertRegex(response.content.decode(), r'>\s*Generate\s*</button>')

    def test_a_plan_code_needs_a_plan_and_days(self):
        response = self.generate(self.admin, days='')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'A plan code needs a plan and a number of days.')
        self.assertFalse(PromoCode.objects.exists())

    def test_support_cannot_generate(self):
        self.assertEqual(self.generate(self.signed_in(self.support)).status_code, 403)
        self.assertFalse(PromoCode.objects.exists())

    def test_export_csv(self):
        self.generate(self.admin)
        ids = [str(pk) for pk in PromoCode.objects.values_list('pk', flat=True)]
        response = self.admin.post(reverse('admin:billing_promocode_changelist'), {
            'action': 'export_csv', '_selected_action': ids,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        lines = response.content.decode().strip().splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0], 'code,gives,uses,max_uses,ends_at,note')

    def test_campaign_reach_and_switch(self):
        now = timezone.now()
        campaign = Campaign.objects.create(
            name='Eid', slug='eid', title='Eid offer', starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=3), audience=Campaign.AUDIENCE_PLANS, is_active=False,
        )
        campaign.audience_plans.add(self.plan('free'))
        grant_plan(self.bob, 'plus')
        response = self.admin.get(reverse('admin:billing_campaign_change', args=[campaign.pk]))
        self.assertEqual(response.status_code, 200)
        free_users = User.objects.filter(is_active=True).count() - 1
        self.assertContains(response, f'{free_users} user(s) right now')

        self.admin.post(reverse('admin:billing_campaign_changelist'), {
            'action': 'switch_on', '_selected_action': [str(campaign.pk)],
        })
        campaign.refresh_from_db()
        self.assertTrue(campaign.is_active)

    def test_records_are_read_only(self):
        for model in ('promoredemption', 'campaignclaim', 'funnelevent', 'usagecounter', 'usageevent', 'billingevent'):
            self.assertEqual(self.admin.get(reverse(f'admin:billing_{model}_add')).status_code, 403, model)


class RoleTests(AdminTestCase):
    def test_groups_exist_with_their_permissions(self):
        self.assertTrue(self.manager.has_perm('billing.change_planfeature'))
        self.assertTrue(self.manager.has_perm('billing.add_campaign'))
        self.assertFalse(self.manager.has_perm('billing.change_usagecounter'))
        self.assertTrue(self.support.has_perm('billing.add_subscription'))
        self.assertTrue(self.support.has_perm('billing.view_plan'))
        self.assertTrue(self.support.has_perm('identity.view_appuser'))
        self.assertFalse(self.support.has_perm('billing.change_plan'))
        self.assertFalse(self.support.has_perm('billing.add_promocode'))
        self.assertFalse(self.support.has_perm('billing.change_billingsettings'))
        self.assertFalse(self.support.has_perm('identity.change_appuser'))

    def test_running_it_again_changes_nothing(self):
        group = Group.objects.get(name=roles.SUPPORT)
        before = set(group.permissions.values_list('codename', flat=True))
        roles.ensure_roles()
        self.assertEqual(set(group.permissions.values_list('codename', flat=True)), before)

    def test_support_cannot_change_settings_or_plans(self):
        support = self.signed_in(self.support)
        response = support.post(reverse('admin:billing_billingsettings_change', args=[1]), {
            'enforcement_mode': BillingSettings.MODE_OFF,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BillingSettings.load().enforcement_mode, BillingSettings.MODE_ENFORCE)
        free = self.plan('free')
        self.assertEqual(support.post(reverse('admin:billing_plan_change', args=[free.pk]), {}).status_code, 403)

    def test_subscriptions_are_never_deleted_from_the_admin(self):
        subscription = grant_plan(self.alice, 'plus')
        response = self.admin.post(reverse('admin:billing_subscription_delete', args=[subscription.pk]), {'post': 'yes'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Subscription.objects.filter(pk=subscription.pk).exists())

    def test_revoke_action_on_the_list(self):
        subscription = grant_plan(self.alice, 'plus')
        self.admin.post(reverse('admin:billing_subscription_changelist'), {
            'action': 'revoke_selected', '_selected_action': [str(subscription.pk)],
        })
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.STATUS_REVOKED)
