from django.apps import AppConfig


class AttachmentsConfig(AppConfig):
    """
    Files kept with a row as proof: bills of business entries, receipts of
    personal transactions. One way in, one storage, one plan allowance; each
    app says which of its rows take attachments (kinds.py).
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'attachments'
