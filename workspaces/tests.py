from allauth.account.models import EmailAddress
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from audit.models import CRUDLog
from billing.catalog.keys import F
from billing.models import BillingSettings
from billing.testing import grant_plan, set_limit, set_mode
from billing.tests.base import BillingTestCase, client_for

from cashbook.models import CashBook, CashBookAdditionalMember, Entry
from . import invitations
from .models import Invitation, Membership, Workspace, WorkspaceOwnedModel
from .services import create_workspace, get_personal_workspace
from .tenancy import activate_tenant_context, bypass_tenant_context, clear_tenant_context

User = get_user_model()

WORKSPACES = '/api/v1/workspaces/'
ACCEPT = f'{WORKSPACES}invitations/accept/'
PREVIEW = f'{WORKSPACES}invitations/preview/'
PULL = '/api/v1/sync/pull/'

# Apps whose models hold tenant data. Each model must carry the workspace key
# and its table must have row-level security, unless listed as exempt.
TENANT_APPS = ['cashbook', 'ledger_personal', 'reminders', 'tags']
EXEMPT_MODELS = set()


def tenant_models():
    for app_label in TENANT_APPS:
        for model in apps.get_app_config(app_label).get_models():
            if model._meta.label not in EXEMPT_MODELS:
                yield model


class TenantKeyTests(TestCase):
    def test_every_tenant_model_has_workspace_key(self):
        for model in tenant_models():
            field_names = {field.name for field in model._meta.get_fields()}
            self.assertIn(
                'workspace', field_names,
                f"{model._meta.label} has no workspace key. Inherit WorkspaceOwnedModel or add it to EXEMPT_MODELS.",
            )

    def test_every_tenant_table_has_forced_row_level_security(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity")
            protected = {row[0] for row in cursor.fetchall()}
        for model in tenant_models():
            self.assertIn(
                model._meta.db_table, protected,
                f"{model._meta.db_table} has no row-level security. Add it with workspaces.rls.enable_rls in a migration.",
            )


    def test_every_synced_table_has_the_cursor_trigger(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE t.tgname = 'sync_server_seq'"
            )
            with_trigger = {row[0] for row in cursor.fetchall()}
        for model in tenant_models():
            if issubclass(model, WorkspaceOwnedModel):
                self.assertIn(
                    model._meta.db_table, with_trigger,
                    f"{model._meta.db_table} has no sync cursor. Add it with sync.sql.enable_sync in a migration.",
                )


class UnprivilegedRoleMixin:
    """
    PostgreSQL superusers skip row-level security. On a superuser connection
    (local development, CI) this switches the test to a plain role, so the
    policies apply as they do in production.
    """

    def use_unprivileged_role(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            self.switched_role = cursor.fetchone()[0]
            if self.switched_role:
                # Rolled back with the test transaction
                cursor.execute("CREATE ROLE spendroo_rls_probe NOLOGIN")
                cursor.execute("GRANT USAGE ON SCHEMA public TO spendroo_rls_probe")
                cursor.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO spendroo_rls_probe")
                cursor.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO spendroo_rls_probe")
                cursor.execute("SET LOCAL ROLE spendroo_rls_probe")
        self.addCleanup(self.restore_role)

    def restore_role(self):
        with connection.cursor() as cursor:
            if self.switched_role:
                cursor.execute("RESET ROLE")
        bypass_tenant_context()


class RowLevelSecurityTests(UnprivilegedRoleMixin, TestCase):
    """The database itself refuses cross-tenant reads and writes."""

    def setUp(self):
        self.alice = User.objects.create_user(email='alice@example.com', password='pass-12345')
        self.bob = User.objects.create_user(email='bob@example.com', password='pass-12345')
        self.alice_book = CashBook.objects.create(workspace=get_personal_workspace(self.alice), book_name='Alice')
        self.bob_book = CashBook.objects.create(workspace=get_personal_workspace(self.bob), book_name='Bob')
        Entry.objects.create(cashbook=self.bob_book, amount_minor=100, entry_date='2026-10-01')
        self.use_unprivileged_role()

    def test_no_context_shows_nothing(self):
        clear_tenant_context()
        self.assertEqual(CashBook.all_objects.count(), 0)
        self.assertEqual(Entry.all_objects.count(), 0)

    def test_context_shows_only_own_workspaces(self):
        activate_tenant_context(self.alice)
        self.assertEqual(list(CashBook.all_objects.all()), [self.alice_book])
        self.assertEqual(Entry.all_objects.count(), 0)
        # History tables are covered as well
        self.assertEqual(CashBook.pgh_event_model.objects.count(), 1)

    def test_cannot_write_into_foreign_workspace(self):
        activate_tenant_context(self.alice)
        # Bob's rows are invisible, so updates and deletes match nothing
        self.assertEqual(CashBook.all_objects.filter(id=self.bob_book.id).update(book_name='Hijacked'), 0)
        with connection.cursor() as cursor:
            cursor.execute("SAVEPOINT before_insert")
            with self.assertRaisesMessage(Exception, 'row-level security'):
                cursor.execute(
                    "INSERT INTO cashbook_cashbook (id, version, server_seq, created_at, updated_at, workspace_id, book_name, currency) "
                    "VALUES (gen_random_uuid(), 1, 0, now(), now(), %s, 'Planted', 'BDT')",
                    [self.bob_book.workspace_id],
                )
            cursor.execute("ROLLBACK TO SAVEPOINT before_insert")

    def test_bypass_shows_everything(self):
        bypass_tenant_context()
        self.assertEqual(CashBook.all_objects.count(), 2)


class PersonalWorkspaceTests(TestCase):
    def test_new_user_gets_personal_workspace(self):
        user = User.objects.create_user(email='a@example.com', password='pass-12345')
        workspace = Workspace.objects.get(owner=user)
        self.assertEqual(workspace.kind, Workspace.KIND_PERSONAL)
        self.assertEqual(workspace.role_of(user), Membership.ROLE_OWNER)

    def test_personal_workspace_is_not_duplicated(self):
        user = User.objects.create_user(email='a@example.com', password='pass-12345')
        self.assertEqual(get_personal_workspace(user), get_personal_workspace(user))
        self.assertEqual(Workspace.objects.filter(owner=user).count(), 1)


class SyncColumnTests(TestCase):
    def test_version_increments_and_soft_delete_hides_row(self):
        user = User.objects.create_user(email='a@example.com', password='pass-12345')
        workspace = create_workspace(owner=user, name='Shop')
        self.assertEqual(workspace.version, 1)

        workspace.name = 'Shop 2'
        workspace.save()
        self.assertEqual(workspace.version, 2)

        workspace.soft_delete()
        self.assertFalse(Workspace.objects.filter(id=workspace.id).exists())
        self.assertTrue(Workspace.all_objects.filter(id=workspace.id).exists())
        self.assertEqual(Workspace.all_objects.get(id=workspace.id).version, 3)


class WorkspaceAPITests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(email='alice@example.com', password='pass-12345')
        self.bob = User.objects.create_user(email='bob@example.com', password='pass-12345')
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def test_list_shows_only_own_workspaces(self):
        create_workspace(owner=self.bob, name='Bob Shop')
        response = self.client.get('/api/v1/workspaces/')
        self.assertEqual(response.status_code, 200)
        names = [w['name'] for w in response.data['results']]
        self.assertEqual(names, ['Personal'])

    def test_create_makes_business_workspace(self):
        response = self.client.post('/api/v1/workspaces/', {'name': 'Alice Shop', 'default_currency': 'usd'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['kind'], 'business')
        self.assertEqual(response.data['default_currency'], 'USD')
        self.assertEqual(response.data['role'], 'owner')

    def test_cannot_read_or_edit_foreign_workspace(self):
        workspace = create_workspace(owner=self.bob, name='Bob Shop')
        self.assertEqual(self.client.get(f'/api/v1/workspaces/{workspace.id}/').status_code, 404)
        self.assertEqual(self.client.patch(f'/api/v1/workspaces/{workspace.id}/', {'name': 'x'}).status_code, 404)
        self.assertEqual(self.client.get(f'/api/v1/workspaces/{workspace.id}/members/').status_code, 404)

    def test_staff_cannot_edit_workspace(self):
        workspace = create_workspace(owner=self.bob, name='Bob Shop')
        Membership.objects.create(workspace=workspace, user=self.alice, role=Membership.ROLE_STAFF)
        response = self.client.patch(f'/api/v1/workspaces/{workspace.id}/', {'name': 'Hijacked'})
        self.assertEqual(response.status_code, 403)

    def test_owner_can_change_default_currency(self):
        workspace = create_workspace(owner=self.alice, name='Alice Shop')
        response = self.client.patch(f'/api/v1/workspaces/{workspace.id}/', {'default_currency': 'usd'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['default_currency'], 'USD')
        self.assertEqual(self.client.patch(f'/api/v1/workspaces/{workspace.id}/', {'default_currency': 'dollars'}).status_code, 400)

        staffed = create_workspace(owner=self.bob, name='Bob Shop')
        Membership.objects.create(workspace=staffed, user=self.alice, role=Membership.ROLE_STAFF)
        response = self.client.patch(f'/api/v1/workspaces/{staffed.id}/', {'default_currency': 'USD'})
        self.assertEqual(response.status_code, 403)

    def test_create_with_client_id_is_idempotent(self):
        workspace_id = '0192f3a1-7b2c-7d4e-8f00-123456789abc'
        first = self.client.post('/api/v1/workspaces/', {'id': workspace_id, 'name': 'Shop'})
        again = self.client.post('/api/v1/workspaces/', {'id': workspace_id, 'name': 'Shop'})
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.data['id'], again.data['id'])
        self.assertEqual(Workspace.objects.filter(owner=self.alice, kind='business').count(), 1)

        bob = APIClient()
        bob.force_authenticate(self.bob)
        self.assertEqual(bob.post('/api/v1/workspaces/', {'id': workspace_id, 'name': 'Mine'}).status_code, 400)
        self.assertEqual(self.client.post('/api/v1/workspaces/', {'id': 'nope', 'name': 'Shop'}).status_code, 400)


class IdentifierTests(TestCase):
    def test_new_rows_get_time_ordered_uuid7(self):
        user = User.objects.create_user(email='alice@example.com', password='pass-12345')
        first = create_workspace(owner=user, name='One')
        second = create_workspace(owner=user, name='Two')
        self.assertEqual((first.id.version, second.id.version), (7, 7))
        # The leading 48 bits are the creation time in milliseconds
        self.assertLessEqual(first.id.int >> 80, second.id.int >> 80)


class InvitationTests(BillingTestCase):
    """Alice owns Shop on the Business plan (five seats); Bob and Carol are invited."""

    def setUp(self):
        super().setUp()
        grant_plan(self.alice, 'business')
        self.carol = User.objects.create_user(email='carol@example.com', password='pass-12345')
        self.carol_client = client_for(self.carol)
        for user in (self.bob, self.carol):
            EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
        self.url = f'{WORKSPACES}{self.shop.id}/invitations/'

    def invite(self, client=None, **data):
        return (client or self.alice_client).post(self.url, data, format='json')

    def accept(self, client, code):
        return client.post(ACCEPT, {'code': code}, format='json')

    def test_invite_by_email_sends_the_code_and_keeps_only_its_hash(self):
        response = self.invite(email='Bob@Example.com', role='viewer')
        self.assertEqual(response.status_code, 201, response.data)
        code = response.data['code']
        self.assertEqual((response.data['email'], response.data['role'], response.data['emailed']), ('bob@example.com', 'viewer', True))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['bob@example.com'])
        self.assertIn(code, mail.outbox[0].body)
        self.assertIn('Shop', mail.outbox[0].subject)
        stored = Invitation.objects.get()
        self.assertEqual(stored.code_hash, invitations.hash_code(code))
        self.assertNotIn(code, str(Invitation.objects.values().get()))

    def test_invite_by_phone_returns_the_code_and_sends_nothing(self):
        response = self.invite(phone='+880 1712-345678')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data['phone'], response.data['role'], response.data['emailed']), ('+8801712345678', 'staff', False))
        self.assertEqual(len(mail.outbox), 0)

    def test_needs_an_email_or_a_phone_but_not_both(self):
        self.assertEqual(self.invite().status_code, 400)
        self.assertEqual(self.invite(email='bob@example.com', phone='+8801712345678').status_code, 400)
        self.assertEqual(self.invite(phone='call me').status_code, 400)
        self.assertEqual(self.invite(email='bob@example.com', role='owner').status_code, 400)

    def test_owner_and_admins_invite_only_the_owner_invites_an_admin(self):
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_ADMIN)
        Membership.objects.create(workspace=self.shop, user=self.carol, role=Membership.ROLE_STAFF)
        self.assertEqual(self.invite(self.bob_client, phone='+8801711111111').status_code, 201)
        self.assertEqual(self.invite(self.bob_client, phone='+8801722222222', role='admin').status_code, 403)
        self.assertEqual(self.invite(self.carol_client, phone='+8801733333333').status_code, 403)
        self.assertEqual(self.carol_client.get(self.url).status_code, 403)
        self.assertEqual(self.invite(phone='+8801722222222', role='admin').status_code, 201)

    def test_only_a_real_business_of_ones_own_takes_invitations(self):
        personal = f'{WORKSPACES}{self.personal.id}/invitations/'
        response = self.alice_client.post(personal, {'phone': '+8801712345678'}, format='json')
        self.assertEqual((response.status_code, response.data['code']), (400, 'not_a_business'))
        demo = self.alice_client.post(f'{WORKSPACES}demo/').data['id']
        self.assertEqual(self.alice_client.post(f'{WORKSPACES}{demo}/invitations/', {'phone': '+8801712345678'}, format='json').status_code, 400)
        self.assertEqual(self.invite(self.bob_client, phone='+8801712345678').status_code, 404)

    def test_someone_already_in_the_business_is_not_invited(self):
        Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_STAFF)
        response = self.invite(email='BOB@example.com')
        self.assertEqual((response.status_code, response.data['code']), (400, 'already_member'))

    def test_accepting_an_email_invitation_makes_a_member_with_the_role(self):
        code = self.invite(email='bob@example.com', role='viewer').data['code']
        preview = self.bob_client.get(PREVIEW, {'code': code.lower()})
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual((preview.data['workspace_name'], preview.data['role'], preview.data['invited_by']), ('Shop', 'viewer', 'alice@example.com'))

        response = self.accept(self.bob_client, f'{code[:5]}-{code[5:]}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['id'], response.data['role']), (str(self.shop.id), 'viewer'))
        membership = Membership.objects.get(workspace=self.shop, user=self.bob)
        self.assertEqual((membership.role, membership.added_by), ('viewer', self.alice))
        invitation = Invitation.objects.get()
        self.assertEqual((invitation.status, invitation.accepted_by), ('accepted', self.bob))
        names = [w['name'] for w in self.bob_client.get(WORKSPACES).data['results']]
        self.assertIn('Shop', names)

    def test_an_email_invitation_is_only_for_that_verified_address(self):
        code = self.invite(email='bob@example.com').data['code']
        response = self.accept(self.carol_client, code)
        self.assertEqual((response.status_code, response.data['code']), (400, 'wrong_account'))
        EmailAddress.objects.filter(user=self.bob).update(verified=False)
        self.assertEqual(self.accept(self.bob_client, code).data['code'], 'wrong_account')
        self.assertFalse(Membership.objects.filter(workspace=self.shop).exclude(user=self.alice).exists())

    def test_a_phone_invitation_is_for_whoever_has_the_code(self):
        code = self.invite(phone='+8801712345678').data['code']
        self.assertEqual(self.accept(self.carol_client, code).status_code, 200)
        self.assertTrue(Membership.objects.filter(workspace=self.shop, user=self.carol, role='staff').exists())

    def test_a_code_works_once_and_not_after_it_expires_or_is_cancelled(self):
        code = self.invite(phone='+8801712345678').data['code']
        self.assertEqual(self.accept(self.carol_client, code).status_code, 200)
        self.assertEqual(self.accept(self.bob_client, code).data['code'], 'used')
        self.assertEqual(self.bob_client.get(PREVIEW, {'code': code}).data['code'], 'used')

        expired = self.invite(phone='+8801722222222').data['code']
        Invitation.objects.filter(phone='+8801722222222').update(expires_at=timezone.now())
        self.assertEqual(self.accept(self.bob_client, expired).data['code'], 'expired')

        cancelled = self.invite(phone='+8801733333333').data
        self.assertEqual(self.alice_client.delete(f'{self.url}{cancelled["id"]}/').status_code, 204)
        self.assertEqual(self.accept(self.bob_client, cancelled['code']).data['code'], 'used')
        self.assertEqual(self.alice_client.delete(f'{self.url}{cancelled["id"]}/').status_code, 400)

        self.assertEqual(self.accept(self.bob_client, 'ABCDEFGHJK').data['code'], 'invalid')
        self.assertEqual(self.accept(self.bob_client, '').status_code, 400)

    def test_cancelling_needs_owner_or_admin_of_that_business(self):
        invitation = self.invite(phone='+8801712345678').data
        Membership.objects.create(workspace=self.shop, user=self.carol, role=Membership.ROLE_STAFF)
        self.assertEqual(self.carol_client.delete(f'{self.url}{invitation["id"]}/').status_code, 403)
        self.assertEqual(self.bob_client.delete(f'{self.url}{invitation["id"]}/').status_code, 404)
        self.assertEqual(self.alice_client.delete(f'{self.url}not-an-id/').status_code, 400)

    def test_inviting_again_replaces_the_earlier_code(self):
        first = self.invite(email='bob@example.com').data['code']
        second = self.invite(email='bob@example.com', role='viewer').data['code']
        self.assertEqual(self.accept(self.bob_client, first).data['code'], 'used')
        self.assertEqual(self.accept(self.bob_client, second).data['role'], 'viewer')
        listed = self.alice_client.get(self.url)
        self.assertEqual((listed.status_code, listed.data), (200, []))

    def test_a_member_cannot_accept_again(self):
        Membership.objects.create(workspace=self.shop, user=self.carol, role=Membership.ROLE_STAFF)
        code = self.invite(phone='+8801712345678').data['code']
        self.assertEqual(self.accept(self.carol_client, code).data['code'], 'already_member')

    def test_the_list_shows_invitations_still_waiting(self):
        self.invite(email='bob@example.com')
        self.invite(phone='+8801712345678')
        Invitation.objects.filter(phone='+8801712345678').update(expires_at=timezone.now())
        listed = self.alice_client.get(self.url).data
        self.assertEqual([(row['email'], row['status']) for row in listed], [('bob@example.com', 'pending')])
        self.assertNotIn('code', listed[0])

    def test_free_has_no_seats_to_invite_to(self):
        stall = create_workspace(owner=self.bob, name='Stall')
        response = self.bob_client.post(f'{WORKSPACES}{stall.id}/invitations/', {'email': 'carol@example.com'}, format='json')
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual((response.data['feature'], response.data['upgrade_to']), ('team.seats', 'business'))
        self.assertFalse(Invitation.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_invitations_waiting_hold_seats(self):
        set_limit(self.alice, F.TEAM_SEATS, 2)
        Membership.objects.create(workspace=self.shop, user=self.carol, role=Membership.ROLE_STAFF)
        self.assertEqual(self.invite(email='bob@example.com').status_code, 201)
        self.assertEqual(self.invite(phone='+8801712345678').status_code, 402)
        # Inviting the same person again takes no second seat
        self.assertEqual(self.invite(email='bob@example.com').status_code, 201)

    def test_accepting_is_refused_when_the_seats_were_taken_since(self):
        code = self.invite(email='bob@example.com').data['code']
        set_limit(self.alice, F.TEAM_SEATS, 1)
        Membership.objects.create(workspace=self.shop, user=self.carol, role=Membership.ROLE_STAFF)
        response = self.accept(self.bob_client, code)
        self.assertEqual((response.status_code, response.data['feature']), (402, 'team.seats'))
        self.assertFalse(Membership.objects.filter(workspace=self.shop, user=self.bob).exists())
        self.assertEqual(Invitation.objects.get().status, 'pending')

    def test_codes_cannot_be_guessed_quickly(self):
        cache.clear()
        statuses = [self.accept(self.bob_client, 'ABCDEFGHJK').status_code for _ in range(21)]
        self.assertEqual((statuses[0], statuses[-1]), (400, 429))
        cache.clear()


class TeamTestCase(BillingTestCase):
    """Alice owns Shop on the Business plan; Bob is an admin, Carol staff and Dan a viewer."""

    def setUp(self):
        super().setUp()
        grant_plan(self.alice, 'business')
        self.carol = User.objects.create_user(email='carol@example.com', password='pass-12345')
        self.dan = User.objects.create_user(email='dan@example.com', password='pass-12345')
        self.carol_client = client_for(self.carol)
        self.dan_client = client_for(self.dan)
        self.owner = self.shop.memberships.get(user=self.alice)
        self.admin = Membership.objects.create(workspace=self.shop, user=self.bob, role=Membership.ROLE_ADMIN)
        self.staff = Membership.objects.create(workspace=self.shop, user=self.carol, role=Membership.ROLE_STAFF)
        self.viewer = Membership.objects.create(workspace=self.shop, user=self.dan, role=Membership.ROLE_VIEWER)

    def member_url(self, membership, workspace=None):
        return f'{WORKSPACES}{(workspace or self.shop).id}/members/{membership.id}/'

    def set_role(self, client, membership, role):
        return client.patch(self.member_url(membership), {'role': role}, format='json')

    def remove(self, client, membership):
        return client.delete(self.member_url(membership))

    def role_of(self, membership):
        return Membership.all_objects.get(pk=membership.pk).role

    def audit(self, operation):
        return CRUDLog.objects.filter(operation=operation)


class MemberRoleTests(TeamTestCase):
    def test_owner_changes_any_role_but_their_own(self):
        response = self.set_role(self.alice_client, self.staff, 'viewer')
        self.assertEqual((response.status_code, response.data['role']), (200, 'viewer'))
        self.assertEqual(self.set_role(self.alice_client, self.admin, 'staff').status_code, 200)
        self.assertEqual(self.set_role(self.alice_client, self.viewer, 'admin').status_code, 200)
        self.assertEqual((self.role_of(self.staff), self.role_of(self.admin), self.role_of(self.viewer)), ('viewer', 'staff', 'admin'))
        self.assertEqual(self.set_role(self.alice_client, self.owner, 'admin').data['code'], 'owner_role')
        self.assertEqual(self.role_of(self.owner), 'owner')

    def test_admins_move_staff_and_viewers_between_staff_and_viewer_only(self):
        self.assertEqual(self.set_role(self.bob_client, self.staff, 'viewer').status_code, 200)
        self.assertEqual(self.set_role(self.bob_client, self.viewer, 'staff').status_code, 200)
        self.assertEqual(self.set_role(self.bob_client, self.staff, 'admin').status_code, 403)
        other_admin = Membership.objects.create(
            workspace=self.shop, user=User.objects.create_user(email='erin@example.com', password='pass-12345'),
            role=Membership.ROLE_ADMIN,
        )
        self.assertEqual(self.set_role(self.bob_client, other_admin, 'staff').status_code, 403)
        self.assertEqual(self.set_role(self.bob_client, self.admin, 'staff').status_code, 403)
        self.assertEqual(self.set_role(self.bob_client, self.owner, 'staff').status_code, 400)
        self.assertEqual((self.role_of(self.staff), self.role_of(self.viewer), self.role_of(other_admin)), ('viewer', 'staff', 'admin'))

    def test_staff_and_viewers_change_nothing(self):
        self.assertEqual(self.set_role(self.carol_client, self.viewer, 'staff').status_code, 403)
        self.assertEqual(self.set_role(self.dan_client, self.staff, 'viewer').status_code, 403)
        self.assertEqual(self.set_role(self.dan_client, self.viewer, 'admin').status_code, 403)
        self.assertEqual(self.role_of(self.viewer), 'viewer')

    def test_nobody_is_made_owner_by_a_role_change(self):
        response = self.set_role(self.alice_client, self.admin, 'owner')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.role_of(self.admin), 'admin')
        self.assertEqual(self.set_role(self.alice_client, self.admin, 'boss').status_code, 400)

    def test_outsiders_and_unknown_members_get_not_found(self):
        stranger = client_for(User.objects.create_user(email='zed@example.com', password='pass-12345'))
        self.assertEqual(self.set_role(stranger, self.staff, 'viewer').status_code, 404)
        self.assertEqual(self.alice_client.patch(f'{WORKSPACES}{self.shop.id}/members/nope/', {'role': 'staff'}).status_code, 400)
        elsewhere = create_workspace(owner=self.alice, name='Stall')
        url = self.member_url(self.staff, elsewhere)
        self.assertEqual(self.alice_client.patch(url, {'role': 'staff'}, format='json').status_code, 404)

    def test_personal_and_demo_spaces_have_no_team_to_manage(self):
        personal = self.personal.memberships.get(user=self.alice)
        response = self.alice_client.patch(self.member_url(personal, self.personal), {'role': 'staff'}, format='json')
        self.assertEqual((response.status_code, response.data['code']), (400, 'not_a_business'))
        response = self.alice_client.delete(self.member_url(personal, self.personal))
        self.assertEqual((response.status_code, response.data['code']), (400, 'not_a_business'))

    def test_a_change_is_audited_and_the_same_role_is_not(self):
        self.set_role(self.alice_client, self.staff, 'viewer')
        log = self.audit('change_member_role').get()
        self.assertEqual((log.user, log.action, log.model_name, log.record_ids), (self.alice, 'UPDATE', 'Membership', str(self.staff.id)))
        self.assertEqual(log.changes['role'], {'old': 'staff', 'new': 'viewer'})
        self.set_role(self.alice_client, self.staff, 'viewer')
        self.assertEqual(self.audit('change_member_role').count(), 1)

    def test_a_viewer_made_staff_sees_only_the_books_granted(self):
        till = CashBook.objects.create(workspace=self.shop, book_name='Till')
        CashBook.objects.create(workspace=self.shop, book_name='Safe')
        pull = lambda: self.dan_client.get(PULL, {'workspace': str(self.shop.id)}).data['accessible_cashbook_ids']
        self.assertEqual(len(pull()), 2)
        self.set_role(self.alice_client, self.viewer, 'staff')
        self.assertEqual(pull(), [])
        CashBookAdditionalMember.objects.create(cashbook=till, member=self.dan, role='viewer')
        self.assertEqual(pull(), [till.id])


class MemberRemovalTests(TeamTestCase):
    def test_a_removed_member_loses_the_business_at_once_but_stays_signed_in(self):
        till = CashBook.objects.create(workspace=self.shop, book_name='Till')
        grant = CashBookAdditionalMember.objects.create(cashbook=till, member=self.carol, role='editor')
        self.assertEqual(self.remove(self.alice_client, self.staff).status_code, 204)

        removed = Membership.all_objects.get(pk=self.staff.pk)
        self.assertIsNotNone(removed.deleted_at)
        self.assertIsNotNone(CashBookAdditionalMember.all_objects.get(pk=grant.pk).deleted_at)
        listed = [row['id'] for row in self.carol_client.get(WORKSPACES).data['results']]
        self.assertNotIn(str(self.shop.id), listed)
        self.assertIn(str(get_personal_workspace(self.carol).id), listed)
        # Not 403: the app takes a 403 from sync for a lost session and signs out
        self.assertEqual(self.carol_client.get(PULL, {'workspace': str(self.shop.id)}).status_code, 404)
        self.assertEqual(self.carol_client.get(f'{WORKSPACES}{self.shop.id}/').status_code, 404)
        self.assertEqual(self.carol_client.get(PULL, {'workspace': str(get_personal_workspace(self.carol).id)}).status_code, 200)
        self.assertNotIn(self.carol.id, [row['user'] for row in self.alice_client.get(f'{WORKSPACES}{self.shop.id}/members/').data])

    def test_admins_remove_staff_and_viewers_only(self):
        other_admin = Membership.objects.create(
            workspace=self.shop, user=User.objects.create_user(email='erin@example.com', password='pass-12345'),
            role=Membership.ROLE_ADMIN,
        )
        self.assertEqual(self.remove(self.bob_client, other_admin).status_code, 403)
        self.assertEqual(self.remove(self.carol_client, self.viewer).status_code, 403)
        self.assertEqual(self.remove(self.bob_client, self.viewer).status_code, 204)
        self.assertEqual(self.remove(self.alice_client, other_admin).status_code, 204)

    def test_the_owner_cannot_be_removed(self):
        for client in (self.alice_client, self.bob_client):
            response = self.remove(client, self.owner)
            self.assertEqual((response.status_code, response.data['code']), (400, 'owner'))
        self.assertIsNone(Membership.all_objects.get(pk=self.owner.pk).deleted_at)

    def test_members_can_leave(self):
        for client, membership in ((self.dan_client, self.viewer), (self.bob_client, self.admin)):
            self.assertEqual(self.remove(client, membership).status_code, 204)
        self.assertEqual(self.audit('leave_business').count(), 2)
        self.assertEqual(self.audit('remove_member').count(), 0)

    def test_removal_is_audited(self):
        self.remove(self.alice_client, self.staff)
        log = self.audit('remove_member').get()
        self.assertEqual((log.user, log.action, log.model_name, log.record_ids), (self.alice, 'DELETE', 'Membership', str(self.staff.id)))
        self.assertEqual((log.changes['user'], log.changes['role']), ('carol@example.com', 'staff'))

    def test_removing_frees_the_seat_and_they_can_be_invited_back(self):
        set_limit(self.alice, F.TEAM_SEATS, 3)
        EmailAddress.objects.create(user=self.carol, email=self.carol.email, verified=True, primary=True)
        invite = lambda: self.alice_client.post(f'{WORKSPACES}{self.shop.id}/invitations/', {'email': 'carol@example.com'}, format='json')
        self.assertEqual(invite().status_code, 400)  # already in
        self.remove(self.alice_client, self.staff)
        response = invite()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.carol_client.post(ACCEPT, {'code': response.data['code']}, format='json').status_code, 200)
        self.assertEqual(self.shop.memberships.get(user=self.carol).role, 'staff')


