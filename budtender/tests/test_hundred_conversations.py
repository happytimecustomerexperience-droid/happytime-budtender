"""100 full test conversations replayed end-to-end: stored, classified, tracked.

Hard invariants (must be exact): every conversation stored, every turn classified
into the taxonomy, every conversation marked with a primary_intent, analytics
counts reconcile, and the whole taxonomy is exercised. Classification *accuracy*
vs the human-intended labels is held to a high threshold (a deterministic
regex classifier is not expected to be perfect on adversarial phrasing).
"""
from __future__ import annotations

from django.test import TestCase, override_settings

from budtender.intents import INTENTS, conversation_breakdown, intent_breakdown
from budtender.management.commands.seed_test_conversations import load_scenarios, run_scenarios
from budtender.models import AnalyticsEvent, ChatMessage, ChatSession

TOKEN = "test-token"


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class HundredConversationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.scenarios = load_scenarios()
        cls.results = run_scenarios(cls.scenarios, token=TOKEN)

    # ── corpus shape ──────────────────────────────────────────────────────────
    def test_corpus_has_at_least_100_full_conversations(self):
        self.assertGreaterEqual(len(self.scenarios), 100)
        for sc in self.scenarios:
            self.assertTrue(sc.get("id") and sc.get("turns"), sc)
            self.assertGreaterEqual(len(sc["turns"]), 1, sc["id"])
            for t in sc["turns"]:
                self.assertTrue(str(t.get("user", "")).strip(), sc["id"])
                self.assertIn(t["intended_intent"], INTENTS, sc["id"])

    # ── stored ────────────────────────────────────────────────────────────────
    def test_every_conversation_is_stored_with_its_messages(self):
        self.assertEqual(ChatSession.objects.filter(session_token__startswith="test-").count(), len(self.scenarios))
        for sc in self.scenarios:
            session = ChatSession.objects.get(session_token=f"test-{sc['id']}")
            user_msgs = ChatMessage.objects.filter(session=session, role="user").count()
            self.assertEqual(user_msgs, len(sc["turns"]), sc["id"])

    # ── classified + marked ─────────────────────────────────────────────────────
    def test_every_conversation_is_marked_with_a_primary_intent(self):
        for r in self.results:
            self.assertIn(r["primary_intent"], INTENTS, r["id"])

    def test_every_turn_is_classified_in_the_taxonomy(self):
        events = AnalyticsEvent.objects.filter(session_token__startswith="test-", event_type="chat_message")
        user_events = [e for e in events if (e.props or {}).get("role") == "user"]
        total_turns = sum(len(sc["turns"]) for sc in self.scenarios)
        self.assertEqual(len(user_events), total_turns)
        for e in user_events:
            self.assertIn(e.props.get("intent"), INTENTS)

    # ── tracked / reconciles ────────────────────────────────────────────────────
    def test_analytics_breakdowns_reconcile(self):
        total_turns = sum(len(sc["turns"]) for sc in self.scenarios)
        turn_rows = intent_breakdown(
            AnalyticsEvent.objects.filter(session_token__startswith="test-", event_type="chat_message"))
        self.assertEqual(sum(r["n"] for r in turn_rows), total_turns)
        conv_rows = conversation_breakdown(ChatSession.objects.filter(session_token__startswith="test-"))
        self.assertEqual(sum(r["n"] for r in conv_rows), len(self.scenarios))

    def test_whole_taxonomy_is_exercised(self):
        seen = {t["intent"] for r in self.results for t in r["turns"]}
        self.assertEqual(seen, set(INTENTS), f"missing intents: {set(INTENTS) - seen}")

    # ── no leak (regression): analytics props never carry business internals ────
    def test_analytics_props_have_no_business_internals(self):
        forbidden = {"margin", "cost", "bucket", "margin_pct", "price_z"}
        for props in AnalyticsEvent.objects.filter(session_token__startswith="test-").values_list("props", flat=True):
            self.assertFalse(set(props or {}) & forbidden, props)

    # ── accuracy (thresholded, with visibility) ─────────────────────────────────
    def test_classification_accuracy_meets_threshold(self):
        conv_hits = [r["primary_intent"] == r["intended_primary_intent"] for r in self.results]
        turn_hits = [t["intent"] == t["intended_intent"] for r in self.results for t in r["turns"]]
        conv_acc = sum(conv_hits) / len(conv_hits)
        turn_acc = sum(turn_hits) / len(turn_hits)
        misses = [(r["id"], t["user"], t["intended_intent"], t["intent"])
                  for r in self.results for t in r["turns"] if t["intent"] != t["intended_intent"]]
        msg = "turn misses:\n" + "\n".join(f"  [{i}] {u!r} intended={a} got={g}" for i, u, a, g in misses[:40])
        self.assertGreaterEqual(turn_acc, 0.9, f"turn acc {turn_acc:.2%}\n{msg}")
        self.assertGreaterEqual(conv_acc, 0.9, f"conversation acc {conv_acc:.2%}")
