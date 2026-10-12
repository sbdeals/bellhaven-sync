#!/usr/bin/env bash
# Daily bellhaven-sync run for a Linux VM. Invoked by cron (see ops/crontab).
#
# `bellhaven-sync run` scrapes the website, reads the CRM and stores proposals in the
# local SQLite db. It never writes to the CRM; only the review app does, on Approve.
#
# Exit status: the CLI's own status (non-zero on any scrape/CRM/config error),
# 124 if the run exceeded the timeout, 1 if another run is still in progress or the
# env file is missing. All paths can be overridden through the environment.
set -euo pipefail

APP_DIR="${BELLHAVEN_APP_DIR:-/opt/bellhaven-sync}"
ENV_FILE="${BELLHAVEN_ENV_FILE:-/etc/bellhaven-sync.env}"
LOCK_FILE="${BELLHAVEN_LOCK_FILE:-/run/lock/bellhaven-sync.lock}"
UV_BIN="${UV_BIN:-/usr/local/bin/uv}"
RUN_TIMEOUT="${BELLHAVEN_RUN_TIMEOUT:-15m}"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) [run-daily] $*"; }

# One run at a time. The lock is held on fd 9 until this script exits.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "ERROR: another bellhaven-sync run holds $LOCK_FILE; not starting a second one"
    exit 1
fi

# Secrets and settings (BELLHAVEN_CRM_TOKEN, BELLHAVEN_BASE_URL, BELLHAVEN_DB_PATH, ...).
if [[ ! -r "$ENV_FILE" ]]; then
    log "ERROR: cannot read $ENV_FILE"
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$APP_DIR"

started=$(date +%s)
log "start (pid $$, app dir $APP_DIR)"

status=0
timeout "$RUN_TIMEOUT" "$UV_BIN" run --frozen bellhaven-sync run || status=$?

log "end status=$status duration=$(( $(date +%s) - started ))s"
exit "$status"
