# Backend Guide — Reading the code, file by file

You said you don't know Django yet, so this is written like a tour: what
Django is, what each file does, why it exists, and how money flows from
an HTTP request all the way down to a Postgres `SELECT FOR UPDATE`.

If you read this top-to-bottom you'll come out understanding:

1. The shape of a Django project.
2. What every single file in `backend/` is for.
3. The exact request lifecycle for `POST /api/v1/payouts`.
4. Every env variable in `.env` and why it's there.

---

## Part 0 — A 60-second crash course on Django

Django is a Python web framework. It splits a project into two layers:

- **The "project"** — global wiring (settings, root URL routing, WSGI/ASGI
  entry points, Celery setup). Lives in `backend/config/`.
- **One or more "apps"** — feature modules with their own models, views,
  URLs, tests, and migrations. Lives in `backend/payouts/`.

You can think of the project as the "main()" of your service, and apps
as plugins that the project mounts.

A few Django words you'll see:

| Word | What it is |
|---|---|
| **model** | A Python class that maps to a Postgres table. Subclasses `django.db.models.Model`. |
| **migration** | A file that describes a schema change (CREATE TABLE, ADD COLUMN, …). Auto-generated from models. |
| **manager / queryset** | `Model.objects.filter(...)` — a lazy SQL builder. Returns rows when you iterate. |
| **view** | A function that takes an HTTP request and returns an HTTP response. |
| **serializer** | DRF (Django REST Framework) class that turns model rows ↔ JSON. |
| **URLconf** | The `urlpatterns` list that maps URL paths → views. |
| **management command** | A CLI subcommand of `manage.py`, e.g. `python manage.py seed`. |
| **Celery task** | A function executed by a background worker, not by the web server. |

That's the whole vocabulary you need for this project.

---

## Part 1 — How to run the backend (commands, in order)

You'll need **four terminals**. Run all commands from `backend/`.

### Terminal 1 — One-time setup

```bash
cd backend

# 1. Make a virtualenv (an isolated Python install for this project)
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install Python dependencies listed in requirements.txt
pip install -r requirements.txt

# 3. Copy the env template and fill it in
cp .env.example .env
#    -> open .env in an editor
#    -> paste your Supabase SESSION connection string into DATABASE_URL
#       (port 5432, NOT 6543)
#    -> set DJANGO_SECRET_KEY to any long random string
#       you can generate one with:
#       python -c "import secrets; print(secrets.token_urlsafe(64))"

# 4. Make sure Redis is running locally
#    macOS:    brew services start redis
#    Docker:   docker run -d -p 6379:6379 --name redis redis:7
#    Test it:  redis-cli ping     # should print "PONG"

# 5. Generate the initial migration for the `payouts` app.
#    This reads payouts/models.py and writes payouts/migrations/0001_initial.py.
#    Only needed once, on a fresh clone where the migration file isn't checked in.
python manage.py makemigrations payouts

# 6. Apply all migrations to Supabase (Django built-ins + our payouts schema)
python manage.py migrate

# 7. Seed three merchants, bank accounts, and credit history
python manage.py seed
```

After step 6 you should see something like:

```
merchant created: Bright Fox Studio (xxxxxxxx-xxxx-...)
  -> seeded 3 credits (11_250_000 paise total)
merchant created: Sahil Mehta (Freelancer) (...)
  -> seeded 2 credits (1_250_000 paise total)
merchant created: Indigo Labs (...)
  -> seeded 1 credits (5_000_000 paise total)
seed complete
```

### Terminal 2 — Web server

```bash
cd backend
source .venv/bin/activate
python manage.py runserver 0.0.0.0:8000
```

This is your API. Test it: `curl http://localhost:8000/health/`
should return `{"status": "ok"}`.

### Terminal 3 — Celery worker (does the actual payout settlement)

```bash
cd backend
source .venv/bin/activate
celery -A config worker -l info
```

This is the background process that picks up `process_payout` tasks
and simulates the bank settlement. **Without this running, your
payouts will sit in `pending` forever.**

