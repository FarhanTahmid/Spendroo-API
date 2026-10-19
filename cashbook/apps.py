from django.apps import AppConfig


class CashbookConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cashbook'

    def ready(self):
        from . import bills  # noqa: F401  (gives storage back when a bill is hard-deleted)
