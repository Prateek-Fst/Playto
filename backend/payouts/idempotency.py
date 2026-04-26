"""Idempotency-Key handling for ``POST /api/v1/payouts``.

Design summary:

* ``IdempotencyKey`` has a unique constraint on ``(merchant_id, key)``.
* The *first* request inside a transaction does ``get_or_create``. The
  freshly inserted row is implicitly write-locked by Postgres until the
  enclosing transaction commits.
* A *concurrent* second request with the same key calls
  ``select_for_update`` on that row and **blocks** at the database level
  until the first transaction commits. It then reads the cached response
  and returns it without doing any work.
* If the second request arrives *after* the first finished, it sees the
  row already in ``completed`` state and returns the cached body
  immediately.
* If the request payload differs (same key, different body), we raise
  ``IdempotencyConflict`` — that's a client bug, not a replay.
* Records older than ``IDEMPOTENCY_TTL_HOURS`` are treated as expired
  on read (and reaped by a Celery beat job).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Callable

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .exceptions import IdempotencyConflict, PayoutValidationError
from .models import IdempotencyKey

logger = logging.getLogger(__name__)


def fingerprint(payload: dict) -> str:
    """Stable hash of the request body so we can detect "same key, different payload"."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _is_expired(record: IdempotencyKey) -> bool:
    ttl = timedelta(hours=settings.IDEMPOTENCY_TTL_HOURS)
    return timezone.now() - record.created_at > ttl


def with_idempotency(
    *,
    merchant_id,
    key: str,
    payload: dict,
    handler: Callable[[], tuple[int, dict]],
) -> tuple[int, dict, bool]:
    """Run ``handler`` exactly once for a given ``(merchant, key)``.

    Returns ``(status_code, response_body, replayed)``.

    ``replayed`` is True when we returned a cached response from a previous
    successful call.
    """
    if not key:
        raise PayoutValidationError("Idempotency-Key header is required")

    request_fp = fingerprint(payload)

    with transaction.atomic():
        # Path 1: try to claim the key by INSERT. If it already exists, fall
        # through to Path 2.
        try:
            with transaction.atomic():
                record = IdempotencyKey.objects.create(
                    merchant_id=merchant_id,
                    key=key,
                    request_fingerprint=request_fp,
                    status=IdempotencyKey.STATUS_IN_PROGRESS,
                )
                created = True
        except IntegrityError:
            created = False
            record = None

        if not created:
            # Path 2: row exists. Lock it. If a concurrent worker is still
            # processing, this SELECT FOR UPDATE blocks until they commit.
            record = (
                IdempotencyKey.objects.select_for_update()
                .filter(merchant_id=merchant_id, key=key)
                .first()
            )
            if record is None:  # raced with cleanup
                # Treat as fresh: re-INSERT in a nested savepoint.
                record = IdempotencyKey.objects.create(
                    merchant_id=merchant_id,
                    key=key,
                    request_fingerprint=request_fp,
                    status=IdempotencyKey.STATUS_IN_PROGRESS,
                )
                created = True

        if not created:
            # Expired? Treat as a brand-new request: drop the old row and
            # claim a fresh one.
            if _is_expired(record):
                logger.info("idempotency.expired merchant=%s key=%s", merchant_id, key)
                record.delete()
                record = IdempotencyKey.objects.create(
                    merchant_id=merchant_id,
                    key=key,
                    request_fingerprint=request_fp,
                    status=IdempotencyKey.STATUS_IN_PROGRESS,
                )
                created = True

        if not created:
            # Same key with a different payload is a client bug — refuse it.
            if record.request_fingerprint != request_fp:
                raise IdempotencyConflict(
                    "Idempotency-Key reused with a different request payload",
                    key=key,
                )

            if record.status == IdempotencyKey.STATUS_COMPLETED:
                logger.info(
                    "idempotency.replay merchant=%s key=%s payout=%s",
                    merchant_id,
                    key,
                    record.payout_id,
                )
                return (
                    record.response_status_code or 200,
                    record.response_body or {},
                    True,
                )
            # status == in_progress: previous attempt crashed mid-flight (we
            # only got here because we hold the lock and the prior tx already
            # committed/rolled back). status == failed: previous attempt
            # raised before writing a response. In both cases let the caller
            # try again with the same key — re-run the handler.

        # We are the canonical executor. Run the handler.
        try:
            status_code, body = handler()
        except Exception as exc:  # noqa: BLE001
            record.status = IdempotencyKey.STATUS_FAILED
            record.response_status_code = getattr(exc, "status_code", 500)
            record.response_body = {
                "error": {
                    "code": getattr(exc, "code", "internal_error"),
                    "message": str(exc),
                }
            }
            record.save(
                update_fields=[
                    "status",
                    "response_status_code",
                    "response_body",
                    "updated_at",
                ]
            )
            raise

        record.status = IdempotencyKey.STATUS_COMPLETED
        record.response_status_code = status_code
        record.response_body = body
        # If the handler returned a payout, link it for forensics.
        payout_id = body.get("id") if isinstance(body, dict) else None
        if payout_id:
            record.payout_id = payout_id
        record.save(
            update_fields=[
                "status",
                "response_status_code",
                "response_body",
                "payout",
                "updated_at",
            ]
        )
        return status_code, body, False
