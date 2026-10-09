"""Seed the voice knowledge base (idempotent, create-only). 22-SPEC-kb-seed.md §7 / 10-P0 §4.7.

  python manage.py seed_kb              # kb.seed.seed_all(): insert MISSING rows only
  python manage.py seed_kb --refresh    # also overwrite existing rows with the code defaults
  python manage.py seed_kb --reindex    # also semantic.reindex() + vapi_files.mirror_all()

Safe to re-run — every block writes through ``kb.seed._seed`` by a natural key (acceptance D1). The
root docker-compose runs the plain form at every voice-web start, so it never overwrites an existing
row: owner dashboard edits (prompts, model, voice, greeting, hours, FAQ text) survive a restart.
``--refresh`` is the deliberate reset, and the way to deploy changed seed content to an existing DB.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Seed the voice knowledge base (idempotent; inserts missing rows, keeps existing ones)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--refresh",
            "--overwrite",
            action="store_true",
            dest="refresh",
            help="Overwrite EXISTING rows with the code defaults (resets dashboard edits to agent "
            "prompts/model/voice/greeting, store facts, FAQ/policy/education rows; deals are never "
            "touched). Default off: only missing rows are inserted.",
        )
        parser.add_argument(
            "--reindex",
            action="store_true",
            help="After seeding, rebuild the cosine cache + mirror the KB to Vapi Files.",
        )

    def handle(self, *args, **opts):
        from kb import seed

        refresh = opts["refresh"]
        counts = seed.seed_all(refresh=refresh)
        total = sum(counts.values())
        run = seed.LAST_RUN
        split = (
            f"created {run['created']}, refreshed {run['refreshed']} to code defaults"
            if refresh
            else f"created {run['created']}, kept {run['kept']} existing unchanged"
        )
        self.stdout.write(
            self.style.SUCCESS(
                ("Seeded KB (--refresh): " if refresh else "Seeded KB (create-only): ")
                + ", ".join(f"{k}={v}" for k, v in counts.items())
                + f" (total {total} rows; {split})."
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
