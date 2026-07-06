"""Pre-warm the per-store inventory cache so customer requests never pay the slow
product_SearchV2 pull. Run on a short loop (warmer sidecar) under a shared Redis
cache so all web workers serve from the warm entry.

    python manage.py warm_menu
"""

from django.core.management.base import BaseCommand

from pos import catalog
from dutchie.stores import load_stores


class Command(BaseCommand):
    help = "Refresh the inventory cache for every configured store."

    def handle(self, *args, **opts):
        for name in load_stores():
            try:
                n = len(catalog.get_inventory(name, force=True))
                self.stdout.write(self.style.SUCCESS(f"{name}: {n} items cached"))
            except Exception as exc:
                self.stderr.write(f"{name}: warm failed — {exc}")
