"""Mirror each store's current Dutchie deals into the Specials & hours rows (see kb.deals_sync).

    python manage.py sync_deals --dry-run   # print what would change per store; writes nothing
    python manage.py sync_deals             # apply (needs the "Sync deals from Dutchie" switch on)

``--dry-run`` only reads, so it runs even while the switch is off — use it to preview before turning
the switch on.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from kb import deals_sync


class Command(BaseCommand):
    help = "Sync Dutchie online-menu deals into the Specials & hours rows."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="print planned changes; write nothing")

    def handle(self, *args, dry_run=False, **options):
        result = deals_sync.sync_deals(dry_run=dry_run)
        if "skipped" in result:
            self.stdout.write(f"skipped: {result['skipped']}")
            return
        for store, c in result.items():
            if "skipped" in c:
                self.stdout.write(f"{store}: skipped ({c['skipped']}) — left untouched")
                continue
            summary = f"{c['created']} created, {c['updated']} updated, {c['deactivated']} deactivated"
            self.stdout.write(f"{store}: {summary}")
            for line in c.get("changes", []):
                self.stdout.write(f"    {line}")