### Terminal 4 — Celery Beat (the cron-like scheduler)

```bash
cd backend
source .venv/bin/activate
celery -A config beat -l info
```

Beat is responsible for the two periodic jobs:

- `sweep_stuck_payouts` every 10 seconds — moves stuck payouts to
  `queued_for_retry` and re-enqueues them with exponential backoff.
- `cleanup_idempotency_keys` every hour — deletes keys older than 24h.

### Optional Terminal 5 — Tests

```bash
cd backend
source .venv/bin/activate
python manage.py test payouts
```

Note: the concurrency test needs a real Postgres. If `manage.py test`
errors with "permission denied to create database" against Supabase,
either:

- run it against a local Postgres, OR
- pre-create a `payouts_test` schema in Supabase and run
  `python manage.py test --keepdb`.

---

## Part 2 — Every file in `backend/` explained

Project layout:

```
backend/
├── manage.py                      Django CLI entry point
├── requirements.txt               Python dependencies
├── .env                           Local config (you create this from .env.example)
├── .env.example                   Template of all required env vars
│
├── config/                        ← THE DJANGO "PROJECT" (global wiring)
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   ├── celery.py
│   ├── wsgi.py
│   └── asgi.py
│
└── payouts/                       ← THE DJANGO "APP" (the feature itself)
    ├── __init__.py
    ├── apps.py
    ├── models.py
    ├── serializers.py
    ├── views.py
    ├── urls.py
    ├── services.py
    ├── state_machine.py
    ├── idempotency.py
    ├── tasks.py
    ├── exceptions.py
    ├── admin.py
    │
    ├── migrations/                ← Auto-generated DB schema files
    │   └── __init__.py
    │
    ├── management/commands/
    │   ├── __init__.py
    │   └── seed.py                ← `python manage.py seed`
    │
    └── tests/
        ├── __init__.py
        ├── test_concurrency.py
        ├── test_idempotency.py
        └── test_state_machine.py
```

---

### `manage.py`

**What it is:** the CLI for everything Django-related. Auto-generated by
`django-admin startproject`. Pretty much never edited.

**What you use it for:**

- `python manage.py runserver` — start the dev web server
- `python manage.py migrate` — apply migrations to the DB
- `python manage.py makemigrations` — generate a migration file from
  changes in `models.py`
- `python manage.py seed` — our custom seed command
- `python manage.py test` — run the test suite
- `python manage.py shell` — open a Python REPL with Django loaded

How it works internally: it reads `DJANGO_SETTINGS_MODULE` (which we
hard-code to `config.settings`) and dispatches to the right command.

---

### `requirements.txt`

**What it is:** a flat list of Python packages, one per line, with
pinned versions. `pip install -r requirements.txt` reads this.

| Package | Why we need it |
|---|---|
| `Django==5.0.6` | The framework itself. |
| `djangorestframework==3.15.2` | REST Framework — turns Django views into JSON APIs. We use it for the `@api_view` decorator, `Response`, exception handler. |
| `django-cors-headers==4.4.0` | Lets the React frontend (running on `:5173`) call the Django API (running on `:8000`) without browser CORS errors. |
| `psycopg2-binary==2.9.9` | The Postgres driver. Django talks to Supabase through this. |
| `dj-database-url==2.2.0` | Tiny helper that parses a `postgres://...` URL into the dict shape Django wants in `DATABASES`. Saves us 30 lines of boilerplate. |
| `celery==5.4.0` | Distributed task queue. Runs `process_payout` and the sweeper outside the request/response cycle. |
| `redis==5.0.7` | Python client for Redis. Celery uses Redis as its message broker. |
| `django-celery-beat==2.6.0` | Stores the Beat schedule (the cron rules) in the database, so you can edit them at runtime via Django admin. |
| `python-dotenv==1.0.1` | Lets `settings.py` read `.env` files in development. |
| `gunicorn==22.0.0` | The production WSGI web server. Dev uses `runserver`; prod uses `gunicorn config.wsgi`. |
| `whitenoise==6.7.0` | Serves Django's collected static files in production without needing nginx. |

