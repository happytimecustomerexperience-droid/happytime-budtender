"""
Reset BEHAVIORAL analytics to a clean slate.

Wipes chatbot/menu activity — analytics events, chat sessions, message
transcripts, suggestion impressions, and feedback — so the dashboard reflects
only real, post-reset usage. KEEPS:
  * CustomerProfile — purchase-derived personalization, built from Dutchie
    TRANSACTIONS (not chatbot activity); wiping it would throw away real history.
  * Product — live inventory.

Dry run by default (prints counts, changes nothing). Pass --yes to actually wipe.

    python manage.py reset_analytics          # dry run
    python manage.py reset_analytics --yes     # delete
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from budtender.models import (AnalyticsEvent, ChatMessage, ChatSession,
                              CustomerProfile, Feedback, Product,
                              SuggestedProduct)

# Child-first, so explicit deletes never hit a ProtectedError regardless of the
# FK on_delete policy (ChatMessage/SuggestedProduct reference ChatSession).
WIPE = [
    ("ChatMessage", ChatMessage),
    ("SuggestedProduct", SuggestedProduct),
    ("Feedback", Feedback),
    ("ChatSession", ChatSession),
    ("AnalyticsEvent", AnalyticsEvent),
]
KEEP = [("CustomerProfile", CustomerProfile), ("Product", Product)]


class Command(BaseCommand):
    help = ("Reset behavioral analytics (events/sessions/transcripts/"
            "suggestions/feedback). Keeps customer profiles + inventory.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually delete. Without this flag the command is a dry run.")

    def handle(self, *args, **opts):
        counts = {name: model.objects.count() for name, model in WIPE}
        total = sum(counts.values())

        self.stdout.write("Behavioral analytics rows to wipe:")
        for name, _ in WIPE:
            self.stdout.write(f"  {name:<18}{counts[name]:>9}")
        self.stdout.write(f"  {'TOTAL':<18}{total:>9}")
        self.stdout.write("Keeping untouched:")
        for name, model in KEEP:
            self.stdout.write(f"  {name:<18}{model.objects.count():>9}")

        if not opts["yes"]:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing deleted. Re-run with --yes to wipe."))
            return

        with transaction.atomic():
            for name, model in WIPE:
                deleted, _ = model.objects.all().delete()
                self.stdout.write(self.style.SUCCESS(f"Deleted {name}: {deleted}"))

        remaining = sum(model.objects.count() for _, model in WIPE)
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Behavioral tables now hold {remaining} rows. "
            f"CustomerProfile={CustomerProfile.objects.count()} preserved, "
            f"Product={Product.objects.count()} preserved."))
