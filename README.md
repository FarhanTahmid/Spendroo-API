# Spendroo API

Django 5.2 + Django REST Framework on PostgreSQL. One deployable plus a background worker.

## Apps

| App | Holds |
|---|---|
| `identity` | User model, profile endpoint. Authentication itself is django-allauth (headless). |
| `workspaces` | The tenant: `Workspace`, `Membership`, roles, row-level-security helpers. |
| `cashbook` | Cashbooks, entries, categories, payment methods, reports. |
| `ledger_personal` | Personal mode: accounts, categories, transactions (incl. transfers), budgets. |
| `sync` | Offline sync for the app: change log pull and idempotent mutation push. |
| `billing` | Plans, limits, subscriptions, promo codes and campaigns, run from the admin. See [docs/billing.md](docs/billing.md). |
| `notifications` | Email accounts, email queue backend and task. |
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
- `POST /api/v1/workspaces/` creates a business; an optional client `id` makes retries return the same workspace.
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
- **Secrets stored in the database** use `spendroo.fields.EncryptedTextField`.
- The **database role** must not be a PostgreSQL superuser in production; superusers skip row-level security.