---

### `.env.example` and `.env`

**`.env.example`** is committed to git. It documents every variable.

**`.env`** is local-only (in `.gitignore`). You create it by copying
`.env.example` and filling in real values. `python-dotenv` loads it at
process start so the values appear in `os.environ`.

#### Every variable, what it does, why we need it

| Variable | What it controls | Why it's there |
|---|---|---|
| `DJANGO_SECRET_KEY` | Django's master key for cryptographic signing (sessions, CSRF tokens, password reset links). | Django requires it. Must be long, random, and **never** committed. |
| `DJANGO_DEBUG` | `True` shows full error pages with stack traces; `False` returns 500s with no internals. | `True` in dev, `False` in prod. Leaking debug info in prod is a security hole. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated list of hostnames Django will accept requests for (e.g. `playto-api.onrender.com`). | Prevents Host-header attacks in production. In dev we just use `localhost,127.0.0.1`. |
| `CORS_ALLOWED_ORIGINS` | Comma-separated list of origins (e.g. `http://localhost:5173`) the frontend may live at. | Tells the browser "yes, the React app on port 5173 is allowed to call this API." |
| `DATABASE_URL` | The full Postgres connection string. | The single source of truth for which DB to talk to. We point it at Supabase's session connection (port 5432). |
| `CELERY_BROKER_URL` | Redis URL Celery uses to **send** task messages. | This is how the web server enqueues a task that the worker picks up. |
| `CELERY_RESULT_BACKEND` | Redis URL Celery uses to **store** task results. | Optional but lets us inspect results if we ever need to. |
| `PAYOUT_SUCCESS_RATE` | Probability (0–1) the simulated bank settles successfully. Default `0.7`. | Lets tests pin the simulation deterministically (e.g. `1.0` to force success). |
| `PAYOUT_FAILURE_RATE` | Probability (0–1) the simulated bank declines. Default `0.2`. | Same. The remaining `0.1` is "stuck", which the sweeper handles. |
| `PAYOUT_STUCK_TIMEOUT_SECONDS` | How long (seconds) a payout can sit in `processing` before the sweeper considers it stuck. Default `30`. | Tunable for tests; tighter timeout = faster retry. |
| `PAYOUT_MAX_RETRIES` | How many times the sweeper retries before terminally failing. Default `3`. | Caps the worst-case time a payout can take. |
| `IDEMPOTENCY_TTL_HOURS` | Lifetime of stored idempotency keys. Default `24`. | The challenge spec required 24h. Older keys are reaped by Beat. |

---

## Part 3 — `config/` (the project)

### `config/__init__.py`

```python
from .celery import app as celery_app
__all__ = ("celery_app",)
```

**What it does:** when Python imports the `config` package, it eagerly
loads our Celery app object. This is what lets `celery -A config worker`
find the app — `-A config` literally means "import the `config` package
and look for `celery_app`."

**Django concept:** every directory with `__init__.py` is a Python
package. Django apps and projects are just packages.

---

### `config/settings.py`

The biggest file in the project. Django reads it at startup. Highlights:

- `INSTALLED_APPS` — list of apps Django should load. We have
  `payouts` (our app), `rest_framework` (DRF), `corsheaders`,
  `django_celery_beat`, plus Django's built-in admin/auth/etc.
- `MIDDLEWARE` — ordered list of "middlewares" each request passes
  through. CORS is first so it can short-circuit OPTIONS preflights;
  WhiteNoise serves static files; Django's auth/csrf middleware runs
  for the admin pages.
- `ROOT_URLCONF = "config.urls"` — Django uses this to find the URL
  routing rules for the whole site.
- `DATABASES` — built from `DATABASE_URL` via `dj_database_url`. We
  set `conn_max_age=0` because Supabase sits behind a pooler and
  long-held connections behave badly there.
