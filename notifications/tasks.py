import procrastinate
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from procrastinate.contrib.django import app

from . import delivery, widgets
from .email_handler import EmailHandler
from .push import PushError


class EmailDeliveryError(Exception):
    pass


def deliver_email(subject, body, to, from_email=None, html_body=None, purpose='default'):
    """
    Send one email now. Uses the SMTP account configured in the admin for this
    purpose when there is one, otherwise settings.EMAIL_DELIVERY_BACKEND.
    """
    if EmailHandler.get_email_account(purpose):
        success, message, _ = EmailHandler.send_email(
            to_emails=to, subject=subject, text_content=body, html_content=html_body, purpose=purpose
        )
        if not success:
            raise EmailDeliveryError(message)
        return

    email = EmailMultiAlternatives(
        subject=subject, body=body, from_email=from_email, to=to,
        connection=get_connection(settings.EMAIL_DELIVERY_BACKEND),
    )
    if html_body:
        email.attach_alternative(html_body, 'text/html')
    email.send()


@app.task(queue='email', retry=procrastinate.RetryStrategy(max_attempts=5, exponential_wait=10))
def send_email(subject, body, to, from_email=None, html_body=None, purpose='default'):
    deliver_email(subject, body, to, from_email=from_email, html_body=html_body, purpose=purpose)


# Tried again only when Firebase could not be reached: a phone that is gone is forgotten, not retried
@app.task(queue='push', retry=procrastinate.RetryStrategy(max_attempts=5, exponential_wait=10, retry_exceptions=[PushError]))
def send_push(item_id):
    delivery.send_item(item_id)


# A silent push to the phones whose home-screen widgets show a business that changed
@app.task(queue='push', retry=procrastinate.RetryStrategy(max_attempts=5, exponential_wait=10, retry_exceptions=[PushError]))
def notify_widgets(workspace_id):
    widgets.notify(workspace_id)
