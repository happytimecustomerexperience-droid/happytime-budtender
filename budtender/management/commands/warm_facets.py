"""
Precompute the questionnaire FACETS (subtypes / sizes / price-bands) into the
Redis cache for every store, so the chatbot's steps load INSTANTLY and one
container serves many concurrent users without re-scanning inventory per request.

Runs at container STARTUP (see the web service command in docker-compose.yml) so
the cache is never cold after a `docker compose up`/restart, and is ALSO invoked
on every inventory sync (tasks.sync_inventory). Best-effort: a failure to warm one
store never blocks the others or the server from starting.
"""
import time

from django.core.management.base import BaseCommand

from budtender.facets import warm
from budtender.tasks import STORE_SLUGS


class Command(BaseCommand):
    help = "Precompute questionnaire facets (subtypes/sizes/price-bands) for every store."

    def handle(self, *args, **opts):
        for slug in STORE_SLUGS:
            t0 = time.monotonic()
            try:
                n = warm(slug)
                ms = round((time.monotonic() - t0) * 1000)
                self.stdout.write(self.style.SUCCESS(
                    f"warmed {slug}: {n} facet entries in {ms}ms"))
            except Exception as e:  # never fail startup over a warm
                self.stdout.write(self.style.WARNING(f"warm {slug} skipped: {e}"))
