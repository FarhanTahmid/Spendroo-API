import importlib
import os
import re
import shutil
import tempfile
import time
from io import BytesIO
from unittest import mock

import jwt
from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from audit.models import CRUDLog
from workspaces.models import Workspace
from .models import AppUser

AUTH = '/_allauth/app/v1/auth'
PASSWORD = 'a-long-pass-987'


def code_from_last_email():
    """Verification codes are emailed on their own line"""
    return re.search(r'^\s*([A-Z0-9]{4,}(?:-[A-Z0-9]+)*)\s*$', mail.outbox[-1].body, re.MULTILINE).group(1)


class AuthTestCase(TestCase):
    def setUp(self):
        cache.clear()  # rate-limit counters
        self.client = APIClient()

    def post(self, path, data, token=None):
        headers = {'X-Session-Token': token} if token else {}
        return self.client.post(f'{AUTH}{path}', data, format='json', headers=headers)

    def signup(self, email='alice@example.com'):
        """Sign up and verify the email; returns the session token"""
        response = self.post('/signup', {'email': email, 'password': PASSWORD})
        self.assertEqual(response.status_code, 401, response.content)  # pending email verification
        token = response.json()['meta']['session_token']
        response = self.post('/email/verify', {'key': code_from_last_email()}, token=token)
        self.assertEqual(response.status_code, 200, response.content)
        # Logging in rotates the session token
        return response.json()['meta'].get('session_token', token)


