"""Delete shop visits (and their cascaded events) older than N days.

Retention is indefinite by default — this command is unused unless a policy is adopted;
then cron it, e.g. `python manage.py purge_visits --days 365`.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from customers.models import ShopVisit


class Command(BaseCommand):
    help = "Delete ShopVisit rows (and cascaded ShopEvents) older than --days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, required=True,
                            help="Delete visits started more than this many days ago.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report the count without deleting.")

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timezone.timedelta(days=opts["days"])
        qs = ShopVisit.objects.filter(started_at__lt=cutoff)
        n = qs.count()
        if opts["dry_run"]:
            self.stdout.write(f"Would delete {n} visit(s) older than {opts['days']}d.")
            return
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {n} visit(s) older than {opts['days']}d."))
