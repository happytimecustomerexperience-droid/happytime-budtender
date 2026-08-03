"""Show and optionally apply the data-derived online-order cap.

    python manage.py calibrate_order_cap                 # every store, report only
    python manage.py calibrate_order_cap --store yakima  # one store
    python manage.py calibrate_order_cap --apply         # persist the new caps
    python manage.py calibrate_order_cap --days 180      # wider window

The cap bounds how large an unpaid online order can be. It is derived from the p99
of real completed basket totals rather than picked by hand — see
bundles/calibration.py. The weekly celery task `calibrate_order_caps` runs the
`--apply` path; this command is for looking at the distribution yourself.
"""
from django.core.management.base import BaseCommand

from budtender.models import STORES
from bundles import calibration


class Command(BaseCommand):
    help = "Report (and optionally apply) the online-order cap derived from real basket totals."

    def add_arguments(self, parser):
        parser.add_argument("--store", default="", help="One store slug; default is all.")
        parser.add_argument("--days", type=int, default=calibration.WINDOW_DAYS,
                            help=f"Look-back window (default {calibration.WINDOW_DAYS}).")
        parser.add_argument("--apply", action="store_true",
                            help="Persist the derived cap. Without this, report only.")

    def handle(self, *args, **o):
        stores = [o["store"]] if o["store"] else [s[0] for s in STORES]
        for slug in stores:
            dist = (calibration.calibrate(slug, o["days"]) if o["apply"]
                    else calibration.distribution(slug, o["days"]))
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{slug}"))
            if dist.get("error"):
                self.stdout.write(self.style.ERROR(f"  couldn't pull transactions: {dist['error']}"))
                continue
            sample = dist.get("sample", 0)
            if not sample:
                self.stdout.write("  no completed baskets in the window")
                continue
            self.stdout.write(f"  baskets   {sample}")
            for key in ("p50", "p90", "p95", "p99", "max"):
                if key in dist:
                    self.stdout.write(f"  {key:9s} ${dist[key]:.2f}")
            self.stdout.write(f"  live cap  ${calibration.cap_for(slug):.2f}")
            if o["apply"]:
                if dist.get("applied"):
                    self.stdout.write(self.style.SUCCESS(f"  applied   ${dist['applied']:.2f}"))
                else:
                    self.stdout.write(self.style.WARNING(f"  not applied — {dist.get('reason', '')}"))

        if not o["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nReport only. Re-run with --apply to persist, or let the weekly "
                "`calibrate_order_caps` task do it."))
