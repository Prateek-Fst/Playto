"""Business logic for the payout engine.

All money-moving operations live here, not in views or in tasks. Views call
``request_payout``; the Celery worker calls
``state_machine.transition`` / ``complete_payout`` / ``fail_payout``. None
of those functions trust any state passed in from the caller — they all
re-read the relevant row under a row lock and validate before mutating.

What's deliberately NOT here:

* No Python-level balance arithmetic. We never do
  ``available = sum([e.amount for e in entries])``. Balance is a
  ``SUM(amount_paise)`` aggregate the database evaluates.
* No "fast path" that skips the merchant lock. Even read-only balance
  queries that gate a write must be done after taking the lock; otherwise
  you've reintroduced check-then-act.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum, Value
from django.db.models.functions import Coalesce

from .exceptions import InsufficientBalance, PayoutValidationError
from .models import BankAccount, LedgerEntry, Merchant, Payout
from .state_machine import transition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Balances — pure SQL aggregation, never Python.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Balance:
    available_paise: int
    held_paise: int

    @property
    def total_paise(self) -> int:
        return self.available_paise + self.held_paise


def _bucket_sum(merchant_id, bucket: str) -> int:
    """Return ``SUM(amount_paise)`` for one merchant + bucket, or 0.

    The ``Coalesce(..., Value(0))`` keeps the return type as int even when
    the merchant has no rows in that bucket (otherwise Django returns None).
    """
    return LedgerEntry.objects.filter(
        merchant_id=merchant_id, bucket=bucket
    ).aggregate(
        total=Coalesce(Sum("amount_paise"), Value(0))
    )["total"]


def get_balance(merchant_id) -> Balance:
    """Compute available and held balance for a merchant.

    No locking — this is the public read path. If you need to gate a write
    on the balance, call ``_locked_available_paise`` after taking the
    merchant row lock.
    """
    return Balance(
        available_paise=_bucket_sum(merchant_id, LedgerEntry.BUCKET_AVAILABLE),
        held_paise=_bucket_sum(merchant_id, LedgerEntry.BUCKET_HELD),
    )


def _locked_available_paise(merchant_id) -> int:
    """Re-aggregate available balance. Caller must already hold the merchant row lock."""
    return _bucket_sum(merchant_id, LedgerEntry.BUCKET_AVAILABLE)


# ---------------------------------------------------------------------------
# Customer credit (used by the seed script and any future webhook).
# ---------------------------------------------------------------------------


@transaction.atomic
def credit_merchant(
    merchant_id, amount_paise: int, *, reference_id: str = "", note: str = ""
) -> LedgerEntry:
    """Append a credit to the merchant's available bucket. Atomic."""
    if amount_paise <= 0:
        raise PayoutValidationError("Credit amount must be positive")
    return LedgerEntry.objects.create(
        merchant_id=merchant_id,
        bucket=LedgerEntry.BUCKET_AVAILABLE,
        entry_type=LedgerEntry.TYPE_CREDIT,
        amount_paise=amount_paise,
        reference_type="customer_payment",
        reference_id=reference_id,
        note=note,
    )


# ---------------------------------------------------------------------------
# Payout lifecycle.
# ---------------------------------------------------------------------------


