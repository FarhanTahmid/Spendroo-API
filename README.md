# Spendroo API

Django 5.2 + Django REST Framework on PostgreSQL. One deployable plus a background worker.

## Apps

| App | Holds |
|---|---|
| `identity` | User model, profile endpoint. Authentication itself is django-allauth (headless). |
| `workspaces` | The tenant: `Workspace`, `Membership`, roles, row-level-security helpers. |
| `cashbook` | Cashbooks, entries, categories, payment methods, reports. |
| `ledger_personal` | Personal mode: accounts, categories, transactions (incl. transfers and splits), budgets. |
| `reminders` | Recurring transactions and reminders: rules the app writes, the dates they fall due on, and what the user is told. A job every 15 minutes (`reminders/tasks.py`) does the work, so the worker must be running. |
| `sync` | Offline sync for the app: change log pull and idempotent mutation push. |
| `reports` | Reports to download as PDF or Excel: personal, one account, one cashbook, a whole business. |
| `billing` | Plans, limits, subscriptions, promo codes and campaigns, run from the admin. See [docs/billing.md](docs/billing.md). |
| `notifications` | Email accounts, email queue backend and task. Push: the phones a user is signed in on (`Device`), what they want to be told (`NotificationPreference`), and the sender (`push.py`, Firebase Cloud Messaging). After every reminders run, what the inbox holds is handed to the phones (`delivery.py`). |
| `audit` | Request logging, `CRUDLog`. |

## Run locally

```
python -m venv .venv && .venv/Scripts/activate      # Python 3.12
pip install -r requirements.txt
cp .env.example .env                                # fill in SECRET_KEY and database
python manage.py migrate
python manage.py runserver
python manage.py procrastinate worker               # background jobs, separate terminal
```

Or with Docker: `docker compose up --build`.

Tests: `python manage.py test`.

## Keeping the app's features running

### What must be running

