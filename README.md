# Playto Payout Engine

A minimal but production-shaped payout engine for the Playto Founding
Engineer challenge. International customers credit a merchant; the
merchant withdraws to an Indian bank account; the engine in the middle
holds the invariants that a real money-moving system has to hold —
ledger correctness, race-free concurrency, idempotent writes, and a
strict state machine.

```
┌────────────┐     POST /api/v1/payouts      ┌───────────────┐
│ React UI   │ ────────────────────────────▶ │ Django + DRF  │
│ (Vite)     │ ◀──── balance, history ────── │ + idempotency │
└────────────┘                               └───────┬───────┘
                                                     │ enqueue
                                                     ▼
                                             ┌───────────────┐
                                             │ Celery worker │
                                             │  + Beat       │
                                             └───────┬───────┘
                                                     │ row locks
                                                     ▼
                                             ┌───────────────┐
                                             │ PostgreSQL    │
                                             │ (Supabase)    │
                                             └───────────────┘
```

## Stack

- **Backend** — Django 5 + DRF, PostgreSQL (Supabase), Celery + Redis
- **Frontend** — React 18 + Vite + Tailwind
- **Money** — `BigIntegerField` paise everywhere. No floats. No decimals.

## Repo layout

```
backend/        Django project: payouts/ app + config/
frontend/       Vite + React dashboard
EXPLAINER.md    The five EXPLAINER answers (read this second)
README.md       Setup + run instructions (you're here)
```

---

## Backend setup

### 1. Database (Supabase)

Supabase Dashboard → your project → **Project Settings → Database →
Connection string**. Pick the **Session Pooler** option. It looks like:

```
postgresql://postgres.<PROJECT-REF>:<PASSWORD>@aws-0-<REGION>.pooler.supabase.com:5432/postgres
```

Note the username has a `.<project-ref>` suffix — it's not just `postgres`.

> ⚠️ **Do NOT use the other two options Supabase shows you:**
>
> - **Direct connection** (`db.<ref>.supabase.co:5432`) is IPv6-only on
>   the free tier; macOS and many ISPs can't resolve it and you'll get
>   `could not translate host name`.
> - **Transaction pooler** (port `6543`) does not preserve session state
>   across statements, which silently breaks `SELECT FOR UPDATE` row
>   locks. This engine relies on those locks for correctness.
>
> If your password contains `@ # / : ? & +` or spaces, percent-encode
> them in the URL.

### 2. Local environment

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env, paste DATABASE_URL from Supabase, set DJANGO_SECRET_KEY
```

Make sure Redis is running locally (`brew services start redis` on macOS,
or `docker run -p 6379:6379 redis:7`).

### 3. Migrate, seed, run

```bash
python manage.py makemigrations payouts   # one-time: generates 0001_initial.py
python manage.py migrate                  # applies all migrations to Supabase
python manage.py seed                     # 3 merchants + bank accounts + credits
python manage.py runserver 0.0.0.0:8000   # Django API on :8000
```

Then in two more terminals:

```bash
# Celery worker
celery -A config worker -l info

# Celery beat (retry sweeper + idempotency cleanup)
celery -A config beat -l info
```

### 4. Run tests

```bash
# Concurrency test must hit real Postgres (SQLite's SELECT FOR UPDATE is a no-op).
python manage.py test payouts
```

> Notes when running tests against Supabase:
>
> - Use `python manage.py test payouts --keepdb` after the first run.
>   The Supabase pooler holds idle connections, which blocks Django's
>   teardown `DROP DATABASE` (you'll see "database is being accessed by
>   other users"). The tests themselves pass — `--keepdb` just skips
>   the failing drop and reuses the test DB.
> - If `CREATE DATABASE` is blocked at all (paid Supabase tiers
>   sometimes restrict it), pre-create a `test_playto` database in
>   the SQL editor and the same `--keepdb` flow works.

---

## Frontend setup

```bash
cd frontend
npm install
cp .env.example .env   # VITE_API_BASE_URL=http://localhost:8000
npm run dev            # http://localhost:5173
```

The dashboard lets you switch between seeded merchants, see the available /
held / total balance, request a payout to one of the merchant's bank
accounts, and watch payouts move through `pending → processing →
completed/failed` via 3-second polling.

---

## API surface

All endpoints live under `/api/v1/`. The frontend identifies the merchant
via the `X-Merchant-Id` header (in production this would be an
authenticated session).

| Method | Path                                | Notes                                    |
|-------:|-------------------------------------|------------------------------------------|
| GET    | `/merchants/`                       | List seeded merchants (UI bootstrapping) |
| GET    | `/merchants/me/balance/`            | `available_paise`, `held_paise`, `total` |
| GET    | `/merchants/me/bank-accounts/`      | Eligible payout destinations             |
| GET    | `/merchants/me/ledger/`             | Latest 100 entries                       |
| GET    | `/payouts/`                         | Payout history                           |
| POST   | `/payouts/`                         | Requires `Idempotency-Key` header        |
| GET    | `/payouts/<uuid>/`                  | Single payout                            |

`POST /payouts/` body:

```json
{ "amount_paise": 50000, "bank_account_id": "<uuid>" }
```

Response (`201 Created`):

```json
{
  "id": "…",
  "amount_paise": 50000,
  "state": "pending",
  "bank_account": { "ifsc": "HDFC0000123", "account_number_last4": "4321", … },
  "created_at": "…"
}
```

Replays return the same body and a `Idempotent-Replayed: true` header.

---

## Money model in one diagram

```
LedgerEntry rows
┌──────────┬──────────────────┬──────────┐
│ bucket   │ entry_type       │ amount   │
├──────────┼──────────────────┼──────────┤
│ available│ credit           │ +1000    │  customer paid in
│ available│ payout_hold      │  -600    │  (paired)  payout requested
│ held     │ payout_hold      │  +600    │  (paired)
│ held     │ payout_debit     │  -600    │  payout completed (money gone)
│ held     │ payout_release   │  -600    │  (paired)  payout failed
│ available│ payout_release   │  +600    │  (paired)  refund
└──────────┴──────────────────┴──────────┘

available_balance = SUM(amount) WHERE bucket='available'
held_balance      = SUM(amount) WHERE bucket='held'
```

Both are SQL aggregates (`Coalesce(Sum(...), 0)`) — never Python loops over
fetched rows. See `backend/payouts/services.py:_bucket_sum`.

---

## Deployment notes

- **Render / Railway** work out of the box. One web service for Django
  (`gunicorn config.wsgi`), one worker for Celery (`celery -A config
  worker`), one worker for Beat (`celery -A config beat`), and a Redis
  add-on. Point `DATABASE_URL` at the Supabase **session** URI.
- The frontend is a static Vite build; deploy `frontend/dist` anywhere
  (Vercel, Netlify, Render Static).

---

## What's deliberately not here

- **Auth** — `X-Merchant-Id` header in lieu of a real session. Out of scope.
- **Customer-payment ingestion** — credits are seeded; the prompt said
  the customer flow isn't required.
- **Webhook delivery, audit log, event sourcing** — bonus items skipped to
  keep the scope honest.

See [EXPLAINER.md](./EXPLAINER.md) for the five mandatory answers.
