#!/usr/bin/env bash
# Daily maintenance for the Happy Time stack. Installed as a host cron job on the VPS:
#
#   30 7 * * *  /root/happytime-budtender/scripts/daily-maintenance.sh >> /var/log/happytime-maintenance.log 2>&1
#
# TIMING: 07:30 America/Los_Angeles — 30 minutes before the earliest store opens (Yakima 08:00;
# Mt Vernon and Pullman 09:00). Everything below is a read/refresh of data callers will use that
# day, so it runs just before the doors open rather than in the middle of the night: the KB, the
# embeddings and the inventory are then fresh for the first call of the day instead of being
# eight hours old by opening. Nothing here needs to run while the stores are closed.
#
# WHY host cron and not Celery beat: the budtender service already has a beat container running
# inventory/transaction syncs, but the VOICE service has a worker and NO beat — so anything
# scheduled there would silently never fire. Rather than add a second beat container for four
# commands a day, this runs them directly. It is one file, greppable, and easy to change.
#
# Deliberately NOT `set -e`: every step must run even when an earlier one fails, otherwise one
# bad night silently skips the rest of maintenance. Failures are collected and reported at the end,
# and the script exits non-zero so cron mail / any log watcher sees a real signal.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
FAILED=()

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
step() {
  local name="$1"; shift
  log "── $name"
  if "$@"; then log "   ok: $name"; else log "   FAIL: $name"; FAILED+=("$name"); fi
}

dc() { docker compose "$@"; }

log "===== daily maintenance start ====="
log "commit: $(git log --oneline -1 2>/dev/null || echo unknown)"

# 1. Site content -> KB. The public site changes (hours, specials, new pages) without anyone
#    telling the agent, so re-scrape before re-embedding.
step "scrape site into KB" dc exec -T voice-web python manage.py scrape_happytime_site

# 2. Re-embed the KB. Retrieval quality depends on embeddings matching current KB text; a KB edit
#    made in the dashboard is invisible to semantic search until this runs.
step "reindex KB embeddings" dc exec -T voice-web python manage.py reindex_kb

# 3. Re-assert Vapi config from code. provision_vapi is idempotent and zero-drift by design — a
#    clean run reports all "nodrift". This exists because Vapi holds its OWN copy of every tool
#    schema: when TOOL_SPECS changes and nobody re-provisions, the PHONE agent silently keeps the
#    old schema and can no longer ask for things the chat agent can. That exact drift was found
#    in production on 2026-08-10 (faq_lookup + suggest_products both stale).
step "re-assert Vapi provisioning" dc exec -T voice-web python manage.py provision_vapi

# 4. Inventory freshness backstop. Celery beat syncs every 10 min; this is the "beat was down all
#    night" guard, and it is a cheap no-op when everything is already fresh.
step "inventory freshness backstop" dc exec -T web python manage.py sync_inventory

# 5. END-TO-END CANARY. Drives the REAL signed Vapi webhook and asserts every registered tool
#    still answers. This is the check that would have caught the pricing, pre-roll, indica and
#    category regressions — all of which passed unit tests while being broken in production.
#    Side-effecting tools (email/queue/n8n) stay opt-in and are not exercised here.
step "voice webhook canary (all tools)" \
  dc exec -T voice-web sh -c 'python text_smoke.py --url http://localhost:8000/api/voice/vapi --secret "$VAPI_WEBHOOK_SECRET" --store yakima'

# 6. Catalog drift alarm. Dutchie adds categories without warning; when it does, a category with
#    real in-stock product becomes unaskable by any caller and NOTHING else notices. pre-rolls —
#    the largest category in the store — sat unreachable exactly this way.
step "catalog category drift" dc exec -T web python manage.py shell -c "
from django.db.models import Count
from budtender.models import Product
from budtender.ranking import CATEGORY_BY_SLOTKEY
known = set(CATEGORY_BY_SLOTKEY.values())
rows = (Product.objects.filter(availability=True, quantity_on_hand__gt=0)
        .values('category').annotate(n=Count('id')).order_by('-n'))
missing = [(r['category'], r['n']) for r in rows if r['category'] and r['category'] not in known]
if missing:
    for c, n in missing:
        print(f'UNREACHABLE CATEGORY: {c} ({n} in stock)')
    raise SystemExit(1)
print(f'all {len(rows)} live categories reachable')
"

log "===== summary ====="
if [ ${#FAILED[@]} -eq 0 ]; then
  log "all steps ok"
  exit 0
fi
log "FAILED STEPS: ${FAILED[*]}"

# Tell a human. Without this the canary is a diary, not an alarm — a failure would sit in a log
# nobody reads until a customer hits the same bug. Goes to OPS_ALERT_EMAIL (the operator), NOT the
# per-store STAFF_ALERT_* addresses: a stale KB index or a drifted tool schema is an ops problem,
# and paging the shop floor about it trains everyone to ignore the alerts that DO matter to them.
# Best-effort and never fatal: a failed alert must not mask the underlying failure in the exit code.
log "sending failure alert"
dc exec -T voice-web python manage.py shell -c "
import os
from django.conf import settings
from django.core.mail import send_mail
to = os.environ.get('OPS_ALERT_EMAIL') or getattr(settings, 'STAFF_ALERT_EMAIL', '')
if not to:
    print('no OPS_ALERT_EMAIL / STAFF_ALERT_EMAIL configured — alert not sent')
else:
    send_mail(
        subject='[Happy Time] daily maintenance FAILED: ${FAILED[*]}',
        message='Failed steps: ${FAILED[*]}\n\nFull log on the VPS: /var/log/happytime-maintenance.log\n\nThe voice/chat agent may be serving stale or wrong answers until this is resolved.',
        # LEAD_EMAIL_FROM is what crm/sinks.py already sends staff alerts as, and it is a real
        # deliverable address. DEFAULT_FROM_EMAIL is NOT set in this project, so falling back to
        # it yields Django's 'webmaster@localhost' and the SMTP provider rejects the message
        # outright (550 Invalid \`from\` field) — a silently unsendable alert.
        from_email=getattr(settings, 'LEAD_EMAIL_FROM', 'bot@happytimeweed.com'),
        recipient_list=[to],
        fail_silently=True,
    )
    print(f'alert sent to {to}')
" || log "   (alert send failed — original failure still reported below)"

exit 1
