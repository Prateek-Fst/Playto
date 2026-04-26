"""Concurrency test: two simultaneous payouts that together exceed the balance.

Setup: merchant has 100 paise.  Two threads each try to withdraw 60 paise.
Expected: exactly one succeeds; the other raises ``InsufficientBalance``.

This must run against PostgreSQL — SQLite's ``SELECT FOR UPDATE`` is a no-op,
so the test would pass for the wrong reason there. We use
``TransactionTestCase`` (not ``TestCase``) so each thread sees the others'
committed writes.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from django.db import connection, connections
from django.test import TransactionTestCase

from payouts.exceptions import InsufficientBalance
from payouts.models import BankAccount, Merchant, Payout
from payouts.services import credit_merchant, get_balance, request_payout


class PayoutConcurrencyTests(TransactionTestCase):
    """Race conditions are the bug. Make sure we don't have any."""

    def setUp(self):
        self.merchant = Merchant.objects.create(
            name="Race Co", email=f"race-{uuid.uuid4()}@x.test"
        )
        self.bank = BankAccount.objects.create(
            merchant=self.merchant,
            holder_name="Race Co",
            account_number_last4="0001",
            ifsc="HDFC0000001",
        )
        # 100 paise.  Two 60-paise payouts cannot both succeed.
        credit_merchant(self.merchant.id, 100, reference_id="seed", note="setup")

    def _attempt_payout(self, barrier: threading.Barrier):
        """Run inside a worker thread.  Each thread gets its own DB connection."""
        # Force a fresh connection per thread; otherwise threads share one.
        connections.close_all()
        try:
            barrier.wait(timeout=5)  # release both threads at the same instant
            request_payout(
                merchant_id=self.merchant.id,
                amount_paise=60,
                bank_account_id=self.bank.id,
            )
            return "ok"
        except InsufficientBalance:
            return "rejected"
        finally:
            connections.close_all()

    def test_two_concurrent_overlapping_payouts_serialize_cleanly(self):
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._attempt_payout, barrier),
                pool.submit(self._attempt_payout, barrier),
            ]
            results = sorted(f.result() for f in futures)

        self.assertEqual(results, ["ok", "rejected"], f"got {results}")

        # Database state must match the test outcome:
        #   one Payout row, one held entry, available = 40, held = 60.
        connection.close()
        connection.connect()
        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 1)
        bal = get_balance(self.merchant.id)
        self.assertEqual(bal.available_paise, 40)
        self.assertEqual(bal.held_paise, 60)
        self.assertEqual(bal.total_paise, 100)  # invariant: nothing lost or duplicated
