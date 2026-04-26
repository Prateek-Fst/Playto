"""Idempotency test: same key + same payload should never create two payouts."""
from __future__ import annotations

import uuid

from django.test import TestCase

from payouts.exceptions import IdempotencyConflict
from payouts.idempotency import with_idempotency
from payouts.models import BankAccount, Merchant, Payout
from payouts.serializers import PayoutSerializer
from payouts.services import credit_merchant, request_payout


class PayoutIdempotencyTests(TestCase):
    def setUp(self):
        self.merchant = Merchant.objects.create(
            name="Idem Co", email=f"idem-{uuid.uuid4()}@x.test"
        )
        self.bank = BankAccount.objects.create(
            merchant=self.merchant,
            holder_name="Idem Co",
            account_number_last4="0002",
            ifsc="HDFC0000002",
        )
        credit_merchant(self.merchant.id, 10_000, reference_id="seed", note="setup")
        self.key = str(uuid.uuid4())
        self.payload = {"amount_paise": 1_000, "bank_account_id": str(self.bank.id)}

    def _do_call(self):
        def handler():
            payout = request_payout(
                merchant_id=self.merchant.id,
                amount_paise=self.payload["amount_paise"],
                bank_account_id=self.payload["bank_account_id"],
            )
            return 201, PayoutSerializer(payout).data

        return with_idempotency(
            merchant_id=self.merchant.id,
            key=self.key,
            payload=self.payload,
            handler=handler,
        )

    def test_replay_returns_same_payout_and_creates_no_duplicate(self):
        status1, body1, replayed1 = self._do_call()
        status2, body2, replayed2 = self._do_call()

        self.assertEqual(status1, 201)
        self.assertEqual(status2, 201)
        self.assertFalse(replayed1)
        self.assertTrue(replayed2)
        self.assertEqual(body1["id"], body2["id"])
        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 1)

    def test_same_key_different_payload_is_rejected(self):
        # First call with the original payload.
        self._do_call()

        # Second call: same key, different amount.
        original = self.payload
        self.payload = {**original, "amount_paise": 9_999}
        with self.assertRaises(IdempotencyConflict):
            self._do_call()

        self.assertEqual(Payout.objects.filter(merchant=self.merchant).count(), 1)
