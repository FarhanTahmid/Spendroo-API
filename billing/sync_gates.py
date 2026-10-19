"""
Plan checks for synced tables. Each table in sync/registry.py lists the ones
that apply to it in `plan_gates`; the push view runs them before saving a
create or an update. A table nothing limits says so with NO_GATES.
"""
import copy

from . import gates

#: For a table no plan limits. An empty list on purpose, so forgetting is a test failure.
NO_GATES = ()


class Gate:
    def check(self, workspace, obj, creating, before):
        """
        `obj` carries the new values. `before` holds the old value of every
        column this push changes, or None when the row is being created.
        """
        raise NotImplementedError


class Limit(Gate):
    """At most N rows that count. Checked on create, and when an edit makes a row count again."""

    def __init__(self, feature, counts=lambda obj: True):
        self.feature = feature
        self.counts = counts

    def check(self, workspace, obj, creating, before):
        if not self.counts(obj):
            return
        if not creating:
            earlier = copy.copy(obj)
            for column, value in before.items():
                setattr(earlier, column, value)
            if self.counts(earlier):
                return  # it already had its place
        gates.check_limit(workspace, self.feature)


class Flag(Gate):
    """The plan must include the feature, for the rows `when` picks out."""

    def __init__(self, feature, when=lambda obj: True):
        self.feature = feature
        self.when = when

    def check(self, workspace, obj, creating, before):
        if self.when(obj):
            gates.require(workspace, self.feature)


class ColumnFlag(Gate):
    """
    The plan must include the feature to change one column. Other edits of
    the row go through whatever the column holds, so a downgrade never
    freezes the whole row. A new row is never refused over the column: it
    is kept without it (`empty`), since a phone drops a refused create.
    """

    def __init__(self, feature, column, empty):
        self.feature = feature
        self.column = column
        self.empty = empty

    def check(self, workspace, obj, creating, before):
        value = getattr(obj, self.column)
        if creating:
            if value == self.empty:
                return
            try:
                gates.require(workspace, self.feature)
            except gates.PlanLimit as error:
                setattr(obj, self.column, copy.deepcopy(self.empty))
                gates.record_hit(gates.payer_of(workspace), error.feature_key, error.meta['plan'])
            return
        if self.column in before and before[self.column] != value:
            gates.require(workspace, self.feature)


class Lock(Gate):
    """
    Read-only when a downgrade locked the row `target` points at: the row
    itself, or the parent it hangs off. `unless` lets through the edits that
    take a row out of the count, such as archiving it.
    """

    def __init__(self, feature, target=lambda obj: obj.pk, unless=lambda obj: False):
        self.feature = feature
        self.target = target
        self.unless = unless

    def check(self, workspace, obj, creating, before):
        row_id = self.target(obj)
        if row_id is None or (creating and row_id == obj.pk) or self.unless(obj):
            return
        gates.assert_row_writable(workspace, self.feature, row_id)
