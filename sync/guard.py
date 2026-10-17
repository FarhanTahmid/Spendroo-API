"""
Synced rows are never hard-deleted.

A phone learns that a row is gone only from its tombstone: deleted_at set by
an update, which gives the row a new server_seq for the next pull. A real
DELETE leaves nothing to pull, so every phone keeps the row for good and its
totals drift from the server's. The database refuses such a DELETE on every
synced table (a trigger, see CREATE_FUNCTION); code removes a row with
soft_delete(), or several with tombstone().

The one exception is taking away a whole workspace: phones drop everything
of a workspace that leaves their list (refreshWorkspaces in the app's sync
engine), so nothing is left behind. That runs inside allow_hard_delete().
"""
from contextlib import contextmanager

from django.db import connection, migrations, transaction
from django.db.models import F
from django.utils import timezone

SETTING = 'spendroo.allow_hard_delete'
TRIGGER = 'sync_no_hard_delete'

CREATE_FUNCTION = f"""
CREATE FUNCTION sync_refuse_hard_delete() RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('{SETTING}', true), '') = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'Rows of % are never hard-deleted: phones learn of a deletion only from a tombstone. '
        'Use soft_delete(), or sync.guard.allow_hard_delete() to remove a whole workspace.', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END
$$ LANGUAGE plpgsql;
"""
DROP_FUNCTION = 'DROP FUNCTION sync_refuse_hard_delete();'


def guard_deletes(table):
    """Migration operations refusing a hard DELETE on a synced table. Every table in sync/registry.py needs them."""
    return [
        migrations.RunSQL(
            sql=f'CREATE TRIGGER {TRIGGER} BEFORE DELETE ON "{table}" '
                f'FOR EACH ROW EXECUTE FUNCTION sync_refuse_hard_delete()',
            reverse_sql=f'DROP TRIGGER {TRIGGER} ON "{table}"',
        )
    ]


@contextmanager
def allow_hard_delete():
    """
    Lets the block hard-delete synced rows. Only for removing a whole
    workspace: anything less must leave tombstones for the phones.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SELECT set_config(%s, %s, true)', [SETTING, 'on'])
        try:
            yield
        finally:
            # Local to the transaction, but an outer one may carry on after this block
            with connection.cursor() as cursor:
                cursor.execute('SELECT set_config(%s, %s, true)', [SETTING, 'off'])


def tombstone(queryset):
    """Soft-delete every live row of the queryset at once. Each gets a new server_seq, so phones pull the deletion."""
    return queryset.filter(deleted_at__isnull=True).update(
        deleted_at=timezone.now(), version=F('version') + 1, updated_at=timezone.now(),
    )
