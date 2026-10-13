from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from ledger_personal import allocations
from ledger_personal.models import Account, Category
from workspaces.models import WorkspaceOwnedModel

from . import schedule


class Rule(WorkspaceOwnedModel):
    """
    Something that comes round again: a transaction to record (rent, salary,
    a transfer to savings), or a plain reminder with nothing to record. The
    app writes the rule; the engine (engine.py) works out when it falls due.
    """
    KIND_REMINDER = 'reminder'
    KIND_CHOICES = [
        ('expense', 'Expense'),
        ('income', 'Income'),
        ('transfer', 'Transfer'),
        (KIND_REMINDER, 'Reminder'),
    ]
    MODE_AUTO = 'auto'
    MODE_CONFIRM = 'confirm'
    MODE_CHOICES = [
        (MODE_AUTO, 'Recorded by the server'),
        (MODE_CONFIRM, 'The user is asked first'),
    ]
    #: Changing one of these moves the dates still to come
    TIMING_FIELDS = (
        'freq', 'interval', 'weekday_mask', 'month_day', 'set_pos', 'week_start', 'start_on', 'end_on', 'max_count',
        'mode', 'is_paused', 'kind', 'remind_days_before',
    )

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    title = models.CharField(max_length=100, blank=True, default='')
    # What to record. A reminder has no account; its amount is only shown.
    account = models.ForeignKey(Account, on_delete=models.CASCADE, null=True, blank=True, related_name='rules')
    to_account = models.ForeignKey(Account, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # As the user typed it, always positive; the sign follows the kind when it is recorded
    amount_minor = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, editable=False)
    note = models.CharField(max_length=200, blank=True, default='')
    splits = models.JSONField(default=list, blank=True)

    # When (schedule.py)
    freq = models.CharField(max_length=10, choices=[(freq, freq) for freq in schedule.FREQUENCIES])
    interval = models.PositiveSmallIntegerField(default=1)
    weekday_mask = models.PositiveSmallIntegerField(default=0)
    month_day = models.SmallIntegerField(null=True, blank=True)
    set_pos = models.SmallIntegerField(null=True, blank=True)
    # Copied from the user's setting when the rule is made, so changing the setting moves no dates
    week_start = models.PositiveSmallIntegerField(default=5)
    start_on = models.DateField()
    end_on = models.DateField(null=True, blank=True)
    # Stops after falling due this many times
    max_count = models.PositiveIntegerField(null=True, blank=True)

    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default=MODE_CONFIRM)
    # 0: on the day only
    remind_days_before = models.PositiveSmallIntegerField(default=0)
    # In the owner's own time; without one their usual reminder time is used
    remind_at = models.TimeField(null=True, blank=True)
    is_paused = models.BooleanField(default=False)

    # Kept by the engine
    next_due_on = models.DateField(null=True, blank=True, editable=False)
    done_count = models.PositiveIntegerField(default=0, editable=False)
    # Nothing more will fall due. Not sent to the app: no next_due_on says the same.
    ended = models.BooleanField(default=False, editable=False)
    # The dates to come are to be worked out again
    needs_refresh = models.BooleanField(default=True, editable=False)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['is_paused', 'ended', 'next_due_on'], name='rule_due')]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_minor__isnull=True) | Q(amount_minor__gt=0), name='rule_amount_positive',
            ),
        ]

    def __str__(self):
        return f'{self.title or self.kind} ({self.freq})'

    @property
    def is_reminder(self):
        return self.kind == self.KIND_REMINDER

    def clean(self):
        errors = {}
        if self.is_reminder:
            if not self.title.strip():
                errors['title'] = 'A reminder needs a title.'
            if self.account_id or self.to_account_id or self.category_id or self.splits:
                errors['kind'] = 'A reminder records nothing: it has no account, category or split.'
            # There is nothing for the server to record on its own
            self.mode = self.MODE_CONFIRM
        else:
            if self.account_id is None:
                errors['account'] = 'Choose the account.'
            if not self.amount_minor:
                errors['amount_minor'] = 'Give the amount.'
        if self.kind == 'transfer':
            if self.to_account_id is None or self.to_account_id == self.account_id:
                errors['to_account'] = 'Choose a different account to move the money to.'
            elif self.account_id and self.to_account.currency != self.account.currency:
                errors['to_account'] = 'Both accounts of a transfer must hold the same currency.'
            if self.category_id or self.splits:
                errors['category'] = 'A transfer has no category.'
        elif self.to_account_id is not None:
            errors['to_account'] = 'Only a transfer has a second account.'
        if self.kind in allocations.PLAIN_KINDS and self.category_id and self.category.kind != self.kind:
            errors['category'] = f'Choose an {self.kind} category.'
        if self.remind_days_before > 30:
            errors['remind_days_before'] = 'At most 30 days before.'
        if errors:
            raise ValidationError(errors)
        self.splits = allocations.cleaned(self.splits, self.kind, self.amount_minor)
        schedule.check(self)

    def save(self, *args, **kwargs):
        self.currency = self.account.currency if self.account_id else self.workspace.default_currency
        if self.splits:
            self.category = None
        super().save(*args, **kwargs)


