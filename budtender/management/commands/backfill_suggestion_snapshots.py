"""``manage.py backfill_suggestion_snapshots [--apply]`` — suggestion-analytics-v1 backfill.

Rows written before v1 get a snapshot from the Product row when the SKU still exists (else a
``snapshot_partial`` stub), a channel, identity_via and an outcome. Outcomes are decided only where
``purchase_history`` proves a purchase inside the window; an expired window without that proof is
``unattributable`` — never guessed. Dry run by default; re-running after --apply finds nothing to do.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Backfill snapshots/outcomes for suggestions recorded before suggestion-analytics-v1 (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
        parser.add_argument("--dry-run", action="store_true", help="Explicit dry run (the default).")

    def handle(self, *args, **opts):
        from budtender import suggestions

        apply = bool(opts["apply"]) and not opts["dry_run"]
        stats = suggestions.backfill(apply=apply)
        mode = "APPLIED" if apply else "DRY RUN (nothing written; pass --apply)"
        self.stdout.write(f"{mode}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
