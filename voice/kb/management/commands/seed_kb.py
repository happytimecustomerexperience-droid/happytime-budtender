"""Seed the voice knowledge base (idempotent). 22-SPEC-kb-seed.md §7 / 10-P0 §4.7.

  python manage.py seed_kb              # kb.seed.seed_all()
  python manage.py seed_kb --reindex    # also semantic.reindex() + vapi_files.mirror_all()

Safe to re-run — every block is update_or_create by a natural key (acceptance D1).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Seed the voice knowledge base (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reindex",
            action="store_true",
            help="After seeding, rebuild the cosine cache + mirror the KB to Vapi Files.",
        )

    def handle(self, *args, **opts):
        from kb import seed

        counts = seed.seed_all()
        total = sum(counts.values())
        self.stdout.write(
            self.style.SUCCESS(
                "Seeded KB: "
                + ", ".join(f"{k}={v}" for k, v in counts.items())
                + f" (total {total} rows touched)."
            )
        )

        if opts["reindex"]:
            from kb import semantic, vapi_files

            n = semantic.reindex()
            self.stdout.write(self.style.SUCCESS(f"Reindexed: {n} chunks."))
            mirror = vapi_files.mirror_all()
            if "skipped" in mirror:
                self.stdout.write(f"Vapi mirror skipped ({mirror['skipped']}).")
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Mirrored {len(mirror['files'])} files, tool {mirror['tool_id']}."
                    )
                )
