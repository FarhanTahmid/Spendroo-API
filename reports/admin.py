from django.contrib import admin

from billing.admin.base import ReadOnlyAdmin

from .models import ReportDownload


@admin.register(ReportDownload)
class ReportDownloadAdmin(ReadOnlyAdmin):
    list_display = [
        'created_at', 'reference', 'user', 'scope', 'subject_name', 'file_type', 'period', 'date_from', 'date_to',
        'row_count', 'filters',
    ]
    list_filter = ['scope', 'file_type', 'period', 'language']
    search_fields = ['reference', 'user__email', 'subject_name']
    list_select_related = ['user']
    date_hierarchy = 'created_at'
