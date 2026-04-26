# EXPLAINER.md

The five questions, answered short and specific.

---

## 1. The Ledger — balance query and why credits/debits are modelled this way

**Query** (Django ORM in `backend/payouts/services.py`):

```python
def _bucket_sum(merchant_id, bucket: str) -> int:
    return LedgerEntry.objects.filter(
        merchant_id=merchant_id, bucket=bucket
    ).aggregate(
        total=Coalesce(Sum("amount_paise"), Value(0))
    )["total"]
```

Which translates to:

```sql
SELECT COALESCE(SUM(amount_paise), 0)
FROM ledger_entries
WHERE merchant_id = %s AND bucket = %s;
```

`available_paise = bucket_sum('available')`, `held_paise = bucket_sum('held')`.

**Design choice.** I kept the ledger single-table with a signed
`amount_paise` and a `bucket` column instead of a two-column
`credit_paise` / `debit_paise` shape. Two reasons:

1. **A payout's lifecycle moves money *between* buckets** — request: −600
   from available, +600 in held; success: −600 from held; failure: −600
   from held + 600 back into available. With a `bucket` column the same
   `LedgerEntry` shape covers all four cases. Each transition writes one
   or two rows in a single `transaction.atomic` block, so
   `SUM(available)+SUM(held)` is conserved at every commit boundary.
2. **Balance is computed by the database, not Python.** I never iterate
   `for e in entries: total += e.amount`. That sounds obvious but it's
   the part AI assistants get wrong (see Q5). Doing the sum in Postgres
   means a payout under a row lock can recompute the balance in the same
   transaction without race-prone read-then-act gaps.

I considered double-entry (every transaction emits a paired
debit+credit row across two accounts). Honest answer: it's overkill for
one merchant + a held bucket, and the paired writes I already do
(`-X available`, `+X held`) preserve the same sum-conservation invariant
without the conceptual overhead.

---

## 2. The Lock — code that prevents two concurrent payouts from overdrawing

**Code** (`backend/payouts/services.py:request_payout`):

```python
@transaction.atomic
def request_payout(*, merchant_id, amount_paise, bank_account_id) -> Payout:
    ...
    # 1. Lock the merchant row. Linearization point.
    merchant = Merchant.objects.select_for_update().get(pk=merchant_id)

    ...

    # 2. Recompute balance UNDER the lock.
    available = _locked_available_paise(merchant.id)
    if available < amount_paise:
        raise InsufficientBalance(...)

    # 3. Create the payout + paired ledger entries inside the same tx.
    payout = Payout.objects.create(...)
    LedgerEntry.objects.bulk_create([
        LedgerEntry(... bucket='available', amount_paise=-amount_paise ...),
        LedgerEntry(... bucket='held',      amount_paise=+amount_paise ...),
    ])
```

**The primitive.** PostgreSQL row-level write locks (`SELECT ... FOR
UPDATE`). When two transactions hit `Merchant.objects.select_for_update()`
on the same row, the second one **blocks at the database** until the
first commits or rolls back. Once it acquires the lock, it re-runs its
balance aggregate — which now reflects the first transaction's hold
entries — and rejects with `InsufficientBalance`.

**Why not a Python lock or a balance column with `F('balance') -
amount`?**

- A process-local `threading.Lock` covers one Python process, not a
  multi-worker deployment.
- `UPDATE merchants SET balance = balance - %s WHERE balance >= %s` would
  work *if* we stored balance on the merchant row. We don't — balance is
  derived from the ledger. So we lock the row that's the natural
  serialization point (the merchant), recompute under the lock, and write
  the ledger entries. The lock is per-merchant: two different merchants
  pay out in parallel without contention.

The concurrency test in `backend/payouts/tests/test_concurrency.py`
proves this against a real Postgres: two threads each try to take 60p
out of a 100p balance simultaneously; exactly one succeeds.

---

## 3. The Idempotency — how keys are tracked, and what happens to an in-flight replay

