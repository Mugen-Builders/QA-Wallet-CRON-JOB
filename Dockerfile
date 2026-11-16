# qa-traffic-bot — organic-traffic daemon for the qa-wallet-tester-app (Cartesi Rollups v3) on
# Ethereum Sepolia. This runs as ONE long-lived process (an always-on fly.io Machine), NOT a
# cron/scheduled job: it keeps in-memory + SQLite state and schedules its own traffic internally.
# See docs/DEPLOY-FLY.md for the deploy runbook (secrets, volume, single-instance rule).
FROM python:3.12-slim

# tzdata  : the daily report renders in QA_TRAFFIC_REPORT_TZ (e.g. Africa/Lagos) via zoneinfo.
# ca-certs: TLS to the RPC / node / sequencer / Discord.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, for layer caching. pytest is test-only, so it is stripped from the image.
COPY requirements.txt .
RUN grep -v '^pytest' requirements.txt > /tmp/requirements.runtime.txt \
 && pip install --no-cache-dir -r /tmp/requirements.runtime.txt

# Application code only. .dockerignore keeps var/, *.env, tests, caches out of the build context.
COPY qatraffic ./qatraffic

# All mutable state (SQLite DB, heartbeat, logs) lives under QA_TRAFFIC_HOME. Point it at the
# mounted fly volume so it survives restarts (fly.toml [mounts] destination must match).
ENV QA_TRAFFIC_HOME=/data \
    QA_TRAFFIC_HEALTH_PORT=8080 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data

# Liveness endpoint polled by the platform health check (returns 503 when the loop is wedged).
EXPOSE 8080

# Exec form (no shell) so SIGTERM reaches Python and the loop shuts down cleanly.
CMD ["python3", "-m", "qatraffic", "run"]
