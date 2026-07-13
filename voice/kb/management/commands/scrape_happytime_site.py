"""Scrape happytimeweed.com into the voice KB.

Cron example:
0 3 * * * cd /app && python manage.py scrape_happytime_site
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Scrape happytimeweed.com, validate KB rows, reindex, and publish when safe."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-publish",
            action="store_true",
            help="Apply + reindex but skip assistant/Vapi publish.",
        )
        parser.add_argument(
            "--path",
            action="append",
            default=None,
            help="Limit scrape to one path; may be passed more than once.",
        )

    def handle(self, *args, **opts):
        from kb.site_scrape import run_scrape

        run = run_scrape(publish=not opts["no_publish"], paths=opts["path"])
        msg = f"Scrape #{run.pk}: {run.status} - {run.summary}"
        if run.status == "applied":
            self.stdout.write(self.style.SUCCESS(msg))
        elif run.status == "blocked":
            self.stdout.write(self.style.WARNING(msg))
        else:
            self.stdout.write(self.style.ERROR(msg))
        if run.validation_errors:
            for err in run.validation_errors:
                self.stdout.write(f"- {err}")