**Storage.** Table `idempotency_keys` with a unique constraint on
`(merchant_id, key)` and a fingerprint of the request body
(`SHA-256(json.dumps(payload, sort_keys=True))`).

**Lookup flow** (`backend/payouts/idempotency.py:with_idempotency`):

1. Try to **INSERT** a new row in status `in_progress` inside a
   savepoint. If it succeeds, this caller owns the key — run the handler.
2. If the insert raises `IntegrityError`, the key already exists. Run
   `SELECT ... FOR UPDATE` on it.
   - If status is `completed`: return the cached `(status_code,
     response_body)`. No re-execution.
   - If the request fingerprint differs from the cached one: raise
     `IdempotencyConflict` (`422`) — same key, different body is a
     client bug.
   - If status is `in_progress` / `failed`: the previous attempt
     already finished by the time we got the lock; the handler can
     safely re-run.
3. Records older than `IDEMPOTENCY_TTL_HOURS` (24h) are treated as
   expired on read and reaped hourly by
   `payouts.tasks.cleanup_idempotency_keys`.

**The "first request still in flight" case — exactly the question
asked.** Suppose request A hasn't committed yet when request B arrives
with the same key:

- A is inside its `transaction.atomic`; it has just inserted the
  `idempotency_keys` row, which holds an implicit row-level write lock
  until commit.
- B's `SELECT ... FOR UPDATE` on the same row **blocks** at the
  database. It does not retry, does not return early, does not produce a
  duplicate payout. It just waits.
- When A commits, its row becomes visible with status `completed` and
  the cached response body. B's lock acquires, B reads the cached
  response, B returns it.
- If A rolls back instead of committing, B sees no row at all and
  proceeds as the canonical executor.

Net effect: **exactly one payout is created**, no matter how the two
requests interleave. The frontend reflects this — `Idempotent-Replayed:
true` header on the second response, and the dashboard only shows one
row.

The DRF view also forwards the Celery `process_payout` task only on the
non-replayed path (`backend/payouts/views.py:_create_payout`), so a
replay never produces a second settlement attempt.

---

## 4. The State Machine — where illegal transitions get blocked

`backend/payouts/state_machine.py`:

```python
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    Payout.STATE_PENDING:          frozenset({Payout.STATE_PROCESSING}),
    Payout.STATE_PROCESSING:       frozenset({
        Payout.STATE_COMPLETED,
        Payout.STATE_FAILED,
        Payout.STATE_QUEUED_FOR_RETRY,
    }),
    Payout.STATE_QUEUED_FOR_RETRY: frozenset({
        Payout.STATE_PROCESSING,
        Payout.STATE_FAILED,
    }),
    Payout.STATE_COMPLETED:        frozenset(),     # terminal
    Payout.STATE_FAILED:           frozenset(),     # terminal
}

def assert_legal(from_state: str, to_state: str) -> None:
    allowed = LEGAL_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidStateTransition(
            f"Illegal payout state transition {from_state!r} -> {to_state!r}",
            from_state=from_state, to_state=to_state,
        )

@transaction.atomic
def transition(payout_id, to_state, *, failure_reason: str = "") -> Payout:
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    assert_legal(payout.state, to_state)              # <- the check
    payout.state = to_state
    ...
    payout.save(update_fields=[...])
    return payout
```

The full graph is **strictly forward-only**:

```
pending ──▶ processing ──▶ completed (terminal)
                │   ▲
                │   │
                ▼   │
           queued_for_retry ──▶ failed (terminal)
                │
                └─── (also: processing ──▶ failed directly)
```

`failed → completed` is blocked because `LEGAL_TRANSITIONS[STATE_FAILED]
= frozenset()` — every target raises. Same for `completed → *`. And
`processing → pending` (the obvious-but-wrong way to model retry) is
blocked too: `STATE_PENDING` is *not* in
`LEGAL_TRANSITIONS[STATE_PROCESSING]`. The only forward exit from
`processing` for a stuck row is into `queued_for_retry`; the retry
worker then moves it back into `processing` via the same state-machine
helper.

