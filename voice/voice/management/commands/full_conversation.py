"""``manage.py full_conversation <call_id>`` — fetch the authoritative full conversation for a Vapi
call (transcript + tool-call timeline), persist it, and print it. The CLI side of the dashboard's
"Fetch full conversation" action (P6)."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from core.services import vapi
from voice import callfetch


class Command(BaseCommand):
    help = "Fetch + store the full conversation (transcript + tool calls) for a Vapi call id."

    def add_arguments(self, parser):
        parser.add_argument("call_id", help="The Vapi call id (call.id).")
        parser.add_argument("--json", action="store_true", help="Print the raw JSON result.")

    def handle(self, *args, **opts):
        if not vapi.configured():
            raise CommandError("VAPI_PRIVATE_KEY not configured — cannot reach Vapi.")
        try:
            out = callfetch.fetch_full_conversation(opts["call_id"])
        except vapi.VapiError as exc:
            raise CommandError(f"Vapi fetch failed: {exc}") from exc

        if opts["json"]:
            self.stdout.write(json.dumps(out, indent=2, default=str))
            return

        self.stdout.write(self.style.SUCCESS(f"Call {out['call_id']} — {len(out['tool_calls'])} tool call(s)"))
        if out["summary"]:
            self.stdout.write(f"\nSummary:\n{out['summary']}")
        self.stdout.write("\nTranscript:\n" + (out["transcript"] or "(none)"))
        for tc in out["tool_calls"]:
            self.stdout.write(f"\n→ {tc['name']}({json.dumps(tc['args'])}) = {json.dumps(tc['result'], default=str)}")
