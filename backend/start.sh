#!/usr/bin/env bash
# Single-container launcher for Render's free tier.
#
# Spawns:
#   - 1 Celery worker  (background)
#   - 1 Celery beat    (background)
#   - 1 Gunicorn       (foreground; this is the process Render watches)
#
# Why this exists: the standard pattern is one Render service per process
# (web / worker / beat). On the free tier, only 1 service is free, so we
# bundle all three into the web service. Memory budget on free is 512 MB,
# so we run thin: gunicorn with 1 worker, celery worker with --pool=threads
# (no extra forks), beat as-is (it's tiny).
#
# Signal handling: gunicorn runs in the foreground via `exec`, so it is the
# main PID. The trap below forwards SIGTERM / SIGINT to the celery children
# and waits for them to exit, so Render's graceful-shutdown signal cleans
# up everything instead of leaving zombies.

set -euo pipefail

cd "$(dirname "$0")"

echo "[start] applying database migrations…"
python manage.py migrate --noinput

# Optional: collectstatic. Already done in the build phase, but harmless
# to re-run if static files weren't gathered. Comment out if it slows boot.
# python manage.py collectstatic --noinput >/dev/null

echo "[start] launching celery worker…"
celery -A config worker \
    --loglevel=info \
    --concurrency=1 \
    --pool=threads \
    --without-gossip --without-mingle --without-heartbeat \
    &
WORKER_PID=$!

echo "[start] launching celery beat…"
# Default (file-based) scheduler — picks up `app.conf.beat_schedule` from
# config/celery.py. We explicitly avoid django_celery_beat's DatabaseScheduler
# because it ignores the in-code schedule and only reads the
# django_celery_beat_periodictask table, which would mean Beat does nothing
# until we manually populate that table.
#
# `--schedule` points at /tmp so we don't pollute the project dir; the file
# is just a small pickle of last-run-times and is fine to lose on restart.
celery -A config beat \
    --loglevel=info \
    --schedule=/tmp/celerybeat-schedule \
    &
BEAT_PID=$!

# Forward shutdown signals to children, then wait for them to die.
shutdown() {
    echo "[start] received shutdown signal — stopping celery children"
    kill -TERM "$WORKER_PID" "$BEAT_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
    wait "$BEAT_PID"   2>/dev/null || true
    echo "[start] children stopped"
}
trap shutdown EXIT INT TERM

echo "[start] launching gunicorn on :${PORT:-8000}…"
exec gunicorn config.wsgi \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers 1 \
    --threads 4 \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
