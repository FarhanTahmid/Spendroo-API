"""
Base Django settings for spendroo project.

"""
import os
import base64
import hashlib
from dotenv import load_dotenv
from pathlib import Path
from decouple import config
from django.urls import reverse_lazy

load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SECRET KEY
SECRET_KEY = os.environ.get('SECRET_KEY')

# Keys for spendroo.fields.EncryptedTextField, newest first. Generate one with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Without the variable (development only) a key is derived from SECRET_KEY.
FIELD_ENCRYPTION_KEYS = config(
    'FIELD_ENCRYPTION_KEYS',
    default='',
    cast=lambda v: [s.strip() for s in v.split(',') if s.strip()]
) or [base64.urlsafe_b64encode(hashlib.sha256((SECRET_KEY or '').encode()).digest()).decode()]

# Number of reverse proxies in front of the app that we run ourselves.
# X-Forwarded-For is ignored when this is 0.
TRUSTED_PROXY_COUNT = config('TRUSTED_PROXY_COUNT', default=0, cast=int)
ALLAUTH_TRUSTED_PROXY_COUNT = TRUSTED_PROXY_COUNT

# Admin lives off the default path in production
ADMIN_URL = config('ADMIN_URL', default='admin/')


def database_from_env(prefix):
    """Database settings from <PREFIX>_DATABASE_* environment variables"""
    return {
        'ENGINE': os.environ.get(f'{prefix}_DATABASE_ENGINE', 'django.db.backends.postgresql'),
        'NAME': os.environ.get(f'{prefix}_DATABASE_NAME'),
        'USER': os.environ.get(f'{prefix}_DATABASE_USER'),
        'PASSWORD': os.environ.get(f'{prefix}_DATABASE_PASSWORD'),
        'HOST': os.environ.get(f'{prefix}_DATABASE_HOST'),
        'PORT': os.environ.get(f'{prefix}_DATABASE_PORT'),
    }

# Application definition

DJANGO_APPS = [
    # The admin theme has to come before the admin it restyles
    'unfold',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
]

THIRD_PARTY_APPS=[
    'rest_framework',
    'corsheaders',
    'drf_spectacular',
    # Authentication
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'allauth.mfa',
    'allauth.headless',
    'allauth.usersessions',
    # Trigger-based edit history
    'pgtrigger',
    'pghistory',
    # Background jobs
    'procrastinate.contrib.django',
]

LOCAL_APPS=[
    'identity',
    'workspaces',
    'audit',
    'notifications',
    'cashbook',
    'ledger_personal',
    'reminders',
    'sync',
    'billing',
    'reports',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# Middlewares
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'allauth.usersessions.middleware.UserSessionsMiddleware',
    # Records who made each change in the history tables
    'pghistory.middleware.HistoryMiddleware',
    # Row-level security: requests see no tenant rows until a view activates a tenant
    'workspaces.tenancy.TenantContextMiddleware',
    # A request works out each user's plan once
    'billing.middleware.EntitlementsCacheMiddleware',
    # Logging middleware
    'audit.logger.middleware.LoggerMiddleware',
]

# Root URL configuration
ROOT_URLCONF = 'spendroo.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR,'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# Custom AUTH model
AUTH_USER_MODEL = 'identity.AppUser'

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
]

# django-allauth: Django is the only identity authority. The mobile app uses the
# headless API (/_allauth/app/v1/) and sends its session token in X-Session-Token.
HEADLESS_ONLY = True
HEADLESS_CLIENTS = ('app',)
ACCOUNT_ADAPTER = 'identity.adapters.AccountAdapter'
# One account per email. Log in with either; the username is made from the email
ACCOUNT_LOGIN_METHODS = {'email', 'username'}
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*']
ACCOUNT_EMAIL_VERIFICATION = 'mandatory'
# Codes typed into the app, no links: nothing to deep-link and no web frontend needed
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED = True
ACCOUNT_PASSWORD_RESET_BY_CODE_ENABLED = True
ACCOUNT_EMAIL_SUBJECT_PREFIX = '[Spendroo] '
MFA_SUPPORTED_TYPES = ['totp', 'recovery_codes']
MFA_TOTP_ISSUER = 'Spendroo'
USERSESSIONS_TRACK_ACTIVITY = True

# Google sign-in: one entry per platform client ID. The app sends the Google ID
# token to /_allauth/app/v1/auth/provider/token together with its client ID.
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'APPS': [
            {'client_id': client_id, 'secret': ''}
            for client_id in (
                os.environ.get('ANDROID_OAUTH2_CLIENT_ID'),
                os.environ.get('IOS_OAUTH2_CLIENT_ID'),
                os.environ.get('WEB_OAUTH2_CLIENT_ID'),
            ) if client_id
        ],
    }
}
# A Google-verified email that matches an existing account signs into it and links
# Google to it. allauth removes the password first when that account never verified
# its email, so whoever made it cannot get back in.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