- `REST_FRAMEWORK` — DRF's own settings. We pin our custom exception
  handler so domain errors come out as nice JSON.
- `CELERY_*` — Celery reads these from Django settings (it shares the
  config with the rest of the app).
- `PAYOUT_*` and `IDEMPOTENCY_TTL_HOURS` — our application-specific
  knobs, read from env so tests can override them.
- `LOGGING` — sends Python `logging` output to the console with
  timestamps. The `payouts` logger is what you see when a payout is
  requested or completed.

**Django concept:** `settings.py` is just a Python module. Django imports
it and reads its module-level names. No magic.

---

### `config/urls.py`

```python
urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", healthcheck),
    path("api/v1/", include("payouts.urls")),
]
```

**What it does:** the top-level URL router.

- `/admin/` → Django's auto-generated admin panel.
- `/health/` → a tiny JSON OK endpoint for monitoring.
- `/api/v1/...` → delegated to `payouts/urls.py`, which has all the
  real API routes.

`include()` is how you nest URL routers across apps. It's how big Django
projects stay organized.

---

### `config/celery.py`

Boots the Celery app and registers the Beat schedule (cron rules).

Important line: `app.config_from_object("django.conf:settings", namespace="CELERY")`
means Celery reads any setting starting with `CELERY_` from Django's
settings module. So `CELERY_BROKER_URL` in `settings.py` becomes
Celery's broker URL automatically.

`app.autodiscover_tasks()` scans every installed app for a `tasks.py`
file and registers tasks declared with `@shared_task` — that's how
`payouts/tasks.py` gets wired in.

---

### `config/wsgi.py` and `config/asgi.py`

Production entry points.

- **WSGI** is the synchronous web-server interface. Gunicorn uses it.
- **ASGI** is the async equivalent. Used if you ever want WebSockets.

In dev you don't touch either — `runserver` uses WSGI internally.

---

## Part 4 — `payouts/` (the feature)

This is where every business rule lives.

### `payouts/__init__.py` and `payouts/apps.py`

`__init__.py` is empty — it just marks the directory as a package.

`apps.py` declares the app's metadata:

```python
class PayoutsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payouts"
```

`default_auto_field` says "primary keys default to BigInteger" — fine
for any growing table. We mostly use UUIDs anyway.

---

### `payouts/models.py` — the database schema, in Python

Five models. Each becomes a Postgres table.

#### `Merchant`
- One row per merchant.
- The lock target for serialization. In `request_payout` we do
  `Merchant.objects.select_for_update().get(...)` to take a row-level
  write lock so concurrent payout requests for the same merchant
  serialize at the database.

#### `BankAccount`
- A merchant can have many bank accounts.
- We never store the full account number — only `account_number_last4`.

#### `LedgerEntry` — the ledger
The most important model. Append-only, never updated, never deleted.

- `bucket` ∈ `{available, held}` — separates the spendable balance
  from money held for in-flight payouts.
- `amount_paise` is **signed**. Positive adds to the bucket; negative
  subtracts.
- `entry_type` is the human label (`credit`, `payout_hold`,
  `payout_debit`, `payout_release`).

Balance is always a SQL aggregate over this table — never stored on the
merchant. See `services._bucket_sum`.

#### `Payout`
- The state machine row. `state` ∈
  `{pending, processing, queued_for_retry, completed, failed}`.
- Only the state-machine helper (`state_machine.transition`) is
  allowed to mutate `state`.
- `processing_started_at` is the clock the sweeper uses to detect
  stuck rows.
- `retry_count` increments each time the sweeper retries.

#### `IdempotencyKey`
- Unique on `(merchant_id, key)`.
- Stores the request fingerprint (sha256 of the body) so we can detect
  "same key, different payload" client bugs.
- Stores the cached response so a replay returns identical bytes.

**Django concept:** changing `models.py` is not enough — you also have
to run `python manage.py makemigrations payouts` to generate a
migration file, then `python manage.py migrate` to apply it.

---

