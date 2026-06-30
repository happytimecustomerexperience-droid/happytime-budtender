"""``manage.py rebuild_customer_history`` — wipe + rebuild every customer's purchase history from
the FULL Dutchie transaction history (P7).

Use this for the initial complete backfill, or to correct any count drift: it clears the cumulative
history + watermark, then re-folds the full lookback window exactly-once (idempotent, re-runnable).
After it runs, the recurring 6-hour sync only folds NEW transactions (watermark-gated), so totals
stay correct and history keeps growing without deleting or double-counting anything.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Reset + rebuild customer purchase history from the full Dutchie transaction history."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None,
                            help="Lookback days for the backfill (default HHT_TX_LOOKBACK_DAYS ≈ 20y).")

    def handle(self, *args, **opts):
        from budtender import tasks
        from budtender.models import STORES, CustomerProfile, SyncState

        # ALWAYS all stores. purchase_history is phone-keyed with NO per-store tag, so the wipe is
        # inherently global — a single-store rebuild would blank every customer's OTHER stores' buys
        # and never rebuild them (their watermarks wouldn't reset), causing permanent data loss.
        slugs = [s[0] for s in STORES]
        # Clean slate so the rebuild folds the full window exactly-once (no leftover over-count).
        CustomerProfile.objects.update(purchase_history=[], total_orders=0)
        SyncState.objects.all().update(last_tx_at=None, last_tx_ids=[])

        total = 0
        for slug in slugs:
            n = tasks.sync_transactions(slug, days=opts["days"], full=True)
            total += n
            self.stdout.write(self.style.SUCCESS(f"{slug}: rebuilt {n} customer histories"))
        self.stdout.write(self.style.SUCCESS(f"Done — {total} customer histories rebuilt across {len(slugs)} store(s)."))