class Occurrence(WorkspaceOwnedModel):
    """
    One date a rule falls on. Made by the engine: the next one to come waits
    as `upcoming`, and on its day becomes `due` (to be confirmed by the user)
    or `done` (recorded). A rule has at most one upcoming occurrence.
    """
    UPCOMING, DUE, DONE, SKIPPED = 'upcoming', 'due', 'done', 'skipped'
    STATUS_CHOICES = [(UPCOMING, 'Upcoming'), (DUE, 'Due'), (DONE, 'Done'), (SKIPPED, 'Skipped')]
    OPEN = (UPCOMING, DUE)

    rule = models.ForeignKey(Rule, on_delete=models.CASCADE, related_name='occurrences')
    due_on = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=UPCOMING)
    # "Remind me later": nothing more is said about it before then
    snoozed_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['due_on']
        indexes = [models.Index(fields=['workspace', 'status', 'due_on'])]
        constraints = [
            # What makes working the dates out twice harmless
            models.UniqueConstraint(
                fields=['rule', 'due_on'], condition=Q(deleted_at__isnull=True), name='one_occurrence_per_rule_day',
            ),
        ]

    def __str__(self):
        return f'{self.rule} on {self.due_on}: {self.status}'

    def get_parent_workspace_id(self):
        return self.rule.workspace_id


class InboxItem(WorkspaceOwnedModel):
    """
    Something to tell the user: a bill coming up, a transaction recorded for
    them, a budget nearly spent. The app lists these and words them itself
    from `type` and `params`; a push notification says the same.
    """
    TYPES = (
        'upcoming', 'due', 'overdue', 'posted', 'income_expected', 'reminder', 'rule_blocked',
        'budget_80', 'budget_over',
    )

    type = models.CharField(max_length=20, choices=[(kind, kind) for kind in TYPES])
    # What the wording is filled from: names and amounts as they were at the time
    params = models.JSONField(default=dict, blank=True)
    # The row it is about, by sync table name, for opening it from the app
    subject_table = models.CharField(max_length=30, blank=True, default='')
    subject_id = models.UUIDField(null=True, blank=True)
    # Said once: making the same item again finds this one
    dedupe_key = models.CharField(max_length=120)
    read_at = models.DateTimeField(null=True, blank=True)
    # What it was about is settled: nothing left to do
    resolved_at = models.DateTimeField(null=True, blank=True)
    # When to push it, and when that was done. Not sent to the app.
    notify_at = models.DateTimeField()
    pushed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-notify_at']
        indexes = [models.Index(fields=['pushed_at', 'notify_at'], name='inbox_to_push')]
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'dedupe_key'], name='one_inbox_item_per_key'),
        ]

    def __str__(self):
        return f'{self.type} {self.dedupe_key}'