class OwnershipTransferTests(TeamTestCase):
    def transfer(self, client, membership):
        return client.post(f'{WORKSPACES}{self.shop.id}/transfer-ownership/', {'membership_id': str(membership.id)}, format='json')

    def test_the_owner_hands_the_business_to_an_admin_and_stays_as_admin(self):
        grant_plan(self.bob, 'business')
        response = self.transfer(self.alice_client, self.admin)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['owner'], response.data['role']), (self.bob.id, 'admin'))
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.owner, self.bob)
        self.assertEqual((self.role_of(self.admin), self.role_of(self.owner)), ('owner', 'admin'))
        # The old owner is now an admin like any other
        self.assertEqual(self.transfer(self.alice_client, self.owner).status_code, 403)
        self.assertEqual(self.alice_client.delete(f'{WORKSPACES}{self.shop.id}/').status_code, 403)
        self.assertEqual(self.remove(self.bob_client, self.owner).status_code, 204)
        log = self.audit('transfer_ownership').get()
        self.assertEqual(log.changes['owner'], {'old': 'alice@example.com', 'new': 'bob@example.com'})

    def test_only_to_an_admin_and_only_by_the_owner(self):
        grant_plan(self.carol, 'business')
        response = self.transfer(self.alice_client, self.staff)
        self.assertEqual((response.status_code, response.data['code']), (400, 'not_admin'))
        self.assertEqual(self.transfer(self.bob_client, self.admin).status_code, 403)
        self.assertEqual(self.transfer(self.carol_client, self.admin).status_code, 403)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.owner, self.alice)

    def test_refused_when_the_new_owners_plan_has_no_room(self):
        # Free has room for one business, and Bob has his own already
        create_workspace(owner=self.bob, name='Stall')
        response = self.transfer(self.alice_client, self.admin)
        self.assertEqual((response.status_code, response.data['feature']), (402, 'business.workspaces'))
        # Free has no team seats at all
        set_limit(self.bob, F.BUSINESS_WORKSPACES, 5)
        response = self.transfer(self.alice_client, self.admin)
        self.assertEqual((response.status_code, response.data['feature']), (402, 'team.seats'))
        grant_plan(self.bob, 'business')
        set_limit(self.bob, F.TEAM_SEATS, 2)  # Alice, Carol and Dan would need three
        response = self.transfer(self.alice_client, self.admin)
        self.assertEqual((response.status_code, response.data['feature']), (402, 'team.seats'))
        set_limit(self.bob, F.TEAM_SEATS, 3)
        set_limit(self.bob, F.BUSINESS_CASHBOOKS, 1)
        CashBook.objects.create(workspace=self.shop, book_name='Till')
        CashBook.objects.create(workspace=self.shop, book_name='Safe')
        response = self.transfer(self.alice_client, self.admin)
        self.assertEqual((response.status_code, response.data['feature']), (402, 'business.cashbooks'))
        # Nothing changed on the way
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.owner, self.role_of(self.owner), self.role_of(self.admin)), (self.alice, 'owner', 'admin'))
        self.assertFalse(self.audit('transfer_ownership').exists())


