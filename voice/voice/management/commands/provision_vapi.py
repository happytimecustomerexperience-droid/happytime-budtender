"""``python manage.py provision_vapi`` — the single operator entry point (20-SPEC §3.3 / §6.3).

Stands up the Vapi stack from env (idempotent, re-runnable, zero drift — ADR-003) and prints a
per-object reconcile report. Exits non-zero on any hard error (``action="error"``).

``--dry-run`` prints the FULL JSON payloads + the planned create/patch list WITHOUT calling Vapi
(auto-engaged when ``VAPI_PRIVATE_KEY`` is unset). Secrets are redacted in every line (the dry-run
dump routes through the client's ``redact_payload``).
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from core.services import vapi
from voice import constants as C
from voice import provision


class Command(BaseCommand):
    help = "Idempotently provision the Vapi stack (assistants/squad/tools/files/phone) from env."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the full JSON payloads + planned calls WITHOUT calling Vapi "
            "(auto when VAPI_PRIVATE_KEY is unset).",
        )
        parser.add_argument(
            "--only",
            choices=["tool", "file", "assistant", "squad", "phone"],
            default=None,
            help="Reconcile only one object kind.",
        )
        parser.add_argument(
            "--verbose", action="store_true", help="Also print the full report JSON (redacted)."
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"] or not vapi.configured()
        only = opts["only"]

        self.stdout.write(f'Provisioning Vapi stack "{C.SQUAD_NAME}" ...')
        if dry_run:
            self.stdout.write(self.style.WARNING("  (dry-run — no Vapi writes will be issued)"))

        report = provision.provision_all(dry_run=dry_run, only=only)

        if report.error:
            raise CommandError(report.error)

        # Per-object reconcile report.
        for r in report.results:
            self.stdout.write(r.line())

        self.stdout.write(
            f"Done: created {report.created}, patched {report.patched}, "
            f"nodrift {report.nodrift}, skipped {report.skipped}, errors {report.errors}."
        )

        # Dry-run: dump the full redacted JSON payloads the run WOULD have sent.
        if dry_run:
            self.stdout.write("")
            self.stdout.write("-- Planned payloads (redacted) " + "-" * 30)
            self.stdout.write(self._dry_run_payloads(only))

        if opts["verbose"]:
            self.stdout.write("")
            self.stdout.write("-- Report " + "-" * 51)
            self.stdout.write(json.dumps(report.to_dict(), indent=2))

        if report.errors:
            raise CommandError(
                f"{report.errors} object(s) failed to provision — see the report above."
            )

    # ── helpers ────────────────────────────────────────────────────────────────
    def _dry_run_payloads(self, only: str | None) -> str:
        """Build + dump the full JSON bodies (redacted) so the operator sees exactly what would be
        sent. Mirrors the provision_all order: tool → assistant → squad → phone."""
        blocks: list[str] = []

        if only in (None, "tool"):
            blocks.append(
                self._block(
                    "POST/PATCH /tool  (faq_lookup)", provision.build_tool_payload("faq_lookup")
                )
            )

        if only in (None, "assistant"):
            payload, warnings = provision.build_assistant_payload(
                C.P0_ASSISTANT_ROLE, name=C.P0_ASSISTANT_NAME
            )
            title = "POST/PATCH /assistant  (entry_faq)"
            if warnings:
                title += f"   [warnings: {'; '.join(warnings)}]"
            blocks.append(self._block(title, payload))

        if only in (None, "squad"):
            # In a dry run the assistant has a synthetic id; show the single-member container shape.
            members = provision._p0_members() or {C.P0_ASSISTANT_ROLE: "dryrun-assistant"}
            blocks.append(
                self._block(
                    "POST/PATCH /squad  (Happy Time Voice)", provision.build_squad_payload(members)
                )
            )

        return "\n\n".join(blocks)

    @staticmethod
    def _block(title: str, payload: dict) -> str:
        body = json.dumps(vapi.redact_payload(payload), indent=2)
        return f"# {title}\n{body}"