# Push notifications: the path of the Firebase service-account key file (Firebase console,
# Project settings, Service accounts). Empty: nothing is pushed, everything else works.
FCM_CREDENTIALS_FILE = config('FCM_CREDENTIALS_FILE', default='')

# Email. EMAIL_BACKEND is set per environment; the worker delivers queued
# mail through EMAIL_DELIVERY_BACKEND unless an SMTP account is set up in the admin.
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='Spendroo <no-reply@localhost>')
EMAIL_DELIVERY_BACKEND = 'django.core.mail.backends.console.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='localhost')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)

# CORS Configuration
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3000,http://127.0.0.1:3000',
    cast=lambda v: [s.strip() for s in v.split(',')]
)
CORS_ALLOW_CREDENTIALS = True

# WSGI APPLICATION
WSGI_APPLICATION = 'spendroo.wsgi.application'

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
# Media files
MEDIA_ROOT = os.path.join(BASE_DIR, 'Media_Files/')
MEDIA_URL = "/media_files/"

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {
            'min_length': 8,
        }
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Dhaka'

USE_I18N = True

USE_TZ = True

# Resized Image Fields
DJANGORESIZED_DEFAULT_SIZE = [1920, 1080]
DJANGORESIZED_DEFAULT_SCALE = 0.8
DJANGORESIZED_DEFAULT_QUALITY = 85
DJANGORESIZED_DEFAULT_KEEP_META = True
DJANGORESIZED_DEFAULT_NORMALIZE_ROTATION = True

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Django REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'allauth.headless.contrib.rest_framework.authentication.XSessionTokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle'
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        'sync': '6000/hour',
        'username_check': '60/minute',
        'profile_sensitive': '10/hour',
        'push_test': '10/hour',
        # Low, so promo codes cannot be guessed
        'promo_redeem': '10/hour',
        'billing_keep': '30/hour',
        'billing_events': '300/hour',
        # Each one lays out a whole document
        'reports': '30/hour',
    },
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    # DRF's own, plus a note of every plan refusal for the admin dashboard
    'EXCEPTION_HANDLER': 'billing.api.handlers.exception_handler',
}

# Pretend purchases for development and tests (billing/providers/dev.py).
# Production settings refuse to start with this on.
BILLING_DEV_TOOLS = False


# Admin (django-unfold). The home page is the billing dashboard.
def _admin_link(name):
    return reverse_lazy(f'admin:{name}')


def _can(permission):
    return lambda request: request.user.has_perm(permission)


# The only web login is the admin's. Its themed form carries no destination when
# opened directly, and Django would then send staff to /accounts/profile/, which does not exist
LOGIN_REDIRECT_URL = reverse_lazy('admin:index')

