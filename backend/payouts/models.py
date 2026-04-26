"""
Models for the Playto Payout Engine.

Money rules (enforced by these models):

* All amounts are stored as ``BigIntegerField`` in **paise** (1 INR = 100 paise).
  No ``FloatField``, no ``DecimalField``. Integer arithmetic is exact and
  matches what the bank settlement layer talks in.
* Balance is *never* stored on the merchant. It is computed as a SQL aggregate
  over ``LedgerEntry`` rows. See ``payouts.services.balances``.
* ``LedgerEntry`` is double-entry-flavoured but kept as a single signed amount
  per row for simplicity. Each entry belongs to a ``bucket`` — ``available``
  or ``held`` — so we can compute both balances from the same table without
  needing a separate "holds" structure.

Lifecycle of a 600-paise payout against a merchant with 1000 paise of credits:

  1. Customer-payment seed: +1000 in ``available``  (entry_type=CREDIT)
  2. Payout requested:       -600 in ``available``  (entry_type=PAYOUT_HOLD)
                            +600 in ``held``      (entry_type=PAYOUT_HOLD)
     (both rows written in the same transaction; payout state = pending)
  3a. Payout completed:      -600 in ``held``      (entry_type=PAYOUT_DEBIT)
      Money has left the system.  available=400, held=0.
  3b. Payout failed:         -600 in ``held``      (entry_type=PAYOUT_RELEASE)
                            +600 in ``available``  (entry_type=PAYOUT_RELEASE)
      Money is back to the merchant. available=1000, held=0.

The invariant
    SUM(available) + SUM(held) == total credits − total finalized debits
holds at every commit boundary, by construction.
"""
from __future__ import annotations

import uuid

from django.core.validators import MinValueValidator
from django.db import models


class Merchant(models.Model):
    """A merchant who receives credits and requests payouts.

    The row itself is the *lock target* for per-merchant serialization. We
    never compute balance from a column on this table — but acquiring a
    ``SELECT FOR UPDATE`` on this row is what makes "check balance, then
    debit" race-free under concurrent payout requests.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "merchants"

    def __str__(self) -> str:
        return self.name


class BankAccount(models.Model):
    """An Indian bank account a merchant can withdraw to.

    We don't need real verification for this challenge — the schema just
    needs to be plausible enough to model "payout into account X".
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name="bank_accounts"
    )
    holder_name = models.CharField(max_length=255)
    account_number_last4 = models.CharField(max_length=4)
    ifsc = models.CharField(max_length=11)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bank_accounts"

    def __str__(self) -> str:
        return f"{self.holder_name} ****{self.account_number_last4}"


class LedgerEntry(models.Model):
    """An immutable line in the merchant's money ledger.

    Append-only. Never updated, never deleted. ``amount_paise`` is signed:
    positive entries add to the bucket, negative entries subtract from it.
    """

    BUCKET_AVAILABLE = "available"
    BUCKET_HELD = "held"
    BUCKET_CHOICES = [
        (BUCKET_AVAILABLE, "Available"),
        (BUCKET_HELD, "Held"),
    ]

    TYPE_CREDIT = "credit"
    TYPE_PAYOUT_HOLD = "payout_hold"
    TYPE_PAYOUT_DEBIT = "payout_debit"
    TYPE_PAYOUT_RELEASE = "payout_release"
    TYPE_CHOICES = [
        (TYPE_CREDIT, "Credit (incoming customer payment)"),
        (TYPE_PAYOUT_HOLD, "Hold (payout requested)"),
        (TYPE_PAYOUT_DEBIT, "Debit (payout completed)"),
        (TYPE_PAYOUT_RELEASE, "Release (payout failed, funds returned)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    bucket = models.CharField(max_length=16, choices=BUCKET_CHOICES)
    entry_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    # Signed paise amount.  +1000 in `available` means a credit; -600 in
    # `available` means money was moved out of available (e.g. into held).
    amount_paise = models.BigIntegerField()
    # Optional pointer back to a Payout. Free-form to also support credit
    # references (an external payment id) without a hard FK.
    reference_type = models.CharField(max_length=32, blank=True, default="")
    reference_id = models.CharField(max_length=64, blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_entries"
        indexes = [
            models.Index(fields=["merchant", "bucket"]),
            models.Index(fields=["merchant", "created_at"]),
            models.Index(fields=["reference_type", "reference_id"]),
        ]
        # Ledger is append-only.  We don't define a unique constraint on
        # (reference_id, entry_type) because a single payout produces
        # multiple entries (hold across two buckets, etc.) but the service
        # layer never writes the same logical entry twice.

    def __str__(self) -> str:
        sign = "+" if self.amount_paise >= 0 else ""
        return f"{self.bucket} {sign}{self.amount_paise} ({self.entry_type})"


class Payout(models.Model):
    """A merchant's request to withdraw funds to a bank account.

    The state column is the source of truth for the payout's lifecycle. All
    transitions go through ``payouts.state_machine.transition`` — the model
    does not expose an unchecked setter for ``state``.
    """

    STATE_PENDING = "pending"
    STATE_PROCESSING = "processing"
    STATE_QUEUED_FOR_RETRY = "queued_for_retry"
    STATE_COMPLETED = "completed"
    STATE_FAILED = "failed"
    STATE_CHOICES = [
        (STATE_PENDING, "Pending"),
        (STATE_PROCESSING, "Processing"),
        (STATE_QUEUED_FOR_RETRY, "Queued for retry"),
        (STATE_COMPLETED, "Completed"),
        (STATE_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name="payouts"
    )
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="payouts"
    )
    amount_paise = models.BigIntegerField(validators=[MinValueValidator(1)])
    state = models.CharField(
        max_length=16, choices=STATE_CHOICES, default=STATE_PENDING
    )
    # Set when state moves to processing; consulted by the retry sweeper.
    processing_started_at = models.DateTimeField(null=True, blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payouts"
        indexes = [
            models.Index(fields=["merchant", "-created_at"]),
            models.Index(fields=["state", "processing_started_at"]),
        ]

    def __str__(self) -> str:
        return f"Payout {self.id} {self.state} {self.amount_paise}p"


class IdempotencyKey(models.Model):
    """Stored idempotency record for ``POST /api/v1/payouts``.

    The unique constraint is ``(merchant, key)`` — keys are scoped per
    merchant so two merchants can independently use the same UUID. We rely
    on the unique index together with PostgreSQL's row-level lock on the
    inserted row to serialize concurrent requests with the same key:

    * The first request creates the row inside its transaction. The row is
      implicitly locked (write lock) until that transaction commits.
    * A second request that hits the same key will block on
      ``SELECT FOR UPDATE`` until the first commits, then read the cached
      response. No work is repeated.

    Records older than ``IDEMPOTENCY_TTL_HOURS`` are ignored on read and
    swept by ``payouts.tasks.cleanup_idempotency_keys``.
    """

    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.CASCADE, related_name="idempotency_keys"
    )
    key = models.CharField(max_length=128)
    # Hash of the request body so we can detect "same key, different payload"
    # — that is a client bug we should reject loudly.
    request_fingerprint = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_IN_PROGRESS
    )
    response_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    payout = models.ForeignKey(
        Payout, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "idempotency_keys"
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "key"], name="uniq_idempotency_per_merchant"
            ),
        ]
        indexes = [
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.merchant_id}:{self.key} ({self.status})"
