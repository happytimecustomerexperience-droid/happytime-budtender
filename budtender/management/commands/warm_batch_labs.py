"""Fill the BatchLab (terpenes + % per in-stock batch) and ProductDetail (allowlisted product info)
tables now. Ignores the store-hours gate.

    python manage.py warm_batch_labs --dry-run                 # what it WOULD look up; no Dutchie call
    python manage.py warm_batch_labs --store yakima --limit 5  # a first small, paced real run
    python manage.py warm_batch_labs --what labs               # only the lab results (or: details, both)
    python manage.py warm_batch_labs --pause 1.0               # slower: extra seconds between calls

Paced and sequential through ONE login per store; stops after 3 identical consecutive failures and
prints the unresolved ids (exit code 1). Safe to re-run: only batches with no lab (or a 'none' older
than 7 days) and products with no detail checked in the last 7 days are asked.
"""
from django.core.management.base import BaseCommand, CommandError

from budtender import backoffice_lock, lab_enrich
from budtender.models import STORES


class Command(BaseCommand):
    help = "Warm the per-batch lab table and the per-product detail table from Dutchie, paced."

    def add_arguments(self, parser):
        parser.add_argument("--store", choices=[s[0] for s in STORES])
        parser.add_argument("--limit", type=int, default=lab_enrich.MAX_PER_RUN,
                            help="batches (and products) per store")
        parser.add_argument("--what", choices=lab_enrich.WARM_WHAT, default="both",
                            help="labs (terpenes/COA), details (product info) or both")
        parser.add_argument("--pause", type=float, default=None,
                            help="extra seconds between Dutchie calls (default BATCH_LAB_WARM_PAUSE or 0.5)")
        parser.add_argument("--dry-run", action="store_true", help="list what would be fetched; make no Dutchie call")

    def handle(self, *args, store=None, limit=lab_enrich.MAX_PER_RUN, what="both", pause=None, dry_run=False,
               **opts):
        stores = [store] if store else [s[0] for s in STORES]
        if dry_run:
            for slug in stores:
                out = lab_enrich.warm(slug, limit=limit, what=what, dry_run=True)
                self.stdout.write(f"{slug}: would look up {len(out['would_fetch'])} batches: "
                                  f"{', '.join(out['would_fetch']) or '-'}")
                if "would_fetch_details" in out:
                    self.stdout.write(f"{slug}: would look up {len(out['would_fetch_details'])} products: "
                                      f"{', '.join(out['would_fetch_details']) or '-'}")
            return
        # The same Dutchie budget as New Drops and the beat task: two paced runs at once would
        # double the call rate and trip the 60/min limit.
        token = backoffice_lock.acquire(3 * 3600)
        if not token:
            raise CommandError("a New Drops refresh or another lab warm-up is running; try again when it finishes")
        stopped = []
        try:
            for slug in stores:
                out = lab_enrich.warm(slug, limit=limit, pause=pause, what=what)
                line = f"{slug}: {out['ok']} labs, {out['none']} none, {out['failed']} failed"
                if "details" in out:
                    d = out["details"]
                    line += f"; details {d['ok']} ok, {d['none']} none, {d['failed']} failed"
                self.stdout.write(line + (" - STOPPED" if out["stopped"] else ""))
                self.stdout.flush()
                if out["stopped"]:
                    left = list(out["unresolved"]) + list(out.get("details", {}).get("unresolved", []))
                    stopped.append(f"{slug}: {', '.join(left)}")
        finally:
            backoffice_lock.release(token)
        if stopped:
            raise CommandError("stopped early; unresolved ids - " + "; ".join(stopped))