@transaction.atomic
def request_payout(
    *, merchant_id, amount_paise: int, bank_account_id
) -> Payout:
    """Create a pending payout and place a hold on the merchant's available balance.

    Concurrency invariant: at any commit, the available balance is non-negative.

    How we hold that invariant under concurrent requests:

      1. ``SELECT ... FOR UPDATE`` on the ``Merchant`` row. PostgreSQL takes
         a row-level write lock that blocks any other transaction trying the
         same SELECT FOR UPDATE on the same row. Per-merchant serialization,
         no global bottleneck.
      2. *After* we hold the lock, we re-aggregate the available balance.
         (Reading it before the lock would be a classic check-then-act bug.)
      3. If sufficient, we create the Payout and the two paired ledger
         entries (-X available, +X held) inside this same transaction.
      4. The lock is released only at COMMIT, so any concurrent request
         sees the new ledger rows when it eventually acquires the lock.

    The two ledger writes are paired so that ``SUM(available) + SUM(held)``
    is conserved on this operation.
    """
    if amount_paise <= 0:
        raise PayoutValidationError("amount_paise must be a positive integer")

    # 1. Lock the merchant row. This is the linearization point.
    try:
        merchant = Merchant.objects.select_for_update().get(pk=merchant_id)
    except Merchant.DoesNotExist as exc:
        raise PayoutValidationError("Unknown merchant") from exc

    try:
        bank_account = BankAccount.objects.get(
            pk=bank_account_id, merchant_id=merchant.id
        )
    except BankAccount.DoesNotExist as exc:
        raise PayoutValidationError(
            "Unknown bank_account_id for this merchant"
        ) from exc

    # 2. Recompute balance under the lock.
    available = _locked_available_paise(merchant.id)
    if available < amount_paise:
        raise InsufficientBalance(
            "Available balance is insufficient for this payout",
            available_paise=available,
            requested_paise=amount_paise,
        )

    # 3. Create the payout + paired ledger entries. All inside the same
    #    transaction, all under the merchant row lock.
    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank_account,
        amount_paise=amount_paise,
        state=Payout.STATE_PENDING,
    )
    LedgerEntry.objects.bulk_create(
        [
            LedgerEntry(
                merchant=merchant,
                bucket=LedgerEntry.BUCKET_AVAILABLE,
                entry_type=LedgerEntry.TYPE_PAYOUT_HOLD,
                amount_paise=-amount_paise,
                reference_type="payout",
                reference_id=str(payout.id),
                note="Hold for pending payout",
            ),
            LedgerEntry(
                merchant=merchant,
                bucket=LedgerEntry.BUCKET_HELD,
                entry_type=LedgerEntry.TYPE_PAYOUT_HOLD,
                amount_paise=amount_paise,
                reference_type="payout",
                reference_id=str(payout.id),
                note="Hold for pending payout",
            ),
        ]
    )

    logger.info(
        "payout.requested merchant=%s payout=%s amount_paise=%s",
        merchant.id,
        payout.id,
        amount_paise,
    )
    return payout


@transaction.atomic
def complete_payout(payout_id) -> Payout:
    """Finalize a successful payout.

    The held funds become a permanent debit. We move ``-amount`` out of the
    ``held`` bucket and write *no* offsetting credit — the money has left
    the system.
    """
    # Lock the payout via state-machine helper. (transition() also locks.)
    payout = transition(payout_id, Payout.STATE_COMPLETED)

    LedgerEntry.objects.create(
        merchant_id=payout.merchant_id,
        bucket=LedgerEntry.BUCKET_HELD,
        entry_type=LedgerEntry.TYPE_PAYOUT_DEBIT,
        amount_paise=-payout.amount_paise,
        reference_type="payout",
        reference_id=str(payout.id),
        note="Payout settled with bank",
    )
    logger.info("payout.completed payout=%s", payout.id)
    return payout


@transaction.atomic
def fail_payout(payout_id, *, reason: str = "") -> Payout:
    """Fail a payout and atomically return the held funds to available.

    The state transition and the refund ledger entries are written in the
    *same* transaction. Either both happen or neither does — there is no
    intermediate state where a payout is failed but the merchant has not
    been refunded.
    """
    payout = transition(payout_id, Payout.STATE_FAILED, failure_reason=reason)
    LedgerEntry.objects.bulk_create(
        [
            LedgerEntry(
                merchant_id=payout.merchant_id,
                bucket=LedgerEntry.BUCKET_HELD,
                entry_type=LedgerEntry.TYPE_PAYOUT_RELEASE,
                amount_paise=-payout.amount_paise,
                reference_type="payout",
                reference_id=str(payout.id),
                note=f"Release on failure: {reason}" if reason else "Release on failure",
            ),
            LedgerEntry(
                merchant_id=payout.merchant_id,
                bucket=LedgerEntry.BUCKET_AVAILABLE,
                entry_type=LedgerEntry.TYPE_PAYOUT_RELEASE,
                amount_paise=payout.amount_paise,
                reference_type="payout",
                reference_id=str(payout.id),
                note=f"Release on failure: {reason}" if reason else "Release on failure",
            ),
        ]
    )
    logger.info("payout.failed payout=%s reason=%s", payout.id, reason)
    return payout