class SignupAndLoginTests(AuthTestCase):
    def test_signup_requires_email_verification_then_grants_access(self):
        response = self.post('/signup', {'email': 'alice@example.com', 'password': PASSWORD})
        self.assertEqual(response.status_code, 401)
        token = response.json()['meta']['session_token']

        # Not usable against the API until the email is verified
        me = self.client.get('/api/v1/me/', headers={'X-Session-Token': token})
        self.assertIn(me.status_code, (401, 403))

        response = self.post('/email/verify', {'key': code_from_last_email()}, token=token)
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()['meta'].get('session_token', token)

        me = self.client.get('/api/v1/me/', headers={'X-Session-Token': token})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data['email'], 'alice@example.com')

        user = AppUser.objects.get(email='alice@example.com')
        self.assertTrue(user.password.startswith(('argon2', 'md5')))
        self.assertEqual(Workspace.objects.get(owner=user).kind, 'personal')

    def test_login_and_logout(self):
        self.signup()
        response = self.post('/login', {'email': 'alice@example.com', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()['meta']['session_token']
        headers = {'X-Session-Token': token}
        self.assertEqual(self.client.get('/api/v1/cashbooks/', headers=headers).status_code, 200)

        # Logout revokes the token on the server
        self.client.delete(f'{AUTH}/session', headers=headers)
        self.assertIn(self.client.get('/api/v1/me/', headers=headers).status_code, (401, 403))

    def test_wrong_password_is_rejected(self):
        self.signup()
        response = self.post('/login', {'email': 'alice@example.com', 'password': 'wrong-password'})
        self.assertEqual(response.status_code, 400)

    def test_api_requires_authentication(self):
        for path in ('/api/v1/me/', '/api/v1/cashbooks/', '/api/v1/workspaces/'):
            self.assertIn(self.client.get(path).status_code, (401, 403), path)

    def test_weak_password_is_rejected(self):
        response = self.post('/signup', {'email': 'alice@example.com', 'password': '12345'})
        self.assertEqual(response.status_code, 400)


class PasswordTests(AuthTestCase):
    def test_password_reset_needs_the_emailed_code(self):
        self.signup()
        self.assertEqual(self.post('/password/request', {'email': 'alice@example.com'}).status_code, 401)
        code = code_from_last_email()

        # Without the code the password cannot be changed
        response = self.post('/password/reset', {'key': 'WRONG1', 'password': 'new-long-pass-456'})
        self.assertNotEqual(response.status_code, 200)
        user = AppUser.objects.get(email='alice@example.com')
        self.assertTrue(user.check_password(PASSWORD))

    def test_password_reset_with_code(self):
        self.signup()
        response = self.post('/password/request', {'email': 'alice@example.com'})
        token = response.json()['meta']['session_token']
        response = self.post('/password/reset', {'key': code_from_last_email(), 'password': 'new-long-pass-456'}, token=token)
        self.assertIn(response.status_code, (200, 401), response.content)
        user = AppUser.objects.get(email='alice@example.com')
        self.assertTrue(user.check_password('new-long-pass-456'))

    def test_change_password(self):
        token = self.signup()
        response = self.client.post(
            '/_allauth/app/v1/account/password/change',
            {'current_password': PASSWORD, 'new_password': 'new-long-pass-456'},
            format='json', headers={'X-Session-Token': token},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(AppUser.objects.get(email='alice@example.com').check_password('new-long-pass-456'))


class ProfileTests(AuthTestCase):
    def test_profile_update_cannot_change_email(self):
        user = AppUser.objects.create_user(email='alice@example.com', password=PASSWORD)
        self.client.force_authenticate(user)
        response = self.client.patch('/api/v1/me/', {'first_name': 'Alice', 'email': 'evil@example.com'})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.email), ('Alice', 'alice@example.com'))


class OnboardingTests(AuthTestCase):
    def test_onboarding_choice_is_kept_on_the_server(self):
        user = AppUser.objects.create_user(email='alice@example.com', password=PASSWORD)
        self.client.force_authenticate(user)
        profile = self.client.get('/api/v1/me/').json()
        self.assertEqual((profile['onboarded_at'], profile['primary_mode']), (None, ''))

        response = self.client.patch(
            '/api/v1/me/', {'onboarded_at': '2026-10-03T10:00:00Z', 'primary_mode': 'business'}, format='json'
        )
        self.assertEqual(response.status_code, 200, response.content)
        profile = self.client.get('/api/v1/me/').json()
        self.assertTrue(profile['onboarded_at'].startswith('2026-10-03'))
        self.assertEqual(profile['primary_mode'], 'business')
        self.assertEqual(self.client.patch('/api/v1/me/', {'primary_mode': 'galaxy'}).status_code, 400)


class SchemaTests(AuthTestCase):
    def test_health_and_openapi_schema(self):
        self.assertEqual(self.client.get('/health/').json(), {'status': 'ok'})
        self.assertEqual(self.client.get('/health/?db=1').json(), {'status': 'ok', 'database': 'ok'})
        response = self.client.get('/api/v1/schema/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('/api/v1/cashbooks/', response.data['paths'])
        self.assertIn('/api/v1/me/', response.data['paths'])
        for path in ('/api/v1/me/username/', '/api/v1/me/username/check/', '/api/v1/me/avatar/'):
            self.assertIn(path, response.data['paths'])


def png_bytes(size=(1024, 1024), mode='RGBA'):
    out = BytesIO()
    Image.new(mode, size, (200, 30, 30, 128) if mode == 'RGBA' else (200, 30, 30)).save(out, 'PNG')
    return out.getvalue()


class UsernameGenerationTests(AuthTestCase):
    def test_username_is_the_email_part_before_the_at(self):
        user = AppUser.objects.create_user(email='farhantahmidwork@gmail.com', password=PASSWORD)
        self.assertEqual(user.username, 'farhantahmidwork')

    def test_taken_username_gets_four_random_digits(self):
        AppUser.objects.create_user(email='farhantahmidwork@gmail.com', password=PASSWORD)
        second = AppUser.objects.create_user(email='farhantahmidwork@yahoo.com', password=PASSWORD)
        self.assertRegex(second.username, r'^farhantahmidwork\d{4}$')

    def test_case_only_clash_counts_as_taken(self):
        AppUser.objects.create_user(email='Bob@x.com', password=PASSWORD)
        second = AppUser.objects.create_user(email='bob@y.com', password=PASSWORD)
        self.assertRegex(second.username, r'^bob\d{4}$')

    def test_signup_through_allauth_uses_the_same_rule(self):
        self.signup('carol@example.com')
        self.signup('carol@example.org')
        self.assertEqual(AppUser.objects.get(email='carol@example.com').username, 'carol')
        self.assertRegex(AppUser.objects.get(email='carol@example.org').username, r'^carol\d{4}$')

    def test_google_signup_uses_the_same_rule(self):
        AppUser.objects.create_user(email='dave@gmail.com', password=PASSWORD)
        user = AppUser(email='dave@company.com')
        get_adapter().populate_username(RequestFactory().get('/'), user)
        self.assertRegex(user.username, r'^dave\d{4}$')

    def test_one_account_per_email(self):
        self.signup('erin@example.com')
        with self.assertRaises(IntegrityError), transaction.atomic():
            AppUser.objects.create_user(email='erin@example.com', password=PASSWORD)
        cache.clear()  # the first sign-up's code counts against the email rate limit
        response = self.post('/signup', {'email': 'erin@example.com', 'password': PASSWORD})
        # Answered like a new sign-up; the owner gets a mail instead of a code
        self.assertEqual(response.status_code, 401, response.content)
        self.assertIn('already exists', mail.outbox[-1].body)
        self.assertEqual(AppUser.objects.filter(email__iexact='erin@example.com').count(), 1)

    def test_database_rejects_case_only_duplicate(self):
        AppUser.objects.create_user(email='a@x.com', username='Frank', password=PASSWORD)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AppUser.objects.create_user(email='b@x.com', username='frank', password=PASSWORD)

    def test_migration_gives_later_clashing_accounts_digits(self):
        migration = importlib.import_module('identity.migrations.0003_dedupe_usernames_case_insensitive')
        renames = migration.resolve_clashes([(1, 'Sam'), (2, 'sam'), (3, 'other'), (4, 'SAM')])
        self.assertEqual(set(renames), {2, 4})
        self.assertRegex(renames[2], r'^sam\d{4}$')
        self.assertRegex(renames[4], r'^SAM\d{4}$')
        self.assertNotEqual(renames[2].lower(), renames[4].lower())


class UsernameLoginTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.signup('grace@example.com')

    def test_login_with_username(self):
        response = self.post('/login', {'username': 'grace', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['meta']['session_token'])

    def test_username_login_ignores_case(self):
        response = self.post('/login', {'username': 'GRACE', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)

    def test_login_with_email_still_works(self):
        response = self.post('/login', {'email': 'grace@example.com', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)

    def test_wrong_password_with_username(self):
        response = self.post('/login', {'username': 'grace', 'password': 'wrong-password'})
        self.assertEqual(response.status_code, 400)

    def test_email_and_username_together_are_rejected(self):
        response = self.post('/login', {'username': 'grace', 'email': 'grace@example.com', 'password': PASSWORD})
        self.assertEqual(response.status_code, 400)


class UsernameCheckTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.user = AppUser.objects.create_user(email='heidi@example.com', password=PASSWORD)
        AppUser.objects.create_user(email='ivan@example.com', password=PASSWORD)
        self.client.force_authenticate(self.user)

    def check(self, username):
        response = self.client.get('/api/v1/me/username/check/', {'username': username})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_free_username(self):
        self.assertEqual(self.check('newname'),
                         {'available': True, 'current': False, 'reason': None, 'message': None, 'suggestions': []})

    def test_taken_username_comes_with_free_suggestions(self):
        result = self.check('IVAN')
        self.assertEqual((result['available'], result['reason']), (False, 'taken'))
        self.assertEqual(len(result['suggestions']), 3)
        for suggestion in result['suggestions']:
            self.assertRegex(suggestion, r'^IVAN\d{4}$')
            self.assertFalse(AppUser.objects.filter(username__iexact=suggestion).exists())

    def test_own_username_is_available_and_current(self):
        result = self.check('Heidi')
        self.assertEqual((result['available'], result['current']), (True, True))

    def test_invalid_usernames(self):
        for username in ('a@b', 'has space', '', 'x' * 151):
            result = self.check(username)
            self.assertEqual((result['available'], result['reason']), (False, 'invalid'), username)

    def test_needs_authentication(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get('/api/v1/me/username/check/?username=x').status_code, (401, 403))

    def test_is_rate_limited(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {'username_check': '2/minute'}):
            codes = [self.client.get('/api/v1/me/username/check/?username=x').status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


class ChangeUsernameTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.signup('judy@example.com')  # verified, so it can log in again later
        self.user = AppUser.objects.get(email='judy@example.com')
        AppUser.objects.create_user(email='mallory@example.com', password=PASSWORD)
        self.client.force_authenticate(self.user)

    def change(self, data):
        return self.client.post('/api/v1/me/username/', data, format='json')

    def test_change_with_password_then_log_in_with_it(self):
        response = self.change({'username': 'judy.new', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['username'], 'judy.new')
        self.assertTrue(CRUDLog.objects.filter(user=self.user, operation='username_changed').exists())

        self.client.force_authenticate(None)
        login = self.post('/login', {'username': 'judy.new', 'password': PASSWORD})
        self.assertEqual(login.status_code, 200, login.content)

    def test_wrong_or_missing_password(self):
        for data in ({'username': 'judy.new', 'password': 'nope'}, {'username': 'judy.new'}):
            response = self.change(data)
            self.assertEqual(response.status_code, 400)
            self.assertIn('password', response.json())
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'judy')

    def test_taken_username(self):
        response = self.change({'username': 'Mallory', 'password': PASSWORD})
        self.assertEqual(response.status_code, 400)
        self.assertIn('username', response.json())

    def test_invalid_username(self):
        response = self.change({'username': 'judy@home', 'password': PASSWORD})
        self.assertEqual(response.status_code, 400)
        self.assertIn('username', response.json())

    def test_changing_case_of_own_username(self):
        response = self.change({'username': 'Judy', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['username'], 'Judy')

    def test_account_without_password_needs_none(self):
        self.user.set_unusable_password()
        self.user.save()
        self.assertFalse(self.client.get('/api/v1/me/').json()['has_password'])
        response = self.change({'username': 'judy.google'})
        self.assertEqual(response.status_code, 200, response.content)

    def test_profile_patch_cannot_change_username(self):
        self.client.patch('/api/v1/me/', {'username': 'sneaky'}, format='json')
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'judy')


class AvatarTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.user = AppUser.objects.create_user(email='kim@example.com', password=PASSWORD)
        self.client.force_authenticate(self.user)

    def upload(self, content, name='me.png', content_type='image/png'):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post('/api/v1/me/avatar/',
                                    {'file': SimpleUploadedFile(name, content, content_type=content_type)},
                                    format='multipart')

    def stored_image(self):
        self.user.refresh_from_db()
        return Image.open(self.user.profile_picture.path)

    def test_upload_is_shrunk_to_a_small_jpeg_without_metadata(self):
        original = png_bytes()
        response = self.upload(original)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertRegex(response.json()['avatar_url'], r'^/api/v1/me/avatar/\?v=\w+$')
        image = self.stored_image()
        self.assertEqual((image.format, image.size), ('JPEG', (512, 512)))
        self.assertFalse(image.getexif())
        self.assertLess(self.user.profile_picture.size, len(original))
        self.assertTrue(CRUDLog.objects.filter(user=self.user, operation='avatar_changed').exists())

    def test_small_picture_is_not_enlarged(self):
        self.upload(png_bytes((200, 200), 'RGB'))
        self.assertEqual(self.stored_image().size, (200, 200))

    def test_non_square_picture_is_centre_cropped(self):
        self.upload(png_bytes((1200, 600), 'RGB'))
        self.assertEqual(self.stored_image().size, (512, 512))

    def test_file_that_is_not_a_picture_is_rejected(self):
        response = self.upload(b'not really a png', name='fake.png')
        self.assertEqual(response.status_code, 400)
        self.assertIn('file', response.json())

    def test_unsupported_format_is_rejected(self):
        out = BytesIO()
        Image.new('RGB', (50, 50)).save(out, 'GIF')
        self.assertEqual(self.upload(out.getvalue(), name='a.gif', content_type='image/gif').status_code, 400)

    def test_file_over_5mb_is_rejected(self):
        response = self.upload(png_bytes() + b'0' * (5 * 1024 * 1024))
        self.assertEqual(response.status_code, 400)

    def test_replacing_deletes_the_old_file(self):
        first_url = self.upload(png_bytes()).json()['avatar_url']
        self.user.refresh_from_db()
        first_path = self.user.profile_picture.path
        second_url = self.upload(png_bytes((800, 800))).json()['avatar_url']
        self.assertNotEqual(first_url, second_url)
        self.assertFalse(os.path.exists(first_path))

    def test_remove(self):
        self.upload(png_bytes())
        self.user.refresh_from_db()
        path = self.user.profile_picture.path
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete('/api/v1/me/avatar/').status_code, 204)
        self.assertIsNone(self.client.get('/api/v1/me/').json()['avatar_url'])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.client.get('/api/v1/me/avatar/').status_code, 404)

    def test_served_only_to_the_signed_in_owner(self):
        self.upload(png_bytes())
        response = self.client.get('/api/v1/me/avatar/')
        self.assertEqual((response.status_code, response['Content-Type']), (200, 'image/jpeg'))
        self.client.force_authenticate(None)
        self.assertIn(self.client.get('/api/v1/me/avatar/').status_code, (401, 403))


class PasswordChangeExtraTests(AuthTestCase):
    def change(self, token, data):
        return self.client.post('/_allauth/app/v1/account/password/change', data,
                                format='json', headers={'X-Session-Token': token})

    def test_wrong_current_password_is_rejected(self):
        token = self.signup()
        response = self.change(token, {'current_password': 'not-it-123', 'new_password': 'new-long-pass-456'})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(AppUser.objects.get(email='alice@example.com').check_password(PASSWORD))

    def test_change_is_audited(self):
        token = self.signup()
        self.change(token, {'current_password': PASSWORD, 'new_password': 'new-long-pass-456'})
        self.assertTrue(CRUDLog.objects.filter(operation='password_changed').exists())


GOOGLE_CLIENT_ID = 'spendroo-test.apps.googleusercontent.com'
# Stand-ins for Google's signing key and for somebody else's
GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@override_settings(SOCIALACCOUNT_PROVIDERS={'google': {'APPS': [{'client_id': GOOGLE_CLIENT_ID, 'secret': ''}]}})
class GoogleSignInTests(AuthTestCase):
    """
    The app sends the ID token Google gave it. Only the download of Google's
    public keys is faked: the signature, issuer, audience and expiry are checked for real.
    """
    def setUp(self):
        super().setUp()
        patcher = mock.patch(
            'allauth.socialaccount.internal.jwtkit.fetch_key',
            return_value=('RS256', GOOGLE_KEY.public_key()),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def id_token(self, email='grace@gmail.com', sub='google-uid-1', key=GOOGLE_KEY, **claims):
        now = int(time.time())
        payload = {
            'iss': 'https://accounts.google.com', 'aud': GOOGLE_CLIENT_ID, 'sub': sub,
            'email': email, 'email_verified': True, 'given_name': 'Grace', 'family_name': 'Hopper',
            'iat': now, 'exp': now + 3600,
        }
        payload.update(claims)
        return jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'test-key'})

    def google(self, id_token=None, client_id=GOOGLE_CLIENT_ID, **token):
        token = {'client_id': client_id, 'id_token': id_token or self.id_token(), **token}
        return self.post('/provider/token', {'provider': 'google', 'process': 'login', 'token': token})

    def me(self, response):
        token = response.json()['meta']['session_token']
        return self.client.get('/api/v1/me/', headers={'X-Session-Token': token})

    def test_new_user_is_signed_up_and_signed_in(self):
        response = self.google()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['meta']['is_authenticated'])

        me = self.me(response)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data['email'], 'grace@gmail.com')
        self.assertEqual(me.data['username'], 'grace')
        self.assertEqual(me.data['full_name'], 'Grace Hopper')
        self.assertFalse(me.data['has_password'])

        user = AppUser.objects.get(email='grace@gmail.com')
        self.assertTrue(EmailAddress.objects.get(user=user).verified)
        self.assertEqual(SocialAccount.objects.get(user=user).uid, 'google-uid-1')
        self.assertEqual(Workspace.objects.get(owner=user).kind, 'personal')
        self.assertEqual(len(mail.outbox), 0)  # nothing to verify: Google did

    def test_signing_in_again_finds_the_same_account(self):
        self.google()
        # The Google account is the same one even when its address changed
        response = self.google(self.id_token(email='grace.new@gmail.com'))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(AppUser.objects.count(), 1)
        self.assertEqual(self.me(response).data['email'], 'grace@gmail.com')

    def test_taken_username_gets_digits(self):
        AppUser.objects.create_user(email='grace@example.com', password=PASSWORD)
        self.google()
        self.assertRegex(AppUser.objects.get(email='grace@gmail.com').username, r'^grace\d{4}$')

    def test_same_email_as_a_password_account_signs_into_it(self):
        self.signup('grace@gmail.com')
        user = AppUser.objects.get(email='grace@gmail.com')

        response = self.google()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(AppUser.objects.count(), 1)
        self.assertEqual(self.me(response).data['id'], str(user.pk))
        self.assertTrue(self.me(response).data['has_password'])
        self.assertEqual(SocialAccount.objects.get(provider='google').user, user)
        self.assertTrue(CRUDLog.objects.filter(operation='google_connected').exists())

        # The password still works, and so does Google the next time
        self.assertEqual(self.post('/login', {'email': 'grace@gmail.com', 'password': PASSWORD}).status_code, 200)
        self.assertEqual(self.google().status_code, 200)

    def test_account_that_never_verified_the_email_loses_its_password(self):
        # Someone else may have signed up with this address and be waiting with the password
        self.post('/signup', {'email': 'grace@gmail.com', 'password': PASSWORD})
        self.client = APIClient()

        response = self.google()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(AppUser.objects.count(), 1)
        self.assertFalse(self.me(response).data['has_password'])
        # Google vouched for the address, so no code is asked for
        self.assertTrue(EmailAddress.objects.get(email='grace@gmail.com').verified)
        self.client = APIClient()
        self.assertEqual(self.post('/login', {'email': 'grace@gmail.com', 'password': PASSWORD}).status_code, 400)

    def test_email_google_has_not_verified_does_not_reach_an_existing_account(self):
        self.signup('grace@gmail.com')
        self.client = APIClient()
        cache.clear()  # the sign-up's own code counts against the email rate limit
        response = self.google(self.id_token(email_verified=False))
        # Answered like a new sign-up, so the address is not given away
        self.assertEqual(response.status_code, 401, response.content)
        self.assertFalse(response.json().get('meta', {}).get('is_authenticated', False))
        self.assertFalse(SocialAccount.objects.exists())
        # The owner is told, without a code that would let the caller in
        self.assertIn('already exists', mail.outbox[-1].body)

    def test_new_email_google_has_not_verified_needs_the_emailed_code(self):
        response = self.google(self.id_token(email_verified=False))
        self.assertEqual(response.status_code, 401, response.content)
        self.assertIn(self.me(response).status_code, (401, 403))
        token = response.json()['meta']['session_token']
        response = self.post('/email/verify', {'key': code_from_last_email()}, token=token)
        self.assertEqual(response.status_code, 200, response.content)

    def test_tokens_that_are_not_ours_are_refused(self):
        now = int(time.time())
        bad = {
            'made for another app': self.id_token(aud='someone-else.apps.googleusercontent.com'),
            'not from Google': self.id_token(iss='https://accounts.example.com'),
            'expired': self.id_token(iat=now - 7200, exp=now - 3600),
            'signed by another key': self.id_token(key=OTHER_KEY),
            'not a token': 'nonsense',
        }
        for why, id_token in bad.items():
            with self.subTest(why):
                response = self.google(id_token)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertNotIn('session_token', response.json().get('meta', {}))
        self.assertFalse(AppUser.objects.exists())

    def test_token_cannot_be_used_twice(self):
        id_token = self.id_token(jti='once-only')
        self.assertEqual(self.google(id_token).status_code, 200)
        self.client = APIClient()
        self.assertEqual(self.google(id_token).status_code, 400)

    def test_unknown_client_id_is_refused(self):
        response = self.google(client_id='unknown.apps.googleusercontent.com')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(AppUser.objects.exists())

    def test_id_token_is_required(self):
        response = self.post('/provider/token', {
            'provider': 'google', 'process': 'login', 'token': {'client_id': GOOGLE_CLIENT_ID},
        })
        self.assertEqual(response.status_code, 400, response.content)

    @override_settings(SOCIALACCOUNT_PROVIDERS={'google': {'APPS': []}})
    def test_refused_when_no_client_id_is_configured(self):
        self.assertEqual(self.google().status_code, 400)
        self.assertFalse(AppUser.objects.exists())

    def test_google_account_can_set_a_first_password(self):
        token = self.google().json()['meta']['session_token']
        response = self.client.post(
            '/_allauth/app/v1/account/password/change', {'new_password': 'first-long-pass-123'},
            format='json', headers={'X-Session-Token': token},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(CRUDLog.objects.filter(operation='password_set').exists())
        self.client = APIClient()
        login = self.post('/login', {'email': 'grace@gmail.com', 'password': 'first-long-pass-123'})
        self.assertEqual(login.status_code, 200, login.content)
        self.assertTrue(self.me(login).data['has_password'])
