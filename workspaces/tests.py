from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from cashbook.models import CashBook, Entry
from .models import Membership, Workspace, WorkspaceOwnedModel
from .services import create_workspace, get_personal_workspace
from .tenancy import activate_tenant_context, bypass_tenant_context, clear_tenant_context

User = get_user_model()

# Apps whose models hold tenant data. Each model must carry the workspace key
# and its table must have row-level security, unless listed as exempt.
TENANT_APPS = ['cashbook', 'ledger_personal', 'reminders']
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