### `payouts/migrations/`

Auto-generated files that record schema changes. The first one
(`0001_initial.py`) creates all five tables. **You don't write these
by hand** — Django generates them when you run
`python manage.py makemigrations`.

In CI/prod, `python manage.py migrate` reads the migrations table in
the DB, sees which haven't been applied, and applies them in order.

`__init__.py` is required for Django to treat the folder as a package.

---

### `payouts/exceptions.py` — domain errors

Five exception classes plus a DRF exception handler. Why?

Without this, raising `InsufficientBalance("...")` deep in
`services.py` would bubble up as a generic 500. With the handler
registered in `settings.REST_FRAMEWORK["EXCEPTION_HANDLER"]`, it comes
back as a clean 409 with:

```json
{ "error": { "code": "insufficient_balance", "message": "...",
             "details": { "available_paise": 100, "requested_paise": 200 } } }
```

Each exception sets its own HTTP status and error code.

---

### `payouts/state_machine.py` — the rules of state

`LEGAL_TRANSITIONS` is a dict from current-state to a frozenset of
allowed next-states. The `transition()` function:

1. `SELECT FOR UPDATE` on the payout row (re-reads the *committed*
   state from the DB).
2. Validates the requested transition against `LEGAL_TRANSITIONS`.
3. Updates the row inside the same transaction.

Because the lock is held until commit, two workers can't both observe
"processing" and both write "completed" — the second one will read the
new state and raise `InvalidStateTransition`.

---

### `payouts/services.py` — the business logic

Every money-moving operation lives here. Functions:

- `get_balance(merchant_id)` — read-only aggregate.
- `_bucket_sum(merchant_id, bucket)` — the single SQL query that
  computes balance. Uses `Coalesce(Sum, 0)` so an empty result returns
  `0` instead of `None`.
- `credit_merchant(...)` — append a `+amount` row to the available
  bucket.
- `request_payout(...)` — the heart of the engine. Locks the merchant
  row, recomputes balance under the lock, creates the Payout, writes
  the paired `-X available / +X held` ledger entries. All in one
  `@transaction.atomic`.
- `complete_payout(...)` — transitions to `completed` and writes
  `-X held` (money is gone).
- `fail_payout(...)` — transitions to `failed` and writes the paired
  `-X held / +X available` refund. State change and refund are in the
  same transaction — no intermediate state where the row is failed but
  the refund hasn't happened yet.

**Django concept:** `@transaction.atomic` opens a database transaction
for the duration of the function. If any statement raises, everything
rolls back. If the function returns normally, everything commits
together.

---

### `payouts/idempotency.py` — exactly-once semantics for POST /payouts

`with_idempotency(merchant_id, key, payload, handler)` is the wrapper.
The flow:

1. Try to `INSERT` a row with status `in_progress` inside a savepoint.
2. If `IntegrityError` (key already exists), catch it and switch to
   `SELECT FOR UPDATE` on the existing row.
   - The DB blocks until the other transaction (if any) commits.
   - Once we have the lock, read its status:
     - `completed` → return the cached response.
     - Different fingerprint → `IdempotencyConflict`.
     - Older than TTL → expired; delete and proceed as fresh.
3. If we are the "owner" of the key, run `handler()` (which creates
   the payout), then store the response on the record before
   committing.

Net effect: same request body + same key = exactly one Payout, no
matter how many times the client retries.

---

### `payouts/serializers.py` — JSON shape definitions

DRF serializers. They do two things:

- **Outbound:** turn a `Payout` ORM object into a JSON dict.
- **Inbound:** validate an incoming JSON body against a schema (e.g.
  `amount_paise` is a positive integer, `bank_account_id` is a UUID).

`PayoutCreateSerializer` is for input validation only. The actual
creation happens in `services.request_payout`.

---

### `payouts/views.py` — the HTTP layer

Every endpoint is a thin function decorated with `@api_view(...)`.
Pattern: parse input → resolve merchant → delegate to services →
serialize response. Money rules live in services, not here.