UNFOLD = {
    'SITE_TITLE': 'Spendroo admin',
    'SITE_HEADER': 'Spendroo',
    'SITE_SUBHEADER': 'Control room',
    'SITE_SYMBOL': 'savings',
    'SHOW_VIEW_ON_SITE': False,
    'ENVIRONMENT': 'billing.admin.dashboard.environment',
    'DASHBOARD_CALLBACK': 'billing.admin.dashboard.dashboard_callback',
    'SIDEBAR': {
        'show_search': True,
        'show_all_applications': True,
        'navigation': [
            {
                'title': 'Overview',
                'items': [
                    {'title': 'Dashboard', 'icon': 'dashboard', 'link': _admin_link('index')},
                ],
            },
            {
                'title': 'Plans',
                'separator': True,
                'items': [
                    {'title': 'Plan matrix', 'icon': 'grid_on', 'link': _admin_link('billing_plan_matrix'),
                     'permission': _can('billing.view_plan')},
                    {'title': 'Plans', 'icon': 'workspace_premium', 'link': _admin_link('billing_plan_changelist'),
                     'permission': _can('billing.view_plan'),
                     # The matrix and the history sit under the same address and have entries of their own
                     'active': lambda request: request.path.startswith('/admin/billing/plan/')
                     and not request.path.startswith(('/admin/billing/plan/matrix/', '/admin/billing/plan/history/'))},
                    {'title': 'Features', 'icon': 'toggle_on', 'link': _admin_link('billing_feature_changelist'),
                     'permission': _can('billing.view_feature')},
                    {'title': 'Store products', 'icon': 'sell', 'link': _admin_link('billing_product_changelist'),
                     'permission': _can('billing.view_product')},
                ],
            },
            {
                'title': 'Customers',
                'separator': True,
                'items': [
                    {'title': 'Users', 'icon': 'group', 'link': _admin_link('identity_appuser_changelist'),
                     'permission': _can('identity.view_appuser')},
                    {'title': 'Subscriptions', 'icon': 'card_membership',
                     'link': _admin_link('billing_subscription_changelist'),
                     'permission': _can('billing.view_subscription')},
                    {'title': 'Overrides', 'icon': 'tune', 'link': _admin_link('billing_entitlementoverride_changelist'),
                     'permission': _can('billing.view_entitlementoverride')},
                    {'title': 'Workspaces', 'icon': 'store', 'link': _admin_link('workspaces_workspace_changelist'),
                     'permission': _can('workspaces.view_workspace')},
                ],
            },
            {
                'title': 'Recurring',
                'separator': True,
                'items': [
                    {'title': 'Rules', 'icon': 'event_repeat', 'link': _admin_link('reminders_rule_changelist'),
                     'permission': _can('reminders.view_rule')},
                    {'title': 'Occurrences', 'icon': 'event_available',
                     'link': _admin_link('reminders_occurrence_changelist'),
                     'permission': _can('reminders.view_occurrence')},
                    {'title': 'Inbox items', 'icon': 'notifications',
                     'link': _admin_link('reminders_inboxitem_changelist'),
                     'permission': _can('reminders.view_inboxitem')},
                    {'title': 'Devices', 'icon': 'smartphone',
                     'link': _admin_link('notifications_device_changelist'),
                     'permission': _can('notifications.view_device')},
                    {'title': 'Notification preferences', 'icon': 'notifications_active',
                     'link': _admin_link('notifications_notificationpreference_changelist'),
                     'permission': _can('notifications.view_notificationpreference')},
                ],
            },
            {
                'title': 'Offers',
                'separator': True,
                'items': [
                    {'title': 'Campaigns', 'icon': 'campaign', 'link': _admin_link('billing_campaign_changelist'),
                     'permission': _can('billing.view_campaign')},
                    {'title': 'Promo codes', 'icon': 'confirmation_number',
                     'link': _admin_link('billing_promocode_changelist'),
                     'permission': _can('billing.view_promocode')},
                ],
            },
            {
                'title': 'Activity',
                'separator': True,
                'items': [
                    {'title': 'Limit hits and upgrades', 'icon': 'trending_up',
                     'link': _admin_link('billing_funnelevent_changelist'),
                     'permission': _can('billing.view_funnelevent')},
                    {'title': 'Usage', 'icon': 'data_usage', 'link': _admin_link('billing_usagecounter_changelist'),
                     'permission': _can('billing.view_usagecounter')},
                    {'title': 'Report downloads', 'icon': 'download',
                     'link': _admin_link('reports_reportdownload_changelist'),
                     'permission': _can('reports.view_reportdownload')},
                    {'title': 'Store events', 'icon': 'receipt_long',
                     'link': _admin_link('billing_billingevent_changelist'),
                     'permission': _can('billing.view_billingevent')},
                    {'title': 'Plan changes', 'icon': 'history', 'link': _admin_link('billing_plan_history'),
                     'permission': _can('billing.view_plan')},
                ],
            },
            {
                'title': 'Settings',
                'separator': True,
                'items': [
                    {'title': 'Billing settings', 'icon': 'settings',
                     'link': _admin_link('billing_billingsettings_changelist'),
                     'permission': _can('billing.view_billingsettings')},
                ],
            },
        ],
    },
}

# OpenAPI schema
SPECTACULAR_SETTINGS = {
    'TITLE': 'Spendroo API',
    'DESCRIPTION': 'Money amounts are integers in minor units (paisa, cents) with an ISO 4217 currency code.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SCHEMA_PATH_PREFIX': r'/api/v[0-9]+',
}


# Logging configuration
LOGS_DIR = BASE_DIR/'logs'
LOGS_DIR.mkdir(exist_ok=True)

# Set directory permissions for web server access (Linux/Unix systems)
try:
    os.chmod(LOGS_DIR, 0o755)  # rwxr-xr-x permissions
except (OSError, AttributeError):
    # Handle Windows or permission errors gracefully
    pass

