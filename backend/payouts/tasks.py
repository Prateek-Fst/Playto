"""Celery tasks: payout processor + retry sweeper + idempotency cleanup.

Why each task is shaped the way it is:

* ``process_payout`` re-locks the payout row and *re-checks* the state from
  the DB before mutating. A worker can be invoked redundantly (Beat retry,
  manual re-enqueue, queue redelivery) and must be idempotent at the row
  level. The state machine helper enforces this — if the row is no longer
  in ``pending``, the transition raises and we exit cleanly.

* ``sweep_stuck_payouts`` is the safety net for the simulated 10% of
  payouts that "hang" in processing. Beat fires it every 10s. We pick up
  rows that have been processing longer than ``PAYOUT_STUCK_TIMEOUT_SECONDS``
  and retry them with exponential backoff (2^n seconds), capped at
  ``PAYOUT_MAX_RETRIES``. Past the cap, we fail the payout and refund.

* The "stuck" outcome doesn't change the row, so the sweeper finds it on
  ``processing_started_at`` age. We do NOT need a separate "stuck" state —
  ``processing`` + age is enough information.
"""
from __future__ import annotations

import logging
import random
import time

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .exceptions import InvalidStateTransition
from .models import IdempotencyKey, Payout
from .services import complete_payout, fail_payout
from .state_machine import TERMINAL_STATES, transition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bank settlement simulation.
# ---------------------------------------------------------------------------


def _simulate_bank_outcome() -> str:
    """Return one of: 'success', 'failure', 'stuck'.

    Probabilities come from settings so tests can pin them.
    """
    success = settings.PAYOUT_SUCCESS_RATE
    failure = settings.PAYOUT_FAILURE_RATE
    roll = random.random()
    if roll < success:
        return "success"
    if roll < success + failure:
        return "failure"
    return "stuck"


# ---------------------------------------------------------------------------
# process_payout
# ---------------------------------------------------------------------------


@shared_task(name="payouts.tasks.process_payout", bind=True)
def process_payout(self, payout_id: str) -> str:
    """Move a payout through the lifecycle.

    Accepts both first-attempt rows (``pending``) and retry-claimed rows
    (``queued_for_retry``). Idempotent: if the payout is already terminal
    or already processing under another worker, we exit cleanly — the
    state machine guards both paths.

    Returns a short status string for log-friendliness.
    """
    # 1. Claim the row by transitioning into `processing`. Both pending
    #    (first attempt) and queued_for_retry (sweeper-claimed) are legal
    #    sources for this transition; everything else raises and we bail.
    try:
        transition(payout_id, Payout.STATE_PROCESSING)
    except InvalidStateTransition:
        logger.info(
            "process_payout.skip payout=%s (not in a claimable state)", payout_id
        )
        return "skipped"
    except Payout.DoesNotExist:
        logger.warning("process_payout.missing payout=%s", payout_id)
        return "missing"

    # 2. Talk to the "bank".  Real life: HTTP call to a settlement provider.
    #    Here: small sleep + random outcome.
    time.sleep(random.uniform(0.2, 1.5))
    outcome = _simulate_bank_outcome()
    logger.info("process_payout.outcome payout=%s outcome=%s", payout_id, outcome)

    # 3. Apply the result.  Each branch is its own atomic transaction inside
    #    the relevant service function.
    try:
        if outcome == "success":
            complete_payout(payout_id)
        elif outcome == "failure":
            fail_payout(payout_id, reason="simulated_bank_failure")
        else:  # stuck
            # Leave it in `processing`. The sweeper will pick it up after
            # PAYOUT_STUCK_TIMEOUT_SECONDS and retry/fail.
            pass
    except InvalidStateTransition as exc:
        # Another worker beat us to a terminal state. That's fine.
        logger.info("process_payout.lost_race payout=%s reason=%s", payout_id, exc)
        return "lost_race"

    return outcome


