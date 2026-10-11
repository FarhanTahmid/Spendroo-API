from allauth.account.models import EmailAddress
from allauth.account.signals import password_changed, password_set
from allauth.socialaccount.signals import social_account_added
from django.dispatch import receiver

from audit.utils.audit_utils import AuditLogMixin


def _log(request, user, operation):
    if request is None:
        return
    AuditLogMixin().log_action(request, 'UPDATE', 'AppUser', user.pk, operation=operation)


@receiver(password_changed)
def log_password_changed(sender, request, user, **kwargs):
    _log(request, user, 'password_changed')


@receiver(password_set)
def log_password_set(sender, request, user, **kwargs):
    _log(request, user, 'password_set')


@receiver(social_account_added)
def log_social_account_added(sender, request, sociallogin, **kwargs):
    # An account that already existed got Google (or another provider) linked to it
    _log(request, sociallogin.user, f'{sociallogin.account.provider}_connected')
    # The provider vouches for these addresses, so the user is not asked for an emailed code as well.
    # By now allauth has removed the password of an account that never verified its email.
    for address in sociallogin.email_addresses:
        if address.verified:
            EmailAddress.objects.filter(
                user=sociallogin.user, email__iexact=address.email, verified=False,
            ).update(verified=True)