# Django Logging Configuration Dictionary
# This follows Django's standard logging configuration format
LOGGING = {
    # Configuration version - always set to 1 for current Django versions
    'version': 1,
    
    # Don't disable existing loggers - allows third-party packages to log
    'disable_existing_loggers': False,
    
    # ==========================================================================
    # LOG MESSAGE FORMATTERS
    # ==========================================================================
    # Formatters define how log messages appear in output files and console
    
    'formatters': {
        # Detailed formatter for production file logs
        # Provides comprehensive information for troubleshooting and analysis
        'detailed': {
            'format': '{asctime} | {levelname:8} | {name} | {message}',
            'style': '{',  # Use new-style string formatting
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        
        # Simple formatter for console output during development
        # Reduces clutter while providing essential information
        'simple': {
            'format': '{asctime} | {levelname} | {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        
        # JSON-compatible formatter for log aggregation systems
        # Includes file path and line number for debugging
        'json': {
            'format': '{asctime} | {levelname} | {name} | {message} | {pathname}:{lineno}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    
    # ==========================================================================
    # LOG OUTPUT HANDLERS
    # ==========================================================================
    # Handlers determine where log messages are sent (files, console, etc.)
    
    'handlers': {
        # Console handler for development and debugging
        # Shows log messages in terminal/command prompt
        'console': {
            'level': 'INFO',  # Only show INFO level and above in console
            'class': 'logging.StreamHandler',  # Standard output stream
            'formatter': 'simple',  # Use simple format for readability
        },
        
        # HTTP request/response logs with automatic rotation
        # Captures all web traffic for performance and usage analysis
        'requests_file': {
            'level': 'INFO',  # Log all requests (INFO level and above)
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'requests.log',
            'maxBytes': 10 * 1024 * 1024,  # 10MB maximum file size
            'backupCount': 10,  # Keep 10 backup files (100MB total retention)
            'formatter': 'detailed',  # Use detailed format for analysis
            'encoding': 'utf-8',  # Support international characters
        },
        
        # Error and exception logs with extended retention
        # Critical for debugging and system monitoring
        'errors_file': {
            'level': 'WARNING',  # Log warnings, errors, and critical messages
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'errors.log',
            'maxBytes': 5 * 1024 * 1024,  # 5MB maximum file size
            'backupCount': 20,  # Keep 20 backup files (errors are important!)
            'formatter': 'detailed',  # Detailed format for debugging
            'encoding': 'utf-8',
        },
        
        # Authentication and security event logs
        # Essential for compliance and security monitoring
        'auth_file': {
            'level': 'INFO',  # Log all authentication events
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'auth.log',
            'maxBytes': 5 * 1024 * 1024,  # 5MB maximum file size
            'backupCount': 15,  # Keep 15 backup files for audit trail
            'formatter': 'detailed',  # Detailed format for security analysis
            'encoding': 'utf-8',
        },
        
        # General application logs for system events
        # Catches miscellaneous application events and system messages
        'general_file': {
            'level': 'INFO',  # Log general information and above
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'general.log',
            'maxBytes': 10 * 1024 * 1024,  # 10MB maximum file size
            'backupCount': 5,  # Keep 5 backup files (moderate retention)
            'formatter': 'detailed',  # Detailed format for analysis
            'encoding': 'utf-8',
        },
    },
    
    # ==========================================================================
    # SPECIALIZED LOGGERS
    # ==========================================================================
    # Loggers categorize and route different types of log messages
    
    'loggers': {
        # HTTP request/response logger
        # Used by ERPLoggerMiddleware for all web traffic logging
        'spendroo.requests': {
            'handlers': ['requests_file', 'console'],  # File + console output
            'level': 'INFO',  # Log INFO level and above
            'propagate': False,  # Don't pass to parent loggers (avoid duplicates)
        },
        
        # Error and exception logger  
        # Used for unhandled exceptions and application errors
        'spendroo.errors': {
            'handlers': ['errors_file', 'console'],  # File + console output
            'level': 'WARNING',  # Log WARNING level and above
            'propagate': False,  # Independent error handling
        },
        
        # Authentication and security event logger
        # Used for login/logout events and security monitoring
        'spendroo.auth': {
            'handlers': ['auth_file', 'console'],  # File + console output
            'level': 'INFO',  # Log all auth events
            'propagate': False,  # Separate from general logging
        },
        
        # Django's default logger for framework messages
        # Handles Django internal logging (database, cache, etc.)
        'django': {
            'handlers': ['general_file', 'console'],  # File + console output
            'level': 'INFO',  # Framework information and above
            'propagate': False,  # Don't duplicate Django's internal logging
        },
        
        # The PDF library narrates every step of every report at INFO
        'weasyprint': {'level': 'WARNING'},
        'fontTools': {'level': 'WARNING'},

        # Root logger - catches any unrouted log messages
        # Fallback for any logging not handled by specific loggers
        '': {
            'handlers': ['general_file', 'console'],  # Default output
            'level': 'INFO',  # General information level
        },
    },
}

# Maximum log message length (prevents memory issues with very long messages)
LOGGING_MAX_MESSAGE_LENGTH = 8192  # 8KB maximum per log message

# Log file permissions (Unix/Linux systems)
LOGGING_FILE_PERMISSIONS = 0o644  # rw-r--r-- (readable by owner and group)

# Timezone for log timestamps (should match Django's TIME_ZONE setting)
LOGGING_TIMEZONE = 'Asia/Dhaka'