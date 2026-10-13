import uuid
from datetime import time

from django.conf import settings
from django.db import models
from django.utils.timezone import now

from spendroo.fields import EncryptedTextField


class EmailAccounts(models.Model):
    name = models.CharField(max_length=100, help_text="Friendly name for this email account")
    email_address = models.EmailField(max_length=100, help_text="Email address to send from")
    smtp_server = models.CharField(max_length=100, help_text="SMTP server address")
    smtp_port = models.IntegerField(default=465, help_text="SMTP server port")
    username = models.CharField(max_length=100, help_text="SMTP username",null=True,blank=True)
    password = EncryptedTextField(help_text="SMTP password, stored encrypted (Use app password for gmail accounts!)")
    use_tls = models.BooleanField(default=False, help_text="Use TLS for connection")
    use_ssl = models.BooleanField(default=True, help_text="Use SSL for connection")
    is_active = models.BooleanField(default=True, help_text="Is this account active?")

    PURPOSE_CHOICES = [
        ('default','Default'),
        ('marketing', 'Marketing Emails'),
        ('transactional', 'Transactional Emails'),
        ('notification', 'Notification Emails'),
        ('support', 'Support Emails'),
        ('auth','Authentication'),
        ('no-reply','No Reply'),
        ('other', 'Other'),
    ]

    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, help_text="Email purpose")

    class Meta:
        verbose_name = "Email Account"
        verbose_name_plural = "Email Accounts"

    def __str__(self):
        return str(self.pk)

class EmailTemplate(models.Model):
    """Model to store email templates"""
    name = models.CharField(max_length=100, help_text="Template name (e.g: auth_signup)",unique=True)
    subject = models.CharField(max_length=200, help_text="Email subject")
    body_text = models.TextField(help_text="Plain text email body")
    body_html = models.TextField(blank=True, null=True, help_text="HTML email body")

    PURPOSE_CHOICES = [
        ('default','Default'),
        ('marketing', 'Marketing Emails'),
        ('transactional', 'Transactional Emails'),
        ('notification', 'Notification Emails'),
        ('support', 'Support Emails'),
        ('auth','Authentication'),
        ('other', 'Other'),
    ]
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, help_text="Template purpose")

    def __str__(self):
        return str(self.pk)

class EmailLog(models.Model):
    """
    Model to track email sending status and history
    """
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('SENT', 'Sent'),
        ('FAILED', 'Failed'),
        ('RETRY', 'Retry'),
    )

    to_emails = models.TextField()  # Store as comma-separated list
    sender_email = models.EmailField(null=True, blank=True)
    subject = models.CharField(max_length=255)
    purpose = models.CharField(max_length=100, default='default')
    template_name = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    error_message = models.TextField(null=True, blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Email Log'
        verbose_name_plural = 'Email Logs'

    def __str__(self):
        return f"{self.subject} - {self.status} - {self.created_at}"


class Device(models.Model):
    """
    A phone the app is signed in on, by the address Firebase gives it for
    push. The app says so after sign-in and takes it back before signing out.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='devices')
    # One row per phone: signing in as someone else on it moves the row to them
    fcm_token = models.CharField(max_length=512, unique=True)
    platform = models.CharField(max_length=10, default='android')
    # The language the app is shown in there: a push is worded in it
    locale = models.CharField(max_length=10, default='en')
    timezone = models.CharField(max_length=64, blank=True, default='')
    app_version = models.CharField(max_length=40, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ['-last_seen_at']

    def __str__(self):
        return f'{self.platform} of {self.user_id}'


class NotificationPreference(models.Model):
    """What a user wants to be told by push, and when. Without a row, the defaults below."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_preference',
    )
    push_enabled = models.BooleanField(default=True)
    # Bills coming up, due and waiting, and plain reminders
    bills = models.BooleanField(default=True)
    # Income that is expected
    income = models.BooleanField(default=True)
    # What was recorded by itself, or could not be
    recorded = models.BooleanField(default=True)
    budgets = models.BooleanField(default=True)
    # Savings goals: milestones, falling behind, a long spell with nothing put in
    goals = models.BooleanField(default=True)
    # "Did you spend anything today?" The phone schedules this one itself
    daily_nudge = models.BooleanField(default=True)
    nudge_time = models.TimeField(default=time(21, 0))
    # The hour reminders come at, in the user's own time, when a rule names none
    reminder_time = models.TimeField(default=time(9, 0))
    # Nothing is pushed between these; it waits for the end. Both empty: no quiet hours
    quiet_from = models.TimeField(null=True, blank=True)
    quiet_to = models.TimeField(null=True, blank=True)

    #: inbox item type -> the switch that turns its push off
    SWITCHES = {
        'upcoming': 'bills', 'due': 'bills', 'overdue': 'bills', 'reminder': 'bills',
        'income_expected': 'income',
        'posted': 'recorded', 'rule_blocked': 'recorded',
        'budget_80': 'budgets', 'budget_over': 'budgets',
        'goal_milestone': 'goals', 'goal_reached': 'goals', 'goal_behind': 'goals', 'goal_idle': 'goals',
    }

    def __str__(self):
        return f'Notification preferences of {self.user_id}'

    @classmethod
    def of(cls, user_id):
        """The user's preferences; the defaults, unsaved, when they never changed any."""
        return cls.objects.filter(user_id=user_id).first() or cls(user_id=user_id)

    def wants(self, kind):
        return self.push_enabled and getattr(self, self.SWITCHES.get(kind, 'bills'))

    def is_quiet(self, at):
        """Whether `at`, a time of day in the user's zone, falls in their quiet hours."""
        if self.quiet_from is None or self.quiet_to is None or self.quiet_from == self.quiet_to:
            return False
        if self.quiet_from < self.quiet_to:
            return self.quiet_from <= at < self.quiet_to
        # Through midnight
        return at >= self.quiet_from or at < self.quiet_to