class SeatTests(TeamTestCase):
    """Bob, Carol and Dan take three of the five seats on Alice's Business plan."""

    def seats(self, client, workspace=None):
        return client.get(f'{WORKSPACES}{(workspace or self.shop).id}/seats/')

    def test_counts_members_but_the_owner_and_invitations_waiting(self):
        invitations.invite(self.shop, self.alice, 'staff', phone='+8801712345678')
        response = self.seats(self.alice_client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            {'limit': 5, 'unlimited': False, 'used': 4, 'members': 3, 'pending': 1, 'enforced': True, 'upgrade_to': None},
        )

    def test_every_member_sees_the_owners_plan_not_their_own(self):
        # Bob, Carol and Dan are on Free, which has no seats
        for client in (self.bob_client, self.carol_client, self.dan_client):
            response = self.seats(client)
            self.assertEqual((response.status_code, response.data['limit'], response.data['used']), (200, 5, 3))

    def test_a_free_owner_has_none_and_is_pointed_at_business(self):
        eve = User.objects.create_user(email='eve@example.com', password='pass-12345')
        stall = create_workspace(owner=eve, name='Stall')
        response = self.seats(client_for(eve), stall)
        self.assertEqual((response.data['limit'], response.data['used'], response.data['upgrade_to']), (0, 0, 'business'))

    def test_unlimited_and_not_enforced_are_said(self):
        set_limit(self.alice, F.TEAM_SEATS, None)
        response = self.seats(self.alice_client)
        self.assertEqual((response.data['limit'], response.data['unlimited']), (None, True))
        set_mode(BillingSettings.MODE_LOG_ONLY)
        self.assertFalse(self.seats(self.alice_client).data['enforced'])

    def test_only_members_of_a_real_business(self):
        eve = User.objects.create_user(email='eve@example.com', password='pass-12345')
        self.assertEqual(self.seats(client_for(eve)).status_code, 404)
        response = self.seats(self.alice_client, self.personal)
        self.assertEqual((response.status_code, response.data['code']), (400, 'not_a_business'))


class MemberListTests(TeamTestCase):
    def test_every_member_sees_everyone_with_their_name(self):
        User.objects.filter(pk=self.carol.pk).update(first_name='Carol', last_name='Khan')
        response = self.dan_client.get(f'{WORKSPACES}{self.shop.id}/members/')
        self.assertEqual(response.status_code, 200)
        rows = {row['user_email']: row for row in response.data}
        self.assertEqual(set(rows), {'alice@example.com', 'bob@example.com', 'carol@example.com', 'dan@example.com'})
        self.assertEqual((rows['carol@example.com']['user_name'], rows['carol@example.com']['role']), ('Carol Khan', 'staff'))
        self.assertEqual((rows['alice@example.com']['user_name'], rows['alice@example.com']['role']), ('', 'owner'))
