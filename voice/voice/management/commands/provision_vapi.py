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
from voice import caller, provision
from voice import constants as C
from voice.models import VapiObject


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
            "--per-store",
            action="store_true",
            help="Also provision one squad per store and attach each to that store's own number "
            "(VAPI_PHONE_NUMBER_STORE_MAP). This re-routes live calls — run --dry-run first.",
        )
        parser.add_argument(
            "--force-squad-id",
            action="store_true",
            help="When VAPI_SQUAD_ID disagrees with the squad id provision_vapi recorded, trust "
            "VAPI_SQUAD_ID: re-record it and PATCH only that squad (the run refuses otherwise).",
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
        if provision.single_mode_ready():
            self.stdout.write(
                "  (HHT_SQUAD_MODE=single: ONE concierge agent in a one-member squad, no handoffs; the "
                "old agents are left as they are in Vapi. Roll back: HHT_SQUAD_MODE=multi + provision_vapi)"
            )

        report = provision.provision_all(
            dry_run=dry_run,
            only=only,
            per_store=opts["per_store"],
            force_squad_id=opts["force_squad_id"],
        )

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
            self.stdout.write(self._dry_run_payloads(only, opts["per_store"]))

        if opts["verbose"]:
            self.stdout.write("")
            self.stdout.write("-- Report " + "-" * 51)
            self.stdout.write(json.dumps(report.to_dict(), indent=2))

        if report.errors:
            raise CommandError(
                f"{report.errors} object(s) failed to provision — see the report above."
            )

    # ── helpers ────────────────────────────────────────────────────────────────
    def _dry_run_payloads(self, only: str | None, opts_per_store: bool = False) -> str:
        """Build + dump the full JSON bodies (redacted) so the operator sees exactly what would be
        sent. Mirrors the provision_all order: tool → assistant → squad → phone."""
        blocks: list[str] = []
        dynamic = caller.dynamic_greeting()
        if dynamic:
            blocks.append(
                "# HHT_DYNAMIC_GREETING is ON: the inbound number(s) are unbound from the squad so Vapi\n"
                "# sends assistant-request, every assistant prompt ends with {{caller_context}}, and\n"
                "# entry_router/budtender gain remember_caller. Turn it off and re-run to roll back."
            )

        single = provision.single_mode_ready()
        if single:
            blocks.append(
                "# HHT_SQUAD_MODE=single: the squad becomes ONE member (concierge) with no\n"
                "# assistantDestinations. entry_router/budtender/faq/vendor/escalation are not touched in\n"
                "# Vapi (kept for rollback: HHT_SQUAD_MODE=multi, then provision_vapi)."
            )
        tools = ["faq_lookup"] + (["remember_caller"] if dynamic else [])
        if single:
            tools = ["faq_lookup", *provision._tools_for_roles([C.CONCIERGE_ROLE])]
        if only in (None, "tool"):
            for name in tools:
                blocks.append(self._block(f"POST/PATCH /tool  ({name})", provision.build_tool_payload(name)))

        assistants = [(C.P0_ASSISTANT_ROLE, C.P0_ASSISTANT_NAME)] + ([("entry_router", "entry_router")] if dynamic else [])
        if single:
            assistants = [(C.CONCIERGE_ROLE, C.CONCIERGE_ROLE)]
        if only in (None, "assistant"):
            for role, name in assistants:
                payload, warnings = provision.build_assistant_payload(role, name=name)
                title = f"POST/PATCH /assistant  ({name})"
                if warnings:
                    title += f"   [warnings: {'; '.join(warnings)}]"
                blocks.append(self._block(title, payload))

        if only in (None, "squad"):
            # In a dry run the assistant has a synthetic id; show the single-member container shape.
            members = provision._p0_members() or {C.P0_ASSISTANT_ROLE: "dryrun-assistant"}
            if single:
                rec = VapiObject.objects.filter(kind="assistant", name=C.CONCIERGE_ROLE).first()
                members = {C.CONCIERGE_ROLE: (rec.vapi_id if rec and rec.vapi_id else "dryrun-assistant")}
            pinned = provision.pinned_squad_id()
            title = (
                f"PATCH /squad/{pinned}  (Happy Time Voice, VAPI_SQUAD_ID; never POST)"
                if pinned
                else "POST/PATCH /squad  (Happy Time Voice)"
            )
            blocks.append(self._block(title, provision.build_squad_payload(members)))
            if opts_per_store:
                for _key, slug in C.TRANSFER_STORES:
                    blocks.append(
                        self._block(
                            f"POST/PATCH /squad  ({provision.squad_name(slug)})",
                            provision.build_squad_payload(members, slug),
                        )
                    )

        if only in (None, "phone"):
            # The binding is what changes under HHT_DYNAMIC_GREETING: squadId null = Vapi asks us.
            stores = [None] + ([slug for _key, slug in C.TRANSFER_STORES] if opts_per_store else [])
            for store in stores:
                label, number_id = provision.phone_number_target(store)
                if number_id:
                    squad = VapiObject.objects.filter(kind="squad", name=provision.squad_name(store)).first()
                    squad_id = provision.pinned_squad_id(store) or (squad.vapi_id if squad else "<squad id>")
                    payload = provision.phone_number_payload(label, squad_id)
                    blocks.append(self._block(f"PATCH /phone-number/{number_id}  ({label})", payload))

        return "\n\n".join(blocks)

    @staticmethod
    def _block(title: str, payload: dict) -> str:
        body = json.dumps(vapi.redact_payload(payload), indent=2)
        return f"# {title}\n{body}"
