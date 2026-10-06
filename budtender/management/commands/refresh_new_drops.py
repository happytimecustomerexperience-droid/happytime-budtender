"""Build the New Drops snapshot now (ignores the store-hours gate).

    python manage.py refresh_new_drops            # all stores
    python manage.py refresh_new_drops --store yakima
    python manage.py refresh_new_drops --max-lookups 150   # what the 30-min task does
"""
from django.core.management.base import BaseCommand, CommandError

from budtender import backoffice_lock, new_drops
from budtender.models import STORES


class Command(BaseCommand):
    help = "Refresh the New Drops snapshot (brands received in the last 20 days)."

    def add_arguments(self, parser):
        parser.add_argument("--store", choices=[s[0] for s in STORES])
        parser.add_argument("--max-lookups", type=int, default=5000,
                            help="lab lookups per store (paced ~50/min); the beat task uses 150")

    def handle(self, *args, store=None, max_lookups=5000, **opts):
        # Same lock as the 30-min task: two paced runs at once would double the
        # call rate and trip Dutchie's 60/min limit.
        token = backoffice_lock.acquire(3 * 3600)
        if not token:
            raise CommandError("a New Drops refresh or a lab warm-up is already running")
        try:
            for slug in [store] if store else [s[0] for s in STORES]:
                snap = new_drops.refresh_store(slug, max_lookups=max_lookups)
                n = sum(len(b["products"]) for b in snap["brands"])
                linked = sum(1 for b in snap["brands"] for p in b["products"] if p["menu_slug"])
                coa = sum(1 for b in snap["brands"] for p in b["products"] if p["coa_url"])
                self.stdout.write(f"{slug}: {len(snap['brands'])} brands, {n} products, "
                                  f"{linked} exact menu links, {coa} COAs")
                self.stdout.flush()
        finally:
            backoffice_lock.release(token)
