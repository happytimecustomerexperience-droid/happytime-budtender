"""Close idle open visits as abandoned (cron this every ~15 min).

An open ShopVisit whose last activity is older than --minutes (default 45) is a walkaway:
mark it abandoned and log an `abandon` event, so the "active now" panel and the abandon
rate stay honest instead of showing sessions that ended hours ago as still live.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from customers.models import ShopEvent, ShopVisit


class Command(BaseCommand):
    help = "Close stale (idle) open visits as abandoned."

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes", type=int, default=45,
            help="Idle threshold in minutes (no event newer than this -> abandoned).")

    def handle(self, *args, minutes, **opts):
        cutoff = timezone.now() - timezone.timedelta(minutes=minutes)
        closed = 0
        for v in ShopVisit.objects.filter(ended_at__isnull=True, started_at__lt=cutoff):
            last = v.events.order_by("-at").values_list("at", flat=True).first()
            if last and last >= cutoff:
                continue  # recent activity — still a live session
            ShopEvent.objects.create(
                visit=v, kind="abandon", budtender=v.budtender,
                acct_id=v.acct_id, detail=f"idle >{minutes}m")
            v.ended_at = timezone.now()
            v.outcome = "abandoned"
            v.save(update_fields=["ended_at", "outcome"])
            closed += 1
        self.stdout.write(f"closed {closed} stale visit(s) (idle >{minutes}m)")
