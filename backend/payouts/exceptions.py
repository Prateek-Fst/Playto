"""Domain exceptions and a DRF exception handler that maps them to clean HTTP."""
from __future__ import annotations

from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_handler


class PayoutEngineError(Exception):
    """Base for all expected, business-logic errors raised by the engine."""

    status_code = http_status.HTTP_400_BAD_REQUEST
    code = "payout_error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class InsufficientBalance(PayoutEngineError):
    status_code = http_status.HTTP_409_CONFLICT
    code = "insufficient_balance"


class InvalidStateTransition(PayoutEngineError):
    """Raised whenever someone tries to mutate a payout state in an illegal way.

    Crucially, a rejected transition does NOT mutate the row. The state machine
    helper checks the current state under a row lock and raises *before* any
    UPDATE is issued, so we never write a half-baked transition.
    """

    status_code = http_status.HTTP_409_CONFLICT
    code = "invalid_state_transition"


class IdempotencyConflict(PayoutEngineError):
    """Same key, different payload — a client bug we won't paper over."""

    status_code = http_status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "idempotency_conflict"


class PayoutValidationError(PayoutEngineError):
    code = "validation_error"


def payout_exception_handler(exc, context):
    if isinstance(exc, PayoutEngineError):
        body = {
            "error": {
                "code": exc.code,
                "message": exc.message,
                **({"details": exc.details} if exc.details else {}),
            }
        }
        return Response(body, status=exc.status_code)
    return drf_default_handler(exc, context)
