"""
A note of every report handed out. The file itself is never kept.

Like the billing tables this is not tenant data: a row belongs to the user
who downloaded, is never synced, and only reports/ writes it.
"""
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

SCOPE_PERSONAL = 'personal'
SCOPE_ACCOUNT = 'account'
SCOPE_CASHBOOK = 'cashbook'
SCOPE_BUSINESS = 'business'
SCOPE_CHOICES = [
    (SCOPE_PERSONAL, 'Personal report'),
    (SCOPE_ACCOUNT, 'Account statement'),
    (SCOPE_CASHBOOK, 'Cashbook report'),
    (SCOPE_BUSINESS, 'Business report'),
]

FILE_PDF = 'pdf'
FILE_XLSX = 'xlsx'
FILE_CHOICES = [(FILE_PDF, 'PDF'), (FILE_XLSX, 'Excel')]

# No 0/O or 1/I: the reference is read off paper
_REFERENCE_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'


def new_reference(today=None):
    """Printed in the footer of a report, e.g. SR-20261012-7F3K2A."""
    today = today or timezone.localdate()
    tail = ''.join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(6))
    return f'SR-{today:%Y%m%d}-{tail}'


class ReportDownload(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='report_downloads')
    workspace = models.ForeignKey('workspaces.Workspace', on_delete=models.CASCADE, related_name='report_downloads')
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES)
    # The account, cashbook or workspace the report is about, and its name on that day
    subject_id = models.UUIDField()
    subject_name = models.CharField(max_length=100)
    file_type = models.CharField(max_length=5, choices=FILE_CHOICES)
    period = models.CharField(max_length=20)
    date_from = models.DateField()
    date_to = models.DateField()
    language = models.CharField(max_length=5, default='en')
    # What a custom report was narrowed to, in words: {"Type": "Spending", "Categories": "Food, Rent"}
    filters = models.JSONField(default=dict, blank=True)
    row_count = models.PositiveIntegerField(default=0, help_text='Detail rows in the report.')
    reference = models.CharField(max_length=24, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'created_at']),
            models.Index(fields=['scope', 'file_type', 'created_at']),
        ]

    def __str__(self):
        return f"{self.reference}: {self.get_scope_display()} ({self.file_type})"
