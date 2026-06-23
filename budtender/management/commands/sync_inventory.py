"""``python manage.py sync_inventory [--store <slug>]`` — pull live inventory from Dutchie.

The same work celery-beat runs every 10 minutes (``budtender.tasks.sync_inventory_all``), but
runnable ON DEMAND and from a plain host cron — a robust daily refresh that does NOT depend on
celery-beat/worker being up (the failure mode that left the Product table empty on deploy). Prints
per-store counts and exits non-zero if nothing synced, so a cron/healthcheck can alert.

Daily refresh (host crontab on the VPS), independent of celery:
    0 6 * * * cd /root/happytime-budtender && docker compose exec -T web \
        python manage.py sync_inventory >> /var/log/hht-sync.log 2>&1
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from budtender.tasks import sync_inventory, sync_inventory_all


class Command(BaseCommand):
    help = "Pull live inventory from Dutchie into the Product table (all stores, or one with --store)."

    def add_arguments(self, parser):
        parser.add_argument("--store", default=None, help="Sync only this store slug (else all stores).")

    def handle(self, *args, **opts):
        if opts["store"]:
            n = sync_inventory(opts["store"])
            self.stdout.write(self.style.SUCCESS(f"{opts['store']}: {n} products"))
            if not n:
                self.stderr.write("0 products — check the Dutchie POS key for this store + worker logs.")
                raise SystemExit(1)
            return

        counts = sync_inventory_all() or {}
        total = sum(int(v or 0) for v in counts.values()) if isinstance(counts, dict) else 0
        for store, n in (counts or {}).items():
            style = self.style.SUCCESS if n else self.style.WARNING
            self.stdout.write(style(f"{store}: {n} products"))
        self.stdout.write(f"total: {total}")
        if not total:
            self.stderr.write(
                "0 products synced across all stores — Dutchie POS keys in .env.dutchie are likely "
                "missing/expired, or the Dutchie API is unreachable. Check celery-worker logs."
            )
            raise SystemExit(1)
