"""Payout state machine.

Single source of truth for what transitions are legal. The transition helper
re-locks the payout row, checks the *current* state from the DB (not the
stale in-memory value), validates the transition, and saves under the same
lock — so concurrent workers can't both observe ``processing`` and then both
write ``completed``.

Legal (strictly forward-only):
  pending          -> processing
  processing       -> completed
  processing       -> failed
  processing       -> queued_for_retry   (retry sweeper picks up a stuck row)
  queued_for_retry -> processing         (retry worker resumes settlement)
  queued_for_retry -> failed             (retry sweeper gives up at max attempts)

Illegal (raise InvalidStateTransition):
  completed   -> *
  failed      -> *           (terminal states stay terminal)
  *           -> pending     (no going back)
  processing  -> processing  (would re-enter without bookkeeping)

The ``failed`` and ``completed`` states are terminal. Refunds happen
*together* with the failed transition inside the same ``transaction.atomic``
block — see ``payouts.services.fail_payout``.
"""
from __future__ import annotations

from typing import Iterable

from django.db import transaction
from django.utils import timezone

from .exceptions import InvalidStateTransition
from .models import Payout

# Adjacency list of legal transitions. If you add a state, add it here too.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    Payout.STATE_PENDING: frozenset({Payout.STATE_PROCESSING}),
    Payout.STATE_PROCESSING: frozenset(
        {
            Payout.STATE_COMPLETED,
            Payout.STATE_FAILED,
            Payout.STATE_QUEUED_FOR_RETRY,
        }
    ),
    Payout.STATE_QUEUED_FOR_RETRY: frozenset(
        {Payout.STATE_PROCESSING, Payout.STATE_FAILED}
    ),
    Payout.STATE_COMPLETED: frozenset(),
    Payout.STATE_FAILED: frozenset(),
}

TERMINAL_STATES: frozenset[str] = frozenset(
    {Payout.STATE_COMPLETED, Payout.STATE_FAILED}
)


def assert_legal(from_state: str, to_state: str) -> None:
    """Pure check, no I/O. Used by tests and by ``transition``."""
    allowed: Iterable[str] = LEGAL_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise InvalidStateTransition(
            f"Illegal payout state transition {from_state!r} -> {to_state!r}",
            from_state=from_state,
            to_state=to_state,
        )


@transaction.atomic
def transition(payout_id, to_state: str, *, failure_reason: str = "") -> Payout:
    """Atomically move ``payout`` to ``to_state``.

    Re-locks the row with ``select_for_update`` so the *committed* state is
    what we validate against. This is critical when two workers race on the
    same payout — only the one that wins the lock can transition; the other
    will read the new state and raise :class:`InvalidStateTransition`.
    """
    payout = Payout.objects.select_for_update().get(pk=payout_id)
    assert_legal(payout.state, to_state)

    payout.state = to_state
    if to_state == Payout.STATE_PROCESSING:
        # Reset the clock every time we enter processing (initial attempt and
        # retries both). The sweeper uses this to detect stuck rows.
        payout.processing_started_at = timezone.now()
    if to_state == Payout.STATE_QUEUED_FOR_RETRY:
        # Clear the processing-age clock so the sweeper doesn't re-pick the
        # same row before the retry worker has had a chance to run.
        payout.processing_started_at = None
    if to_state == Payout.STATE_FAILED and failure_reason:
        payout.failure_reason = failure_reason
    payout.save(
        update_fields=[
            "state",
            "processing_started_at",
            "failure_reason",
            "updated_at",
        ]
    )
    return payout
