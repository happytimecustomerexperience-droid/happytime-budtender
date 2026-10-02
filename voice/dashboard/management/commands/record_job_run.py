"""Report one finished run of a host job to the dashboard Health page.

A host cron script cannot reach the Celery signals, so it calls this once at its end:

    python manage.py record_job_run daily-maintenance --fail --summary "FAILED: scrape site into KB" --source cron
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from dashboard.models import JobRun


class Command(BaseCommand):
    help = "Record a finished job run (ok or failed) so it shows on /dashboard/health/."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Job name, e.g. daily-maintenance")
        outcome = parser.add_mutually_exclusive_group(required=True)
        outcome.add_argument("--ok", action="store_true", help="The run succeeded.")
        outcome.add_argument("--fail", action="store_true", help="The run failed.")
        parser.add_argument("--summary", default="", help="One line: what happened (500 chars max).")
        parser.add_argument("--source", choices=["cron", "manual"], default="manual")

    def handle(self, *args, **opts):
        if not opts["name"].strip() or len(opts["name"]) > 64:
            raise CommandError("name must be 1-64 characters")
        run = JobRun.record(
            opts["name"], ok=opts["ok"], summary=opts["summary"], source=opts["source"]
        )
        self.stdout.write(f"recorded {run}")