Notable views:

- `merchant_balance` — reads `services.get_balance` and returns it.
- `payouts` (POST) — validates input, calls `with_idempotency` with a
  handler that calls `request_payout`, sets `Idempotent-Replayed`
  header, enqueues the Celery `process_payout` task **only on the
  non-replayed path** (replays must not produce a second settlement).

---

### `payouts/urls.py` — route table for this app

Simple list of `path("...", view_function)` rules. Mounted under
`/api/v1/` by `config/urls.py`.

---

### `payouts/admin.py` — Django admin panel registration

Visit `http://localhost:8000/admin/` (after creating a superuser with
`python manage.py createsuperuser`) to browse merchants, ledger
entries, payouts, and idempotency keys in a CRUD UI. Useful for
debugging during development.

---

### `payouts/tasks.py` — background workers

Celery tasks decorated with `@shared_task`. Three of them:

- `process_payout(payout_id)` — claims the row by transitioning to
  `processing`, simulates the bank call, then transitions to
  `completed` / `failed` / leaves it in `processing` (= "stuck").
- `sweep_stuck_payouts()` — Beat fires this every 10s. Finds rows in
  `processing` whose `processing_started_at` is older than the
  configured timeout. For each, either bumps the retry count and
  transitions to `queued_for_retry` (then re-enqueues `process_payout`
  with exponential backoff), or fails+refunds at max retries.
- `cleanup_idempotency_keys()` — Beat fires this hourly. Deletes
  expired keys.

Every state change in the worker goes through `state_machine.transition`,
so the strict forward-only invariant holds even on the retry path.

---

### `payouts/management/commands/seed.py`

A custom CLI command. Inherits from `django.core.management.base.BaseCommand`.
The `handle()` method runs when you type `python manage.py seed`.

Idempotent — it matches on email, so re-running won't duplicate
merchants or credits.

**Django concept:** every directory under `management/commands/` named
`*.py` becomes a `manage.py <name>` subcommand automatically.

---

### `payouts/tests/`

Three test files:

- `test_concurrency.py` — uses `TransactionTestCase` (each test runs
  outside a wrapping transaction so threads can see each other's
  commits). Two threads each request a 60-paise payout against a 100-
  paise balance. Asserts exactly one succeeds and balance invariants
  hold.
- `test_idempotency.py` — same key + same payload returns the same
  payout id and creates only one row. Same key + different payload
  raises `IdempotencyConflict`.
- `test_state_machine.py` — pure unit tests for `assert_legal` plus
  end-to-end transitions including the retry round-trip
  `processing → queued_for_retry → processing → completed`.

`__init__.py` files in `tests/`, `management/`, `management/commands/`
are required so Python recognizes them as packages.

---

## Part 5 — End-to-end request flow (the money path)

When the React UI submits a payout, here's what happens, file by file:

