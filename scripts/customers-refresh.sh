#!/usr/bin/env bash
# Nightly refresh of the dashboard's Customers page: pull the latest POS analytics export, copy
# customers.json into the voice container, import it. Installed as a host cron job on the VPS —
# this line REPLACES the old inline crontab entry:
#
#   45 7 * * * /root/happytime-budtender/scripts/customers-refresh.sh >> /root/customers-refresh.log 2>&1 # customers-refresh
#
# (Host cron on the VPS runs in UTC and ignores CRON_TZ, so 07:45 is 00:45 PDT.)
#
# WHY a script: the old crontab line chained the three commands inline and only the LAST command's
# output reached the log, so `git pull` failing on a revoked token went unseen — customer profiles
# stopped refreshing on 2026-07-10 and nothing said so. Here every command's stdout+stderr goes to
# the log, any failure stops the chain, and the end of the run is reported to /dashboard/health/
# (record_job_run, name `customers-refresh`) whether it worked or not: ok with the import's own
# summary line, or FAILED with the last line the failing command printed. The Health page marks the
# job STALE when it has not finished within 26 hours.
set -euo pipefail

cd "$(dirname "$0")/.."   # `docker compose` must run from the compose project directory

ANALYTICS_DIR=/root/analytics-data
OUT=$(mktemp)             # everything the three commands print, to find the last line afterwards

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
# Run a command with its output going to the log AND to $OUT; a failing command fails the script.
run() { log "\$ $*"; "$@" 2>&1 | tee -a "$OUT"; }

finish() {
  local rc=$? flag=--ok summary
  trap - EXIT
  # Last line of output, with any credential in a URL (https://TOKEN@host/...) masked.
  summary=$(grep -v '^[[:space:]]*$' "$OUT" | tail -n 1 | sed -E 's#://[^/@[:space:]]+@#://***@#g' | cut -c1-400 || true)
  if [ "$rc" -ne 0 ]; then flag=--fail; summary="${summary:-exit status $rc}"; else summary="${summary:-ok}"; fi
  log "customers-refresh: ${flag#--} (exit $rc): $summary"
  # Best-effort: a failed report must never change this script's exit status.
  docker compose exec -T voice-web python manage.py record_job_run customers-refresh "$flag" \
    --summary "$summary" --source cron \
    || log "(could not record this run on the dashboard Health page)"
  rm -f "$OUT"
  exit "$rc"
}
trap finish EXIT

run git -C "$ANALYTICS_DIR" pull -q
run docker compose cp "$ANALYTICS_DIR/data/customers.json" voice-web:/tmp/customers.json
run docker compose exec -T voice-web python manage.py import_customer_profiles --customers /tmp/customers.json
