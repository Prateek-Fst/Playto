"""DRF views.

We intentionally keep these thin — they parse input, resolve the merchant
context, delegate to ``services``/``idempotency``, and serialize. Anything
that mutates money lives in the service layer behind a
``transaction.atomic`` block.

Merchant resolution: for this take-home there is no auth. The frontend
sends ``X-Merchant-Id`` to identify which merchant context the request is
for. In production this would come from an authenticated session.
"""
from __future__ import annotations

from celery import current_app as celery_app
from rest_framework import status as http_status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .exceptions import PayoutValidationError
from .idempotency import with_idempotency
from .models import BankAccount, LedgerEntry, Merchant, Payout
from .serializers import (
    BankAccountSerializer,
    LedgerEntrySerializer,
    MerchantSerializer,
    PayoutCreateSerializer,
    PayoutSerializer,
)
from .services import get_balance, request_payout


def _merchant_from_request(request) -> Merchant:
    merchant_id = request.headers.get("X-Merchant-Id")
    if not merchant_id:
        raise PayoutValidationError(
            "X-Merchant-Id header is required (acts as the merchant auth context)"
        )
    try:
        return Merchant.objects.get(pk=merchant_id)
    except Merchant.DoesNotExist as exc:
        raise PayoutValidationError("Unknown merchant") from exc
    except (ValueError, TypeError) as exc:
        raise PayoutValidationError("X-Merchant-Id must be a valid UUID") from exc


@api_view(["GET"])
def list_merchants(request):
    """Public list — only used by the frontend to populate a merchant switcher."""
    qs = Merchant.objects.order_by("created_at")
    return Response(MerchantSerializer(qs, many=True).data)


@api_view(["GET"])
def merchant_balance(request):
    merchant = _merchant_from_request(request)
    balance = get_balance(merchant.id)
    return Response(
        {
            "merchant_id": str(merchant.id),
            "available_paise": balance.available_paise,
            "held_paise": balance.held_paise,
            "total_paise": balance.total_paise,
        }
    )


@api_view(["GET"])
def merchant_bank_accounts(request):
    merchant = _merchant_from_request(request)
    qs = merchant.bank_accounts.order_by("created_at")
    return Response(BankAccountSerializer(qs, many=True).data)


@api_view(["GET"])
def merchant_ledger(request):
    merchant = _merchant_from_request(request)
    qs = merchant.ledger_entries.order_by("-created_at")[:100]
    return Response(LedgerEntrySerializer(qs, many=True).data)


@api_view(["GET", "POST"])
def payouts(request):
    if request.method == "GET":
        merchant = _merchant_from_request(request)
        qs = (
            Payout.objects.filter(merchant=merchant)
            .select_related("bank_account")
            .order_by("-created_at")[:100]
        )
        return Response(PayoutSerializer(qs, many=True).data)

    return _create_payout(request)


def _create_payout(request):
    merchant = _merchant_from_request(request)
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key:
        raise PayoutValidationError("Idempotency-Key header is required")

    serializer = PayoutCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payload = serializer.validated_data
    # Use string form so the fingerprint is JSON-serializable.
    payload_for_fp = {
        "amount_paise": payload["amount_paise"],
        "bank_account_id": str(payload["bank_account_id"]),
    }

    def handler():
        payout = request_payout(
            merchant_id=merchant.id,
            amount_paise=payload["amount_paise"],
            bank_account_id=payload["bank_account_id"],
        )
        body = PayoutSerializer(payout).data
        return http_status.HTTP_201_CREATED, body

    status_code, body, replayed = with_idempotency(
        merchant_id=merchant.id,
        key=idempotency_key,
        payload=payload_for_fp,
        handler=handler,
    )

    response = Response(body, status=status_code)
    response["Idempotent-Replayed"] = "true" if replayed else "false"

    # Kick off the async processor for freshly-created payouts only. The
    # replay path must NOT enqueue another worker — that would let one
    # logical request produce two settlement attempts.
    if not replayed and status_code == http_status.HTTP_201_CREATED:
        celery_app.send_task("payouts.tasks.process_payout", args=[body["id"]])

    return response


@api_view(["GET"])
def payout_detail(request, payout_id):
    merchant = _merchant_from_request(request)
    try:
        payout = (
            Payout.objects.select_related("bank_account")
            .get(pk=payout_id, merchant=merchant)
        )
    except Payout.DoesNotExist as exc:
        raise PayoutValidationError("Unknown payout") from exc
    return Response(PayoutSerializer(payout).data)
