"""
Sends a push notification through Firebase Cloud Messaging (the HTTP v1 API).

The server signs in to Google as the service account whose key file
FCM_CREDENTIALS_FILE names. Without one nothing is sent: `send` says so in the
log and answers SKIPPED, so everything else works on a machine with no key.
"""
import json
import logging
import threading
import time

import jwt
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SEND_URL = 'https://fcm.googleapis.com/v1/projects/{project}/messages:send'
TIMEOUT = 10
#: The Android channel the app makes for these (lib/core/push/push_service.dart)
CHANNEL = 'reminders'

SENT = 'sent'
#: The phone no longer has the app, or signed out: the device is to be forgotten
GONE = 'gone'
SKIPPED = 'skipped'
#: Refused for what the message says; sending it again would be refused again
REFUSED = 'refused'

_lock = threading.Lock()
_account = None
_access = (None, 0)


class PushError(Exception):
    """Could not be sent now; worth trying again."""


def _credentials():
    global _account
    path = getattr(settings, 'FCM_CREDENTIALS_FILE', '')
    if not path:
        return None
    if _account is None or _account[0] != path:
        try:
            with open(path, encoding='utf-8') as file:
                _account = (path, json.load(file))
        except (OSError, ValueError) as error:
            logger.error('FCM_CREDENTIALS_FILE cannot be read: %s', error)
            return None
    return _account[1]


def configured():
    return _credentials() is not None


def _access_token(account):
    global _access
    with _lock:
        token, expires = _access
        if token and time.time() < expires - 60:
            return token
        now = int(time.time())
        token_url = account.get('token_uri', TOKEN_URL)
        assertion = jwt.encode(
            {'iss': account['client_email'], 'scope': SCOPE, 'aud': token_url, 'iat': now, 'exp': now + 3600},
            account['private_key'], algorithm='RS256', headers={'kid': account.get('private_key_id', '')},
        )
        try:
            response = requests.post(
                token_url, timeout=TIMEOUT,
                data={'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion},
            )
        except requests.RequestException as error:
            raise PushError(f'Google could not be reached: {error}')
        if response.status_code != 200:
            raise PushError(f'Google refused the service account ({response.status_code}): {response.text[:200]}')
        answer = response.json()
        _access = (answer['access_token'], time.time() + answer.get('expires_in', 3600))
        return _access[0]


def _forget_access():
    global _access
    with _lock:
        _access = (None, 0)


def send(token, title, body, data=None):
    """
    One notification to one phone. Answers SENT, GONE, SKIPPED or REFUSED;
    raises PushError when it is worth trying again.
    """
    account = _credentials()
    if account is None:
        logger.info('Push not sent, no FCM credentials: %s', title)
        return SKIPPED
    message = {
        'token': token,
        'notification': {'title': title, **({'body': body} if body else {})},
        # Everything in `data` is text for Firebase
        'data': {key: '' if value is None else str(value) for key, value in (data or {}).items()},
        'android': {'priority': 'high', 'notification': {'channel_id': CHANNEL}},
    }
    try:
        response = requests.post(
            SEND_URL.format(project=account['project_id']), timeout=TIMEOUT, json={'message': message},
            headers={'Authorization': f'Bearer {_access_token(account)}'},
        )
    except requests.RequestException as error:
        raise PushError(f'Firebase could not be reached: {error}')
    if response.status_code == 200:
        return SENT
    try:
        error = response.json().get('error', {})
    except ValueError:
        error = {}
    code = next(
        (detail.get('errorCode') for detail in error.get('details', []) if detail.get('errorCode')),
        error.get('status', ''),
    )
    # A token made for another Firebase project never works here either
    if response.status_code == 404 or code in ('UNREGISTERED', 'SENDER_ID_MISMATCH'):
        return GONE
    if response.status_code == 401:
        _forget_access()
        raise PushError('Firebase did not accept the access token.')
    if response.status_code in (403, 429) or response.status_code >= 500:
        raise PushError(f'Firebase answered {response.status_code} {code}: {error.get("message", "")}')
    logger.error('Firebase refused a push (%s %s): %s', response.status_code, code, error.get('message', ''))
    return REFUSED
