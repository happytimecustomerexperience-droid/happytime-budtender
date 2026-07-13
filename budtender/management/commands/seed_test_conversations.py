"""Replay authored test conversations through the REAL chat endpoint so they are
stored (ChatSession/ChatMessage) + classified + tracked (AnalyticsEvent + sticky
primary_intent) exactly like production traffic — then print the analytics
breakdown. Used both as a dev seeder and by test_hundred_conversations.py.

    uv run python manage.py seed_test_conversations           # into the configured DB
    uv run python manage.py seed_test_conversations --dry-run # print breakdown only

The Gemini/voice reply is stubbed so the run is offline + deterministic; intent
classification is message-based and does not depend on the reply text.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import BaseCommand
from django.test import Client

from budtender.intents import conversation_breakdown, intent_breakdown
from budtender.models import AnalyticsEvent, ChatSession

DATA = Path(__file__).resolve().parents[2] / "tests" / "data" / "conversations.json"
_STUB_REPLY = "Thanks — happy to help with that. What else can I get you?"


def load_scenarios(path: Path | str = DATA) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["scenarios"]


def intended_primary(intents) -> str:
    """The conversation's expected primary_intent from its per-turn intents, using
    the SAME sticky rule the endpoint applies (escalation dominates, else the
    first real/non-greeting intent). Mirrors views._promote_intent."""
    primary = ""
    for intent in intents:
        if primary == "conflict_resolution":
            break
        if intent == "conflict_resolution" or not primary or primary == "greeting_other":
            primary = intent
    return primary


def run_scenarios(scenarios: list[dict], *, token: str, prefix: str = "test-") -> list[dict]:
    """Replay each scenario through /api/v1/chat/message. Returns, per scenario:
    {id, intended_primary_intent, primary_intent, turns:[{user, intended_intent, intent}]}.
    `token` must equal settings.HHT_BACKEND_TOKEN."""
    client = Client()
    results: list[dict] = []
    with patch("budtender.views.generate_chat_reply", return_value=_STUB_REPLY):
        for sc in scenarios:
            st = f"{prefix}{sc['id']}"
            turns = []
            for turn in sc["turns"]:
                resp = client.post(
                    "/api/v1/chat/message",
                    data=json.dumps({
                        "session_token": st,
                        "message": turn["user"],
                        "location": sc.get("store", "yakima"),
                    }),
                    content_type="application/json",
                    HTTP_AUTHORIZATION=f"Bearer {token}",
                    HTTP_HOST="127.0.0.1",  # allowed in prod settings + tests (testserver isn't)
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"{sc['id']}: HTTP {resp.status_code} {resp.content!r}")
                turns.append({
                    "user": turn["user"],
                    "intended_intent": turn.get("intended_intent"),
                    "intent": resp.json()["intent"],
                })
            session = ChatSession.objects.get(session_token=st)
            results.append({
                "id": sc["id"],
                "intended_primary_intent": intended_primary(t["intended_intent"] for t in sc["turns"]),
                "primary_intent": session.primary_intent,
                "turns": turns,
            })
    return results


class Command(BaseCommand):
    help = "Seed + classify + track authored test conversations, then print analytics."

    def add_arguments(self, parser):
        parser.add_argument("--path", default=str(DATA))
        parser.add_argument("--dry-run", action="store_true", help="do not keep rows")

    def handle(self, *args, **opts):
        token = getattr(settings, "HHT_BACKEND_TOKEN", "") or "seed-token"
        settings.HHT_BACKEND_TOKEN = token
        scenarios = load_scenarios(opts["path"])
        results = run_scenarios(scenarios, token=token)

        turns = sum(len(r["turns"]) for r in results)
        conv_hits = sum(r["primary_intent"] == r["intended_primary_intent"] for r in results)
        turn_hits = sum(t["intent"] == t["intended_intent"] for r in results for t in r["turns"])

        self.stdout.write(f"conversations: {len(results)}  turns: {turns}")
        self.stdout.write(f"conversation-intent match: {conv_hits}/{len(results)}")
        self.stdout.write(f"turn-intent match: {turn_hits}/{turns}")

        misses = [(r["id"], t["user"], t["intended_intent"], t["intent"])
                  for r in results for t in r["turns"] if t["intent"] != t["intended_intent"]]
        if misses:
            self.stdout.write("\nTurn misclassifications (authored intent vs classifier):")
            for cid, user, want, got in misses:
                self.stdout.write(f"  [{cid}] want={want} got={got}  {user!r}")
        self.stdout.write("\nBy conversation (primary_intent):")
        for row in conversation_breakdown(ChatSession.objects.filter(session_token__startswith="test-")):
            self.stdout.write(f"  {row['intent']:<22} {row['n']:>4}  {row['pct']:>3}%")
        self.stdout.write("\nBy turn:")
        for row in intent_breakdown(AnalyticsEvent.objects.filter(session_token__startswith="test-", event_type="chat_message")):
            self.stdout.write(f"  {row['intent']:<22} {row['n']:>4}  {row['pct']:>3}%")

        if opts["dry_run"]:
            ChatSession.objects.filter(session_token__startswith="test-").delete()
            AnalyticsEvent.objects.filter(session_token__startswith="test-").delete()
            self.stdout.write("\n(dry-run: test rows removed)")