| Process | Command | Without it |
|---|---|---|
| PostgreSQL | (service, or the `db` container) | Nothing works. Background jobs are queued in it too (Procrastinate, no Redis). |
| API | `python manage.py runserver 8200` (dev; the app's dev flavour calls `10.0.2.2:8200` from the emulator) or gunicorn in Docker | The app works offline but cannot sign in or sync. |
| Worker | `python manage.py procrastinate worker` | Requests still answer, but jobs only queue up. Recurring transactions are not recorded, reminders and budget alerts are not made or pushed, and email (verification codes, password resets) is not sent. |

With Docker Compose the `worker` service is the worker; check it is up with `docker compose ps`.

### What the worker does

| Job | When | Does |
|---|---|---|
| `reminders.tasks.run_reminders` | Every 15 minutes (cron `*/15`), one run at a time | Works out what falls due, records `auto` rules, fills the inbox (upcoming, due, overdue, reminder), then queues a push for each new item (`notifications/delivery.py`). |
| `reminders.tasks.evaluate_budgets` | 30 s after a sync push touches transactions or budgets, once per workspace per burst | Adds the "80% spent" and "over budget" alerts, each once per budget per month, then queues their pushes. |
| `notifications.tasks.send_push` | Per inbox item | Sends it to each of the owner's phones through Firebase, worded in each phone's language. Retried 5 times only when Firebase is unreachable; a phone Firebase says is gone is forgotten. |
| `notifications.tasks.send_email` | Per email | Sends through the SMTP account set in the admin for that purpose, else `EMAIL_DELIVERY_BACKEND`. Retried 5 times. |

A push is not sent during the user's quiet hours (it waits for them to end), for a kind they switched off, or when
the item is more than 36 hours old. The daily "did you spend anything today?" nudge is scheduled by the phone itself.

### Settings each feature needs (`.env`)

| Setting | Needed for |
|---|---|
| `SECRET_KEY`, database settings | Everything. |
| `FCM_CREDENTIALS_FILE` | Push notifications. Path of the Firebase service-account key, e.g. `secrets/firebase-spendroo-dev.json`. Empty: nothing is pushed, everything else works. |
| `WEB_OAUTH2_CLIENT_ID` (and the Android/iOS ones) | Google sign-in. The app sends ID tokens made for the web client. |
| `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`, or an SMTP account in the admin | Verification codes and password resets in production. In development mail is printed to the console. |
| `FIELD_ENCRYPTION_KEYS` | Production refuses to start without it. It encrypts secrets kept in the database (SMTP passwords). Never change it without keeping the old key in the list. |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `TRUSTED_PROXY_COUNT`, `ADMIN_URL` | Production behind a proxy or tunnel. |

The PDF reports need Pango on the machine (see below).

### The Firebase key (push)

The Firebase project is `spendroo-dev`. Get a key from Firebase console > Project settings > Service accounts >
Generate new private key, and save it as `secrets/firebase-spendroo-dev.json`. `secrets/` is git-ignored and
`.dockerignore`d; Docker Compose mounts it read-only into the web and worker containers. Restart runserver and the
worker after changing `.env`. Each machine (dev, staging, production) needs its own copy of the key and the setting.
If a key leaks, delete it in Google Cloud console > IAM > Service accounts > `firebase-adminsdk-…` > Keys, and make a
new one.

### Checks

```
python manage.py check                                   # settings and models load
curl "http://127.0.0.1:8200/health/?db=1"                # API and database answer

# Push is set up: the key reads and Google gives an access token
python manage.py shell -c "from notifications import push; push._access_token(push._credentials()); print(push.configured())"

# Push reaches a user's phones (same as the app's Settings > Notifications > "Send a test notification")
python manage.py shell -c "from notifications import delivery; from identity.models import AppUser; print(delivery.send_test(AppUser.objects.get(email='you@example.com').pk))"

# Run the reminders pass now instead of waiting for the quarter hour
python manage.py shell -c "from reminders.tasks import run_reminders; run_reminders(timestamp=0)"

python manage.py procrastinate healthchecks              # the worker can reach its tables
python manage.py test notifications reminders            # push, reminders and budget alerts
```

Jobs, failed ones included, are listed in the admin under Procrastinate > Procrastinate jobs.
The worker's log says when each job ran and how it ended.

### Other commands

```
python manage.py billing_seed                  # the default plans and features (safe to re-run)
python manage.py billing_grant <email> <plan> --days 30 --reason "beta tester"
python manage.py billing_explain <email>       # what a user is entitled to, and why
python manage.py seed_demo <email>             # ~2 months of demo data in a user's personal workspace
python manage.py log_status                    # request logging health
```

## API

- `/api/v1/` application endpoints. Schema at `/api/v1/schema/`, Swagger at `/api/v1/docs/`.
- `/_allauth/app/v1/` sign-up, login, logout, email verification, password reset, Google sign-in, MFA, sessions.
  Login returns `meta.session_token`; send it on every request as `X-Session-Token`.
  Log in with `{"email": ...}` or `{"username": ...}` plus `password` (one of the two, not both).
  Password change is `POST /_allauth/app/v1/account/password/change` with `current_password` and `new_password`.
  Google sign-in is `POST /_allauth/app/v1/auth/provider/token` with
  `{"provider": "google", "process": "login", "token": {"client_id": ..., "id_token": ...}}`.
  The server checks the token's signature, issuer, expiry and that it was made for one of the
  `*_OAUTH2_CLIENT_ID`s in `.env` (the app uses the web one). The first sign-in makes the account;
  a Google-verified email that already has an account signs into that account and links Google to it.
- `/api/v1/sync/pull/?workspace=<id>&since=<seq>` changes since a cursor, per table, tombstones included.
- `/api/v1/sync/push/` a batch of client mutations (`upsert`/`delete`), each with a client UUID so retries apply once.
- `GET /api/v1/ledger/accounts/<id>/transactions/?from&to&kind&category&q&cursor&limit` one personal account's transactions, newest first, a page at a time (`next_cursor`). Read-only; the app itself reads its synced copy.
- `GET /api/v1/ledger/accounts/<id>/summary/?from&to&kind&category&q&include=categories,series` what the same filter adds up to: cash in and out, the balance before and after the period, and optionally totals per category and the chart series.
  Upserts carry only the changed columns, so concurrent edits to different fields both survive.
  Sync has its own rate limit (`sync` scope), separate from the rest of the API.
- `/api/v1/me/` profile, plus `onboarded_at` and `primary_mode` so onboarding is asked once per account.
  `has_password` is false for Google-only accounts; `avatar_url` changes whenever the picture does.
- `GET /api/v1/me/username/check/?username=x` whether a username is free, with suggestions when it is taken.
- `POST /api/v1/me/username/` changes the username; needs `password` unless the account has none.
- `/api/v1/me/avatar/` `POST` (multipart `file`) sets the picture, `DELETE` removes it, `GET` serves it.
  The app crops it; the server checks it is a JPEG, PNG or WEBP of 5 MB or less and stores a 512px JPEG without metadata.
- `PUT /api/v1/me/devices/` `{token, platform, locale, timezone, app_version}` registers the phone for push (and sets the
  user's time zone); `POST /api/v1/me/devices/forget/` `{token}` takes it back before signing out.
- `GET`/`PATCH /api/v1/me/notification-settings/` which pushes the user wants, the hour reminders come at, quiet hours.
  `POST /api/v1/me/notification-settings/test/` sends one to the user's own phones and says how many it reached.
- `POST /api/v1/workspaces/` creates a business; an optional client `id` makes retries return the same workspace.
- `GET /api/v1/reports/personal/`, `reports/accounts/<id>/`, `reports/cashbooks/<id>/`, `reports/businesses/<id>/`
  `?file_type=pdf|xlsx&period=this_month|last_month|last_3_months|this_year|custom&from&to&lang=en|bn` a report as a file.
  A PDF of a preset period is for every plan; Excel (`reports.excel`) and a custom report (`reports.custom_range`) are plan
  flags, refused with 402. A custom report has chosen dates and may be narrowed with `&type=expense,income` (or
  `cash_in,cash_out`) and `&category=<id>,<id>,none`; narrowed, it shows money in, money out and net, without balances. `X-Report-Reference` is the number printed in the footer and kept in the admin under Report downloads.
- `/api/v1/billing/` the user's plan and what it gives, public plans, promo codes, offers. See [docs/billing.md](docs/billing.md).
- `/health/` liveness; `/health/?db=1` also checks the database.

## Rules the code relies on

- **One account per email.** The username defaults to the part of the email before the `@`;
  when that is taken (case-insensitively) it gets 4 random digits (`identity/usernames.py`).

- **Plans are data.** What a plan gives is edited in the admin and applies at once. Code asks
  `billing.gates` and never reads a plan's name. A refusal is HTTP 402 `plan_limit`, never 403
  (the app takes 403 for an ended session and wipes the device). Nothing is deleted for a plan:
  what is over a limit becomes read-only. Tests fail for a synced table or write view with no plan check declared.
- **Money** is an integer in minor units (`amount_minor`) plus an ISO 4217 `currency`. No floats.
- **Tenant data** lives in models that inherit `WorkspaceOwnedModel`. Each such table needs
  `workspaces.rls.enable_rls` in a migration, and each view on it needs `TenantScopedMixin`.
  Tests fail if a table is missed.
- **Synced tables** are listed in `sync/registry.py` and need `sync.sql.enable_sync` in a migration
  (a trigger stamps `server_seq`). Tests fail if a tenant table lacks it.
- **Ids** are UUIDv7 (`spendroo.ids.uuid7`); clients generate the same kind offline.
- **Deletes** are tombstones (`soft_delete()`), never SQL deletes, so clients can sync them.
  Deleting a cashbook tombstones its entries, categories, payment methods and grants too.
- **Budgets** are per expense category: one recurring (`month` empty) plus optional one-month overrides.
- **Transfers** are two transactions sharing `transfer_group_id`, moving money in opposite directions.
- **Edit history** is written by database triggers (django-pghistory) into `*Event` tables.
- **Reports** are built once as a `reports.document.Report` and rendered twice (`render/pdf.py`, `render/xlsx.py`),
  so the two files of one report cannot disagree. Amounts are never converted: each currency gets its own totals.
  The PDF needs Pango on the machine (`apt install libpango-1.0-0 libpangoft2-1.0-0`; on Windows see WeasyPrint's docs).
- **Secrets stored in the database** use `spendroo.fields.EncryptedTextField`.
- The **database role** must not be a PostgreSQL superuser in production; superusers skip row-level security.
