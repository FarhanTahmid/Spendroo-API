from django.apps import AppConfig


class LedgerPersonalConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ledger_personal'

    def ready(self):
        from . import receipts, signals  # noqa: F401  (receipts registers itself as a kind of attachment)
