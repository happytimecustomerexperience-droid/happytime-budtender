"""Refresh the local inventory cache from Dutchie REST. Run via cron/Task Scheduler.

    python manage.py refresh_inventory           # all stores
    python manage.py refresh_inventory --store yakima
"""

from django.core.management.base import BaseCommand

from pos.services import refresh_inventory
from dutchie.stores import load_stores


class Command(BaseCommand):
    help = "Refresh local inventory cache from Dutchie REST read API."

    def add_arguments(self, parser):
        parser.add_argument("--store", default="")

    def handle(self, *args, **opts):
        stores = load_stores()
        if opts["store"]:
            stores = {opts["store"]: stores[opts["store"]]} if opts["store"] in stores else {}
        if not stores:
            self.stderr.write("no matching stores in stores.json")
            return
        for name, store in stores.items():
            n = refresh_inventory(store)
            self.stdout.write(self.style.SUCCESS(f"{name}: {n} items"))
