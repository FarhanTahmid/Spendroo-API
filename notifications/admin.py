from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import Device, EmailAccounts, EmailLog, EmailTemplate, NotificationPreference


class EmailAccountsForm(forms.ModelForm):
    # Never render the stored password back into the page
    password = forms.CharField(
        widget=forms.PasswordInput(render_value=False), required=False,
        help_text="Leave blank to keep the current password."
    )

    class Meta:
        model = EmailAccounts
        fields = '__all__'

    def clean_password(self):
        password = self.cleaned_data.get('password')
        if not password:
            if not self.instance.pk:
                raise forms.ValidationError('Password is required.')
            return self.instance.password
        return password


@admin.register(EmailAccounts)
class EmailAccountsAdmin(ModelAdmin):
    form = EmailAccountsForm
    list_display = ('name', 'email_address', 'purpose','smtp_server','smtp_port','is_active')
    list_filter = ('purpose','is_active','smtp_server','smtp_port','is_active')
    search_fields = ('name', 'email_address', 'smtp_server','purpose')

@admin.register(EmailTemplate)
class EmailTemplateAdmin(ModelAdmin):
    list_display = ('name', 'subject', 'purpose')
    list_filter = ('purpose',)
    search_fields = ('name', 'subject', 'body_text','body_html','purpose')

@admin.register(EmailLog)
class EmailLogAdmin(ModelAdmin):
    list_display = ('to_emails', 'subject', 'status', 'created_at','purpose')
    list_filter = ('status', 'created_at','purpose')
    search_fields = ('to_emails', 'subject', 'status','purpose')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)


@admin.register(Device)
class DeviceAdmin(ModelAdmin):
    list_display = ('user', 'platform', 'locale', 'timezone', 'app_version', 'last_seen_at')
    list_filter = ('platform', 'locale')
    search_fields = ('user__email', 'user__username')
    # The token is the phone's address: shown to no one
    exclude = ('fcm_token',)
    readonly_fields = ('user', 'platform', 'locale', 'timezone', 'app_version', 'created_at', 'last_seen_at')


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(ModelAdmin):
    list_display = ('user', 'push_enabled', 'bills', 'income', 'recorded', 'budgets', 'daily_nudge', 'reminder_time')
    list_filter = ('push_enabled', 'daily_nudge')
    search_fields = ('user__email', 'user__username')
