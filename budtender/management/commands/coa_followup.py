"""Run the New Drops COA follow-up now (what the :15 / :45 beat task does).

    python manage.py coa_followup                  # all stores
    python manage.py coa_followup --store yakima
    python manage.py coa_followup --max-lookups 200

Re-asks Dutchie for every recent drop still without a COA link, rebuilds the snapshot, and prints the
before/after COA counts per store.
"""
from django.core.management.base import BaseCommand, CommandError

from budtender import backoffice_lock, new_drops
from budtender.models import STORES


class Command(BaseCommand):
    help = "Re-check COA links for recent New Drops and rebuild the snapshot."

    def add_arguments(self, parser):
        parser.add_argument("--store", choices=[s[0] for s in STORES])
        parser.add_argument("--max-lookups", type=int, default=new_drops.FOLLOWUP_MAX_LOOKUPS,
                            help="Dutchie lookups per store (paced under the 60/min limit)")

    def handle(self, *args, store=None, max_lookups=new_drops.FOLLOWUP_MAX_LOOKUPS, **opts):
        token = backoffice_lock.acquire(3 * 3600)
        if not token:
            raise CommandError("a New Drops refresh or another backoffice job is already running")
        try:
            for slug in [store] if store else [s[0] for s in STORES]:
                r = new_drops.coa_followup(slug, max_lookups=max_lookups)
                self.stdout.write(f"{slug}: {r['coa_before']} -> {r['coa_after']} of {r['products']} products "
                                  f"have a COA link (+{r['gained']})")
                self.stdout.flush()
        finally:
            backoffice_lock.release(token)
