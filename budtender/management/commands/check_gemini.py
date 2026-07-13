"""One-shot health check for the Gemini/Vertex chat wiring.

Prints the active config and whether a live reply comes back (or why not). Run
after wiring Vertex env, and on the deploy host:

    uv run python manage.py check_gemini
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from django.core.management.base import BaseCommand

from budtender.gemini_chat import GeminiChatUnavailable, generate_chat_reply


class Command(BaseCommand):
    help = "Live-check the Gemini/Vertex chat reply path."

    def add_arguments(self, parser):
        parser.add_argument("--message", default="What's good for relaxing after work? Keep it short.")
        parser.add_argument("--store", default="yakima")

    def handle(self, *args, **opts):
        self.stdout.write(
            f"vertex={os.environ.get('GEMINI_USE_VERTEX')} "
            f"project={os.environ.get('GOOGLE_CLOUD_PROJECT')} "
            f"location={os.environ.get('GOOGLE_CLOUD_LOCATION', 'us-central1')} "
            f"model={os.environ.get('GEMINI_CHAT_MODEL', 'gemini-2.5-flash-lite')} "
            f"creds={'set' if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') else 'MISSING'}"
        )
        msgs = [SimpleNamespace(role="user", content=opts["message"])]
        try:
            reply = generate_chat_reply(msgs, store=opts["store"])
            self.stdout.write(self.style.SUCCESS(f"OK — live reply:\n  {reply}"))
        except GeminiChatUnavailable as exc:
            self.stdout.write(self.style.ERROR(f"UNAVAILABLE: {exc}"))
            if "invalid_grant" in str(exc) or "iat" in str(exc):
                self.stdout.write("  ^ host clock skew - the machine's time is off; fix NTP and retry.")
