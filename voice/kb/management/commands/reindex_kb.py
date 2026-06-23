"""Rebuild the KB cosine cache (and optionally re-mirror the Vapi Files). 22-SPEC §4.6.

  python manage.py reindex_kb            # semantic.reindex() → "{n} chunks reindexed"
  python manage.py reindex_kb --mirror   # also kb.vapi_files.mirror_all()

The dashboard "Reindex" button (P4) calls the same semantic.reindex() + vapi_files.mirror_all()
pair. Bounded work (the corpus is dozens of rows) → runs inline.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Rebuild the KB cosine cache; --mirror also re-pushes the Vapi Files mirror."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mirror",
            action="store_true",
            help="Also mirror the curated KB to Vapi Files + attach the Query Tool.",
        )

    def handle(self, *args, **opts):
        from kb import semantic

        n = semantic.reindex()
        self.stdout.write(self.style.SUCCESS(f"{n} chunks reindexed."))

        if opts["mirror"]:
            from kb import vapi_files

            mirror = vapi_files.mirror_all()
            if "skipped" in mirror:
                self.stdout.write(f"Vapi mirror skipped ({mirror['skipped']}).")
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{len(mirror['files'])} files mirrored, tool {mirror['tool_id']}."
                    )
                )