**Three design choices worth noting.**

1. The check happens **after** re-locking the row from the DB, not
   against the in-memory `payout.state` the caller passed in. So if a
   second worker has already moved the row to a terminal state, we see
   it and bail — `transition()` raises before any `UPDATE` is issued.
2. The `failed` path's *refund* (write the `-X held` and `+X available`
   ledger entries) lives in `services.fail_payout`, which calls
   `transition()` and then writes the entries — all inside one
   `@transaction.atomic`. There is no commit window between "state =
   failed" and "merchant refunded".
3. **The retry sweeper does not bypass the state machine.** It calls
   `transition(payout.id, STATE_QUEUED_FOR_RETRY)` instead of
   editing the row directly. That keeps every state mutation in one
   place and makes "anything backwards is illegal" literally true at
   the code level.

`test_state_machine.py` has explicit tests for `failed → completed`,
`completed → failed`, `processing → pending`, and
`queued_for_retry → pending` — all raise. There's also a
round-trip test for `processing → queued_for_retry → processing →
completed` to prove the legitimate retry path still works.

---

## 5. The AI Audit — one specific case where the assistant got it wrong

**The bug AI suggested.** When I first sketched `request_payout`, the
assistant produced this:

```python
# AI's first cut — wrong.
@transaction.atomic
def request_payout(merchant_id, amount_paise, bank_account_id):
    merchant = Merchant.objects.get(pk=merchant_id)         # no lock
    available = LedgerEntry.objects.filter(
        merchant_id=merchant_id, bucket='available'
    ).aggregate(total=Sum('amount_paise'))['total'] or 0    # not under any lock
    if available < amount_paise:
        raise InsufficientBalance(...)
    Payout.objects.create(...)
    LedgerEntry.objects.bulk_create([...])
```

**What's wrong.** `transaction.atomic` is **not the same as a row lock.**
Two concurrent requests will both:

1. SELECT the merchant → both see the same row (default isolation =
   READ COMMITTED, which is snapshot-per-statement).
2. Aggregate the ledger → both see the same `available` (same reason).
3. Both pass the `if available < amount_paise` check.
4. Both insert ledger entries.

Result: a 100p merchant has two 60p payouts approved, and the next
balance read shows −20p. This is the textbook check-then-act race.

I caught it because the concurrency test fails: with the AI version,
`results == ['ok', 'ok']` and `bal.available_paise == -20`.

**The fix** (what's in the repo today):

```python
@transaction.atomic
def request_payout(*, merchant_id, amount_paise, bank_account_id):
    merchant = Merchant.objects.select_for_update().get(pk=merchant_id)  # ← row lock
    ...
    available = _locked_available_paise(merchant.id)   # recompute under lock
    if available < amount_paise:
        raise InsufficientBalance(...)
    ...
```

`select_for_update()` issues `SELECT ... FOR UPDATE`, which takes a
row-level write lock that blocks the second transaction at step 1. Only
one transaction at a time sees and modifies the merchant's ledger; the
loser sees the first transaction's hold entries when it acquires the
lock and is rejected cleanly.

Two related smaller fixes I had to make to other AI suggestions:

- It wanted to use `Sum('amount_paise')` without `Coalesce(..., 0)` — on
  a merchant with no rows that returns `None`, which the comparison
  `None < amount_paise` raises on in Python 3. Replaced with
  `Coalesce(Sum('amount_paise'), Value(0))`.
- It tried to put the *settlement* call (the simulated bank API) inside
  the `@transaction.atomic` block of `complete_payout`. That holds the
  merchant row lock across an external network call — exactly the kind
  of thing that turns a 200ms outage at the bank into a wedged Postgres.
  I split it: the worker does the simulated settlement *outside* any
  transaction, then opens a short transaction to apply the result.

The general lesson: AI is happy to write code that "looks like it locks"
because the function is decorated with `@transaction.atomic` or wrapped
in `with transaction.atomic():`. Atomicity ≠ isolation. You still have
to think about which row gets the `FOR UPDATE`.
