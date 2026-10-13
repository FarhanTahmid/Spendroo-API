"""
What counts toward each limit, declared once.

The gate that refuses a create, the choice of what stays editable after a
downgrade, and the meters in the admin all read these rules, so they cannot
disagree about what "4 accounts" means.
"""
from cashbook.models import CashBook
from ledger_personal.models import Account, Category
from reminders.models import Rule
from workspaces.models import Membership, Workspace

from .catalog.keys import F


class LimitRule:
    feature = None
    #: Counted inside one workspace (True) or across everything the owner has (False)
    per_workspace = True
    #: What the user calls one of these
    noun = 'item'

    def queryset(self, owner, workspace):
        """The rows that count, alive ones only."""
        raise NotImplementedError

    def scope(self, workspace):
        return str(workspace.pk) if self.per_workspace else ''

    def label(self, obj):
        return str(obj)


class PersonalAccounts(LimitRule):
    feature = F.PERSONAL_ACCOUNTS
    noun = 'account'

    def queryset(self, owner, workspace):
        return Account.objects.filter(workspace=workspace, is_archived=False)

    def label(self, obj):
        return obj.name


class CustomCategories(LimitRule):
    feature = F.PERSONAL_CUSTOM_CATEGORIES
    noun = 'category'

    def queryset(self, owner, workspace):
        return Category.objects.filter(workspace=workspace, is_default=False)

    def label(self, obj):
        return obj.name


class StandaloneReminders(LimitRule):
    feature = F.REMINDERS_STANDALONE
    noun = 'reminder'

    def queryset(self, owner, workspace):
        return Rule.objects.filter(workspace=workspace, kind=Rule.KIND_REMINDER, is_paused=False)

    def label(self, obj):
        return obj.title


class Cashbooks(LimitRule):
    feature = F.BUSINESS_CASHBOOKS
    noun = 'cashbook'

    def queryset(self, owner, workspace):
        return CashBook.objects.filter(workspace=workspace)

    def label(self, obj):
        return obj.book_name


class BusinessWorkspaces(LimitRule):
    feature = F.BUSINESS_WORKSPACES
    per_workspace = False
    noun = 'business'

    def queryset(self, owner, workspace=None):
        # The demo business is a sample, not one of theirs
        return Workspace.objects.filter(owner=owner, kind=Workspace.KIND_BUSINESS, is_demo=False)

    def label(self, obj):
        return obj.name


class TeamSeats(LimitRule):
    feature = F.TEAM_SEATS
    noun = 'team member'

    def queryset(self, owner, workspace):
        return Membership.objects.filter(workspace=workspace).exclude(role=Membership.ROLE_OWNER)

    def label(self, obj):
        return obj.user.email


RULES = {rule.feature.key: rule for rule in (
    PersonalAccounts(), CustomCategories(), StandaloneReminders(), Cashbooks(), BusinessWorkspaces(), TeamSeats(),
)}


def rule_for(feature):
    return RULES[str(feature)]
