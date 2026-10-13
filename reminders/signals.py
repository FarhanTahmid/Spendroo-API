from django.db.models.signals import post_save
from django.dispatch import receiver

from ledger_personal.models import Transaction

from . import engine
from .models import Rule


@receiver(post_save, sender=Transaction)
def settle_occurrence(sender, instance, **kwargs):
    engine.transaction_saved(instance)


@receiver(post_save, sender=Rule)
def close_deleted_rule(sender, instance, **kwargs):
    # Deletes arrive through sync without passing any hook: nothing of a deleted rule stays open
    if instance.deleted_at is not None:
        engine.advance(instance.pk)