# ---------------------------------------------------------------------------
# sweep_stuck_payouts (Beat -> every 10s)
# ---------------------------------------------------------------------------


@shared_task(name="payouts.tasks.sweep_stuck_payouts")
def sweep_stuck_payouts() -> dict:
    """Retry or terminally fail payouts that have hung in 'processing'.

    Called from Celery Beat. We don't try to do anything fancy with
    distributed locks across beat instances — the per-row
    ``select_for_update`` inside each retry call is enough to keep
    concurrent sweepers from double-actioning the same payout.
    """
    cutoff = timezone.now() - timezone.timedelta(
        seconds=settings.PAYOUT_STUCK_TIMEOUT_SECONDS
    )
    stuck_ids = list(
        Payout.objects.filter(
            state=Payout.STATE_PROCESSING,
            processing_started_at__lte=cutoff,
        ).values_list("id", flat=True)[:100]
    )
    if not stuck_ids:
        return {"swept": 0}

    retried = 0
    failed = 0
    for pid in stuck_ids:
        action = _retry_or_fail_one(pid)
        if action == "retried":
            retried += 1
        elif action == "failed":
            failed += 1

    logger.info(
        "sweep_stuck_payouts swept=%s retried=%s failed=%s",
        len(stuck_ids),
        retried,
        failed,
    )
    return {"swept": len(stuck_ids), "retried": retried, "failed": failed}


def _retry_or_fail_one(payout_id) -> str:
    """Atomically decide whether to retry or terminally fail a stuck payout.

    Strictly forward-only: the only state move available out of a stuck
    ``processing`` row is into ``queued_for_retry`` (or ``failed`` once
    we've used up our retries). The state machine refuses any backwards
    move.
    """
    with transaction.atomic():
        try:
            payout = Payout.objects.select_for_update().get(pk=payout_id)
        except Payout.DoesNotExist:
            return "missing"

        # Re-check under the lock — another worker may have already moved it.
        if payout.state != Payout.STATE_PROCESSING:
            return "noop"

        # Confirm it's actually stuck under the lock too.
        cutoff = timezone.now() - timezone.timedelta(
            seconds=settings.PAYOUT_STUCK_TIMEOUT_SECONDS
        )
        if payout.processing_started_at and payout.processing_started_at > cutoff:
            return "noop"  # someone restarted it after we picked it up

        if payout.retry_count >= settings.PAYOUT_MAX_RETRIES:
            # Give up. We're going to call fail_payout below (which opens
            # its own atomic + lock); leave the state alone in here.
            schedule_fail = True
            backoff_seconds = 0
        else:
            schedule_fail = False
            payout.retry_count += 1
            backoff_seconds = 2 ** payout.retry_count  # 2, 4, 8s
            # Forward-only: processing -> queued_for_retry. The retry
            # worker will move it back into processing via the state
            # machine when it picks the row up.
            payout.save(update_fields=["retry_count", "updated_at"])
            transition(payout.id, Payout.STATE_QUEUED_FOR_RETRY)

    # Outside the lock now.
    if schedule_fail:
        fail_payout(payout_id, reason="max_retries_exceeded")
        return "failed"

    process_payout.apply_async(args=[str(payout_id)], countdown=backoff_seconds)
    return "retried"


# ---------------------------------------------------------------------------
# cleanup_idempotency_keys (Beat -> hourly)
# ---------------------------------------------------------------------------


@shared_task(name="payouts.tasks.cleanup_idempotency_keys")
def cleanup_idempotency_keys() -> int:
    """Delete idempotency records older than the configured TTL."""
    cutoff = timezone.now() - timezone.timedelta(hours=settings.IDEMPOTENCY_TTL_HOURS)
    deleted, _ = IdempotencyKey.objects.filter(created_at__lt=cutoff).delete()
    logger.info("cleanup_idempotency_keys deleted=%s", deleted)
    return deleted
