# Billing: plans, limits and offers

The `billing` app decides what each user may do. Plans and their numbers are
data, edited in the admin; code only asks "may this user do this?".

Three rules hold everything together:

- **The server decides.** The app shows what the server tells it and stops a
  user early as a courtesy. Every write is checked again on the server.
- **Nothing is deleted for a plan.** A user over a limit keeps everything;
  what is over the limit becomes read-only.
- **A refusal is HTTP 402 with code `plan_limit`, never 403.** The app treats a
  403 as an ended session and wipes the device.

## Words

| Word | Meaning |
|---|---|
| Feature | One thing a plan can limit, named by a key such as `personal.accounts`. A `flag` is on or off, a `limit` is how many at one time, a `quota` is how much per month. |
| Plan | A set of feature values. Exactly one plan is the default; a user with no subscription is on it, with no row written for them. |
| Subscription | A reason a user is on a plan for a period: a purchase, a grant, a promo code, a campaign, a trial. The highest-ranked live one wins. |
| Override | A change to one feature for one user: "at least" (the default), "add" or "set exactly". |
| Entitlements | The answer for one user: default plan, then the best subscription, then overrides. A business uses its **owner's** entitlements. |

## Gate a new feature

1. Add a key in `billing/catalog/keys.py`.
2. Give it a value per plan in `billing/catalog/defaults.py`, then run
   `python manage.py billing_seed` (it adds what is missing and leaves admin
   edits alone).
3. Call a gate where the feature is used:

```python
from billing import gates
from billing.catalog.keys import F

gates.require(workspace, F.REPORTS_EXCEL)                 # a flag
gates.check_limit(workspace, F.BUSINESS_CASHBOOKS)        # room for one more
with gates.consume(user, F.AI_CREDITS, 3, key=request_id):  # a quota; given back if the block raises
    ...
gates.assert_row_writable(workspace, F.PERSONAL_ACCOUNTS, account_id)  # locked by a downgrade?
```

A `limit` that counts rows also needs a rule in `billing/rules.py` saying
which rows count. A synced table lists its checks in `plan_gates` in
`sync/registry.py`.

Tests fail when a step is missed: every feature key must be classified, every
synced table must declare `plan_gates` (or `NO_GATES`), and every view that
takes writes must set `billing_gate` to `gates.GATED` or
`gates.exempt('why')`. See `billing/tests/test_structure.py`.

In the app, add the key to `F` in `lib/core/entitlements/entitlements.dart`
and ask `PlanGuard` in the repository before the write. Add the key to
`shownFeatures` in `lib/features/plan/plan_text.dart` once the app has the
feature: the upgrade screen lists only what the installed app can do.

## The admin

Open `/admin/`. It starts on a dashboard: users per plan, subscriptions ending
soon, which limits users run into, running campaigns.

| To do this | Go to |
|---|---|
| Change what a plan gives | Plans → **Plan matrix**. One grid, every plan against every feature. Saving applies to everyone on the plan at once. |
| Add a plan | Plans → Add, or **Duplicate** an existing one. A copy starts hidden. |
| Undo a change to a plan | Plans → a plan → **Restore from history**. It shows what would change first. |
| See or change one user | Users → a user → **Billing**. Shows the plan, why each value is what it is, usage and locked items. Grant a plan, add an override, add credits, revoke. |
| Put many users on a plan | Users → select → "Grant a plan to the selected users" (up to 500). For more, use a campaign. |
| Hand out codes | Promo codes → **Generate codes**, then select and "Export as CSV". |
| Run an offer | Campaigns → Add. Choose who sees it, what it gives and where the app shows it. "Auto apply" gives the plan to everyone in the audience for as long as it runs. |
| Stop refusing anyone | Billing settings → enforcement mode. "Log only" allows everything and records what would have been refused; the app stops refusing too. |

Two staff groups are created by `migrate`: **Billing admin** edits the
catalog, campaigns and codes; **Support** can look at everything, grant a
plan for up to 30 days, add overrides and credits, and revoke. Put a staff
user in one of them. Every grant, override and code needs a reason, and every
change is kept in history with who made it.

## After a downgrade

When a user holds more than their plan allows, they choose which items stay
editable (`PUT /api/v1/billing/keep/<feature>/`). Until they choose, the
oldest ones stay editable. A choice can be changed again after the cooldown in
Billing settings (30 days by default), and at once after a plan change.
Deleting or archiving is always allowed, so a user can get back under a limit.
The demo business is not counted and never locked.

## Endpoints

All under `/api/v1/billing/`, all for the signed-in user only.

