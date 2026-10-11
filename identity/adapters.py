from allauth.account.adapter import DefaultAccountAdapter

from .usernames import generate_unique_username


class AccountAdapter(DefaultAccountAdapter):
    def populate_username(self, request, user):
        # Email sign-up and Google sign-in both end up here
        if not user.username:
            user.username = generate_unique_username(user.email)

    def send_account_already_exists_mail(self, email):
        # allauth puts links to a website in this mail; there is only the app
        # (templates/account/email/account_already_exists_message.txt)
        self.send_mail('account/email/account_already_exists', email, {})
