"""Celery app and beat schedule.

Beat is responsible for two periodic jobs:

1. ``payouts.tasks.sweep_stuck_payouts`` — every 10s, pick up payouts that
   have been in the ``processing`` state for longer than
   ``PAYOUT_STUCK_TIMEOUT_SECONDS`` and either retry them with exponential
   backoff or give up and refund the held funds.
2. ``payouts.tasks.cleanup_idempotency_keys`` — every hour, delete keys
   older than ``IDEMPOTENCY_TTL_HOURS``. The TTL is also enforced on read,
   so this is just hygiene.
"""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("playto_payouts")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


# When the worker runs with --pool=threads (which we use on the bundled
# Render free-tier deployment), each task may run on a different thread
# than the previous one. Django keeps a *per-thread* DB connection, and
# without explicit cleanup those connections accumulate behind the
# Supabase pooler until we hit "too many connections".
#
# The `task_postrun` signal fires after every task; we close any stale
# connections that thread might be holding so the next task starts fresh.
from celery.signals import task_postrun  # noqa: E402


@task_postrun.connect
def close_db_connections_after_task(**_kwargs):
    from django.db import connections

    for conn in connections.all():
        try:
            conn.close_if_unusable_or_obsolete()
        except Exception:
            # Connection may already be torn down; safe to ignore.
            pass

# Namespace every key Celery writes to Redis with `playto:` so this project
# can safely share a Redis Cloud / Upstash database with other apps without
# their task queues / result keys colliding.
#
# `visibility_timeout` is shorter than the default 1h to play nicely with
# managed Redis providers (Upstash/Redis Cloud) that drop idle connections
# aggressively. Our individual payout tasks finish in <2s, so 5min is plenty.
app.conf.broker_transport_options = {
    "global_keyprefix": "playto:",
    "visibility_timeout": 300,
}
app.conf.result_backend_transport_options = {"global_keyprefix": "playto:"}
app.conf.broker_connection_retry_on_startup = True

# Celery 5.x requires us to declare TLS verification mode explicitly when the
# broker / result-backend URL is `rediss://` (e.g. Upstash, Redis Cloud TLS).
# Without this Celery raises:
#   "A rediss:// URL must have parameter ssl_cert_reqs and this must be set
#    to CERT_REQUIRED, CERT_OPTIONAL, or CERT_NONE"
# CERT_NONE is fine here: Upstash terminates TLS at their edge with a public
# cert and the connection is encrypted. Verification doesn't add security
# against any threat we're protecting against on a managed Redis hop.
import ssl  # noqa: E402

_redis_ssl_options = {"ssl_cert_reqs": ssl.CERT_NONE}
if (settings_url := os.environ.get("CELERY_BROKER_URL", "")).startswith("rediss://"):
    app.conf.broker_use_ssl = _redis_ssl_options
if os.environ.get("CELERY_RESULT_BACKEND", "").startswith("rediss://"):
    app.conf.redis_backend_use_ssl = _redis_ssl_options

app.conf.beat_schedule = {
    "sweep-stuck-payouts": {
        "task": "payouts.tasks.sweep_stuck_payouts",
        "schedule": 10.0,
    },
    "cleanup-idempotency-keys": {
        "task": "payouts.tasks.cleanup_idempotency_keys",
        "schedule": crontab(minute=0),
    },
}
