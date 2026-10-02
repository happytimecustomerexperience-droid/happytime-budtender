"""One-shot health check for the website chat path.

Sends one message through the same bridge the website chat uses and says whether the shared
voice brain answered or the static floor line did. The chat never calls Gemini directly (that
was the unmetered raw fallback); the command name is kept because the deploy hint points here.
Run after wiring HHT_VOICE_BASE_URL / HHT_BACKEND_TOKEN, and on the deploy host:

    uv run python manage.py check_gemini
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from django.core.management.base import BaseCommand

from budtender.gemini_chat import generate_chat_reply_with_source


class Command(BaseCommand):
    help = "Live-check that the website chat reaches the shared voice brain."

    def add_arguments(self, parser):
        parser.add_argument("--message", default="What's good for relaxing after work? Keep it short.")
        parser.add_argument("--store", default="yakima")

    def handle(self, *args, **opts):
        self.stdout.write(
            f"voice={os.environ.get('HHT_VOICE_BASE_URL') or 'NOT SET'} "
            f"token={'set' if os.environ.get('HHT_BACKEND_TOKEN') else 'MISSING'}"
        )
        msgs = [SimpleNamespace(role="user", content=opts["message"])]
        reply, source, _intent = generate_chat_reply_with_source(msgs, store=opts["store"])
        if source == "brain":
            self.stdout.write(self.style.SUCCESS(f"OK — the brain answered:\n  {reply}"))
        else:
            self.stdout.write(self.style.WARNING(
                f"FLOOR — the brain did not answer (down, 429, or not configured); customers see:\n  {reply}"
            ))