| Endpoint | Purpose |
|---|---|
| `GET entitlements/` | Plan, feature values, usage, locks, ads and offers. Also on `/api/v1/me/` as `entitlements`. |
| `GET plans/` | Public plans and what each gives, for the upgrade screen. |
| `POST promo/redeem/` | Redeem a code. Throttled; one message for every kind of bad code. |
| `POST offers/<slug>/claim/` | Take what a campaign gives. |
| `GET keep/`, `PUT keep/<feature>/` | What is over a limit, and the choice of what to keep. |
| `POST events/` | The app reports an upgrade screen seen or tapped. |
| `POST dev/simulate/` | Development only: act out a purchase, renewal, cancel, expiry or refund. Not routed in production, and production refuses to start with `BILLING_DEV_TOOLS` set. |

Each sync pull carries `billing_stamp`. It moves when a plan is edited or the
user's own plan changes, and the app then fetches its entitlements again.
A synced change the plan does not allow comes back rejected with code
`plan_limit` and `error.meta` (`feature`, `reason`, `limit`, `current`,
`plan`, `upgrade_to`).

Entitlements are the caller's own plan, but a business's team seats come
from its owner's. `GET /api/v1/workspaces/<id>/seats/` gives any member the
owner's numbers for the seat meter: `limit` (null when unlimited), `used`
(members other than the owner plus invitations waiting), `members`,
`pending`, `enforced` and `upgrade_to`.

A business's edit history is the owner's alone, behind `history.audit_trail`
on the owner's plan (402 `plan_limit` without it, 403 `owner_only` for anyone
else). `GET /api/v1/workspaces/<id>/history/` reads the trigger-kept history
of entries, cashbooks, categories, payment methods, cashbook access and
members, newest first, with what changed in each. It can be narrowed by
`cashbook`, `person`, `entry`, `from` and `to`, and pages by `cursor`.
See `audit/history.py`.

## Commands

```
python manage.py billing_seed [--check | --dry-run | --force]   # the default catalog
python manage.py billing_grant <email> <plan> [--days N] [--reason "..."]
python manage.py billing_explain <email>                        # each value and where it came from
python manage.py billing_export [file]                          # the catalog as JSON
python manage.py billing_import <file>                          # load it on another server
```

## In tests

```python
from billing.testing import grant_plan, set_limit, set_flag, set_mode

grant_plan(user, 'business')
set_limit(user, F.PERSONAL_ACCOUNTS, 6)
set_mode('log_only')
```

## Try it by hand

1. `python manage.py migrate`, then open `/admin/`: three plans, and the matrix.
2. Sign up in the dev app. Settings → Plan shows Free. Add a fifth account: the
   upgrade sheet opens and nothing is queued.
3. In the matrix, raise Free accounts to 6. After the app's next sync the
   fifth account is allowed, with no release.
4. On the user's Billing page grant Plus for a day, create more than Free
   allows, then revoke it. The app opens "Choose what to keep"; the items not
   chosen are locked, not gone.
5. Generate a promo code, redeem it in the app, redeem it again: refused.
6. Start a campaign for Free users with the home banner: it shows with its
   end date, and can be claimed once.
7. `python manage.py billing_explain <email>` matches what the app shows.

## Bill attachments

Three keys hold bills (photos and PDFs on business entries) to the plan, all
counted on the business owner:

| Key | Free | Plus | Business |
|---|---|---|---|
| `storage.attachments_mb`: total kept | 10 MB | 1 GB | 10 GB |
| `storage.attachment_file_mb`: one file | 2 MB | 20 MB | 50 MB |
| `storage.attachment_pdf`: PDFs too | off | on | on |

Every bill goes through `cashbook/bills.py`. It checks the file by its bytes
(`cashbook/bill_files.py`): photos are decoded and written out again, so
EXIF/GPS and anything glued on are dropped. PDFs are read with pypdf and
refused if they hold scripts, launch actions, embedded files or a second
file after `%%EOF`. Nothing over 50 MB is read on any plan. Removing a bill,
its entry or its book gives the space back at once. The file is deleted after
the commit, and `manage.py purge_bill_files` (also a daily task) sweeps up the
rest.

Files sit in the `bills` storage (`STORAGES` in settings), never under
`MEDIA_ROOT`. Locally that is `Private_Files/bills` (`BILL_FILES_ROOT`). An
object store takes over with `BILL_STORAGE_BACKEND` and
`BILL_STORAGE_OPTIONS` (JSON). Phones fetch a file through a link good for
five minutes (`…/bills/<id>/link/`, then `/api/v1/bills/file/<token>/`), and
the file endpoint checks again that the person may still see the book. In
production, let the reverse proxy take bodies of 51 MB
(`client_max_body_size`).

## Not built yet

- Store purchases. `billing/providers/base.py` is the interface a store
  provider implements; `providers/dev.py` stands in for it. Purchases must
  only ever be accepted from the store's server, never from what the app says.
- Ads, AI features, statement upload. Their keys and quotas exist and are
  metered; nothing uses them yet.
- The device cap (`devices.max`) is shown but not enforced at login.
- Campaigns cannot target by country: users have no country.
- Features that run only on the phone can be unlocked by someone who modifies
  the app. Anything that reaches the server stays enforced.
