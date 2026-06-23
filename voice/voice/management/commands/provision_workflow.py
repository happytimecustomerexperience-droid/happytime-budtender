"""``python manage.py provision_workflow`` — deploy the guided-questionnaire Vapi Workflow (ADR-023).

Idempotent create-or-update by name (``find_workflow_by_name`` is the key, like the squad's
``find_*_by_name``). The SQUAD keeps answering the live number — this command does NOT touch the
phone unless you pass ``--attach`` (A/B; zero risk to what's live). ``--rollback`` re-attaches the
squad.

  --dry-run   build + print the payload (redacted), no Vapi writes (auto when VAPI_PRIVATE_KEY unset)
  --attach    cut the inbound number over to the workflow (clears squadId; squad still exists)
  --rollback  re-attach the squad to the inbound number (undo --attach)
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services import vapi
from voice import constants as C
from voice import workflow


class Command(BaseCommand):
    help = "Provision the guided-questionnaire Vapi Workflow (ADR-023). Squad stays live unless --attach."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Build + print, no Vapi writes.")
        parser.add_argument(
            "--attach",
            action="store_true",
            help="Route the inbound number to the workflow (clears the squad; squad kept as fallback).",
        )
        parser.add_argument(
            "--rollback",
            action="store_true",
            help="Re-attach the squad to the inbound number (undo --attach).",
        )

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"] or not vapi.configured()

        if opts["rollback"]:
            return self._rollback(dry_run)

        payload, warnings = workflow.build_workflow_payload()
        problems = workflow.validate_payload(payload)
        if problems:
            raise CommandError("workflow payload is malformed: " + "; ".join(problems))

        self.stdout.write(
            f'Workflow "{workflow.WORKFLOW_NAME}": '
            f'{len(payload["nodes"])} nodes, {len(payload["edges"])} edges'
        )
        for w in warnings:
            self.stdout.write(self.style.WARNING(f"  warning: {w}"))

        if dry_run:
            self.stdout.write(self.style.WARNING("  (dry-run — no Vapi writes)"))
            self.stdout.write(json.dumps(vapi.redact_payload(payload), indent=2))
            return

        existing = vapi.find_workflow_by_name(workflow.WORKFLOW_NAME)
        if existing and existing.get("id"):
            wf = vapi.patch_workflow(existing["id"], payload)
            action = "updated"
        else:
            wf = vapi.create_workflow(payload)
            action = "created"
        wid = (wf or {}).get("id", "")
        self.stdout.write(self.style.SUCCESS(f"workflow {action}: {wid}"))

        if opts["attach"]:
            self._attach(wid)
        else:
            self.stdout.write(
                "Squad still answers the live number. Re-run with --attach to cut over (A/B)."
            )

    def _attach(self, workflow_id: str):
        number_id = getattr(settings, "VAPI_PHONE_NUMBER_ID", "") or ""
        if not number_id:
            raise CommandError("VAPI_PHONE_NUMBER_ID unset — cannot attach the workflow to a number.")
        vapi.patch_phone_number(
            number_id, {"workflowId": workflow_id, "squadId": None, "assistantId": None}
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Inbound number now routed to the WORKFLOW (squad detached, still exists as fallback)."
            )
        )

    def _rollback(self, dry_run: bool):
        from voice.models import VapiObject

        squad = VapiObject.objects.filter(kind="squad", name=C.SQUAD_NAME).first()
        if not (squad and squad.vapi_id):
            raise CommandError("no provisioned squad found to roll back to (run provision_vapi first).")
        number_id = getattr(settings, "VAPI_PHONE_NUMBER_ID", "") or ""
        if not number_id:
            raise CommandError("VAPI_PHONE_NUMBER_ID unset — nothing to roll back.")
        if dry_run:
            self.stdout.write(self.style.WARNING(f"(dry-run) would re-attach squad {squad.vapi_id}"))
            return
        vapi.patch_phone_number(
            number_id, {"squadId": squad.vapi_id, "workflowId": None, "assistantId": None}
        )
        self.stdout.write(self.style.SUCCESS("Rolled back: squad re-attached to the inbound number."))
