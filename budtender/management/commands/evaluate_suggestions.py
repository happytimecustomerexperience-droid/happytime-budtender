"""``manage.py evaluate_suggestions [--days N] [--apply]`` — re-evaluate suggestion outcomes on demand.

Undecided outcomes (pending / not_bought / unattributable) of known customers shown in the last N days
are checked against purchase history (certainty only: a stored purchase timestamp inside the window),
then expired windows are closed exactly like the hourly job. Dry run by default.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Re-evaluate suggestion outcomes from purchase history and close expired windows (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Suggestions shown in the last N days (default 30).")
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
        parser.add_argument("--dry-run", action="store_true", help="Explicit dry run (the default).")

    def handle(self, *args, **opts):
        from budtender import suggestions

        apply = bool(opts["apply"]) and not opts["dry_run"]
        days = min(max(int(opts["days"] or 30), 1), 3650)
        out = suggestions.evaluate(apply=apply, days=days)
        mode = "APPLIED" if apply else "DRY RUN (nothing written; pass --apply)"
        self.stdout.write(f"{mode}: " + ", ".join(f"{k}={v}" for k, v in out.items()))
