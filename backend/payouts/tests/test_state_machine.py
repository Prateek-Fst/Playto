"""State machine: illegal transitions must be rejected before any UPDATE."""
from __future__ import annotations

import uuid

from django.test import TestCase

from payouts.exceptions import InvalidStateTransition
from payouts.models import BankAccount, Merchant, Payout
from payouts.services import (
    complete_payout,
    credit_merchant,
    fail_payout,
    request_payout,
)
from payouts.state_machine import assert_legal, transition


class StateMachinePureTests(TestCase):
    """Pure transitions, no DB."""

    def test_pending_to_processing_is_legal(self):
        assert_legal(Payout.STATE_PENDING, Payout.STATE_PROCESSING)

    def test_completed_to_pending_is_illegal(self):
        with self.assertRaises(InvalidStateTransition):
            assert_legal(Payout.STATE_COMPLETED, Payout.STATE_PENDING)

    def test_failed_to_completed_is_illegal(self):
        with self.assertRaises(InvalidStateTransition):
            assert_legal(Payout.STATE_FAILED, Payout.STATE_COMPLETED)

    def test_processing_to_processing_is_illegal(self):
        with self.assertRaises(InvalidStateTransition):
            assert_legal(Payout.STATE_PROCESSING, Payout.STATE_PROCESSING)

    def test_processing_to_queued_for_retry_is_legal(self):
        assert_legal(Payout.STATE_PROCESSING, Payout.STATE_QUEUED_FOR_RETRY)

    def test_queued_for_retry_to_processing_is_legal(self):
        assert_legal(Payout.STATE_QUEUED_FOR_RETRY, Payout.STATE_PROCESSING)

    def test_queued_for_retry_to_pending_is_illegal(self):
        # Strictly forward-only: even retries don't go back to pending.
        with self.assertRaises(InvalidStateTransition):
            assert_legal(Payout.STATE_QUEUED_FOR_RETRY, Payout.STATE_PENDING)

    def test_processing_to_pending_is_illegal(self):
        with self.assertRaises(InvalidStateTransition):
            assert_legal(Payout.STATE_PROCESSING, Payout.STATE_PENDING)


class StateMachineWithDBTests(TestCase):
    """End-to-end transitions with side effects (ledger refunds)."""

    def setUp(self):
        self.merchant = Merchant.objects.create(
            name="SM Co", email=f"sm-{uuid.uuid4()}@x.test"
        )
        self.bank = BankAccount.objects.create(
            merchant=self.merchant,
            holder_name="SM Co",
            account_number_last4="0003",
            ifsc="HDFC0000003",
        )
        credit_merchant(self.merchant.id, 10_000, reference_id="seed")
        self.payout = request_payout(
            merchant_id=self.merchant.id,
            amount_paise=1_000,
            bank_account_id=self.bank.id,
        )

    def test_failed_payout_refunds_held_atomically(self):
        transition(self.payout.id, Payout.STATE_PROCESSING)
        fail_payout(self.payout.id, reason="test")

        from payouts.services import get_balance

        bal = get_balance(self.merchant.id)
        self.assertEqual(bal.available_paise, 10_000)
        self.assertEqual(bal.held_paise, 0)
        # Total never lost.
        self.assertEqual(bal.total_paise, 10_000)

    def test_cannot_complete_a_failed_payout(self):
        transition(self.payout.id, Payout.STATE_PROCESSING)
        fail_payout(self.payout.id, reason="test")
        with self.assertRaises(InvalidStateTransition):
            complete_payout(self.payout.id)

    def test_cannot_re_fail_a_completed_payout(self):
        transition(self.payout.id, Payout.STATE_PROCESSING)
        complete_payout(self.payout.id)
        with self.assertRaises(InvalidStateTransition):
            fail_payout(self.payout.id, reason="nope")

    def test_retry_path_round_trips_via_queued_for_retry(self):
        # processing -> queued_for_retry -> processing -> completed
        transition(self.payout.id, Payout.STATE_PROCESSING)
        transition(self.payout.id, Payout.STATE_QUEUED_FOR_RETRY)
        transition(self.payout.id, Payout.STATE_PROCESSING)
        complete_payout(self.payout.id)
