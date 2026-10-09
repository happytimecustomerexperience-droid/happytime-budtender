"""Trim the high-volume page-performance beacons. NEVER touches conversations or chat analytics.

Not scheduled: the owner decides when to run it on prod.

    python manage.py prune_site_noise --dry-run
    python manage.py prune_site_noise --days 90

The site-wide tracker also sends web-vitals, scroll depth, time-on-page and similar beacons: by far the
largest share of ``AnalyticsEvent`` rows, and of no use once the month is over. Only those exact event
types, older than ``--days`` (default 90), are deleted. Every chat event (``analytics.CHAT_EVENT_NAMES``),
conversion click, feedback, ``ChatSession``, ``ChatMessage`` and ``SuggestedProduct`` is kept forever.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from budtender.models import AnalyticsEvent

NOISE_EVENTS = ("web_vital", "performance", "scroll", "time_on_page", "user_properties_set")


class Command(BaseCommand):
    help = "Delete old web-vitals/scroll/time-on-page beacons (never chat events or conversations)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90, help="Older than this many days (default 90, min 7).")
        parser.add_argument("--dry-run", action="store_true", help="Count only.")

    def handle(self, *args, **opts):
        days = max(opts["days"], 7)
        qs = AnalyticsEvent.objects.filter(event_type__in=NOISE_EVENTS, ts__lt=timezone.now() - timedelta(days=days))
        n = qs.count()
        if opts["dry_run"]:
            self.stdout.write(f"Would delete {n} page-performance beacon(s) older than {days}d.")
            return
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {n} page-performance beacon(s) older than {days}d."))