```
[Browser]
  POST http://localhost:8000/api/v1/payouts/
  Headers: X-Merchant-Id: <uuid>, Idempotency-Key: <uuid>
  Body:    { "amount_paise": 50000, "bank_account_id": "<uuid>" }
        │
        ▼
[Django middleware]   corsheaders → security → auth → ...
        │
        ▼
[config/urls.py]      "/api/v1/" → payouts/urls.py
[payouts/urls.py]     "/payouts/" → views.payouts
        │
        ▼
[payouts/views.py: payouts()]
   → reads X-Merchant-Id, looks up Merchant
   → reads Idempotency-Key
   → validates body via PayoutCreateSerializer
   → calls idempotency.with_idempotency(...)
        │
        ▼
[payouts/idempotency.py: with_idempotency]
   → tries INSERT into idempotency_keys (status=in_progress)
   → if it conflicts: SELECT FOR UPDATE the existing row
   → if cached: return cached response (REPLAY — no further work)
   → otherwise: call handler()
        │
        ▼
[handler in views.py]
   → calls services.request_payout(...)
        │
        ▼
[payouts/services.py: request_payout]
   @transaction.atomic
   → SELECT * FROM merchants WHERE id=? FOR UPDATE   ← row lock
   → SUM(amount_paise) FROM ledger_entries           ← balance under lock
   → if available < amount: raise InsufficientBalance
   → INSERT INTO payouts (..., state='pending')
   → INSERT INTO ledger_entries (-X available, +X held)
   → COMMIT  (lock released)
        │
        ▼
[back in views.py]
   → serializes the new Payout to JSON
   → writes the response into the IdempotencyKey row (so future replays
     return identical bytes)
   → sends Celery task: process_payout(payout_id)
        │
        ▼
HTTP 201 Created, headers: Idempotent-Replayed: false
        │
        ▼
[Celery worker, separate process — payouts/tasks.py: process_payout]
   → state_machine.transition(payout, 'processing')   ← row lock + validate
   → sleep + random outcome (70% success / 20% fail / 10% stuck)
   → success:  services.complete_payout (writes -X held)
   → failure:  services.fail_payout      (writes -X held + +X available)
   → stuck:    leave it; sweeper will pick it up
        │
        ▼
[Beat every 10s — payouts/tasks.py: sweep_stuck_payouts]
   → SELECT processing payouts older than 30s
   → for each: state_machine.transition(payout, 'queued_for_retry')
   → re-enqueue process_payout with countdown = 2^retry_count
   → at max retries: services.fail_payout (+ refund)
```

Read this once with the source open in another window — it's the
single most useful thing you can do to learn the codebase.

---

## Part 6 — When something breaks, where to look

| Symptom | Most likely place |
|---|---|
| `relation "merchants" does not exist` | You forgot `python manage.py migrate`. |
| `DATABASE_URL is required` | `.env` is missing or not being loaded — make sure you ran from `backend/` and the file is named exactly `.env`. |
| Payout sits in `pending` forever | Celery worker is not running. Start Terminal 3. |
| Stuck payouts never retry | Celery beat is not running. Start Terminal 4. |
| Frontend gets CORS error | `CORS_ALLOWED_ORIGINS` doesn't include your frontend origin. Add `http://localhost:5173`. |
| `psycopg2` install fails on macOS | Use `psycopg2-binary` (already in our requirements) and make sure `pip` is updated: `pip install -U pip`. |
| `could not translate host name "db.<ref>.supabase.co"` | You're using Supabase's **direct connection** (IPv6-only, fails DNS on macOS). Switch to the **Session Pooler** URL: `postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres`. |
| Concurrency test creates two payouts | You're on SQLite. PostgreSQL is required for `SELECT FOR UPDATE`. |
| Tests fail with "permission denied to create database" | Supabase blocks `CREATE DATABASE`. Run tests against local Postgres or pre-create `test_playto` and use `--keepdb`. |
| Tests pass but teardown errors with `database "test_..." is being accessed by other users` | The Supabase pooler holds idle connections, blocking `DROP DATABASE`. **The tests passed** — the error is only on cleanup. Use `python manage.py test payouts --keepdb` to skip the drop and reuse the test DB on subsequent runs. |

---

## Part 7 — Useful Django commands you'll reach for

```bash
# Open a Python shell with all your models importable
python manage.py shell

# Inside the shell:
>>> from payouts.models import Merchant, Payout
>>> Merchant.objects.count()
>>> Payout.objects.filter(state="failed").values("id", "failure_reason")

# Create a Django admin login (visit /admin/ to use it)
python manage.py createsuperuser

# Generate a migration after editing models.py
python manage.py makemigrations payouts

# See what SQL a migration will run, without applying it
python manage.py sqlmigrate payouts 0001

# Show all URL patterns
python manage.py show_urls   # (requires django-extensions; optional)
```

That's the whole backend. If you read this guide, then read the source
in this order — `models.py` → `state_machine.py` → `services.py` →
`idempotency.py` → `views.py` → `tasks.py` — you'll have a complete
mental model of how money moves through the system.
