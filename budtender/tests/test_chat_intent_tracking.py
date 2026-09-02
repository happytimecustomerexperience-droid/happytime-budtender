"""Every website chat turn is classified + tracked: the turn intent lands in the
AnalyticsEvent props and the conversation gets a sticky primary_intent."""
import json

from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from budtender.models import AnalyticsEvent, ChatSession

TOKEN = "test-token"


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class ChatIntentTrackingTests(TestCase):
    def setUp(self):
        self.client = Client()

    def _post(self, payload):
        with patch(
            "budtender.views.generate_chat_reply_with_source",
            return_value=("ok, here you go", "brain", ""),
        ):
            return self.client.post(
                "/api/v1/chat/message",
                data=json.dumps(payload),
                content_type="application/json",
                **{"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"},
            )

    def _user_intents(self, token):
        return [
            e.props.get("intent")
            for e in AnalyticsEvent.objects.filter(session_token=token, event_type="chat_message")
            .order_by("ts", "id")
            if e.props.get("role") == "user"
        ]

    def test_product_turn_is_classified_and_tracked(self):
        r = self._post({"session_token": "s-prod", "message": "show me some indica flower under $30"})
        self.assertEqual(r.json()["intent"], "product_suggestion")  # surfaced to the client
        self.assertEqual(self._user_intents("s-prod"), ["product_suggestion"])
        self.assertEqual(ChatSession.objects.get(session_token="s-prod").primary_intent, "product_suggestion")

    def test_conflict_turn_sets_primary_intent(self):
        self._post({"session_token": "s-conf", "message": "my cart is broken and I want a refund"})
        self.assertEqual(self._user_intents("s-conf"), ["conflict_resolution"])
        self.assertEqual(ChatSession.objects.get(session_token="s-conf").primary_intent, "conflict_resolution")

    def test_primary_intent_is_sticky_and_escalation_dominates(self):
        self._post({"session_token": "s-multi", "message": "hi there"})
        self._post({"session_token": "s-multi", "message": "any specials today?"})
        self._post({"session_token": "s-multi", "message": "actually this is a scam, I am furious"})
        self._post({"session_token": "s-multi", "message": "and what are your hours"})
        self.assertEqual(
            self._user_intents("s-multi"),
            ["greeting_other", "specials", "conflict_resolution", "hours_location"],
        )
        # first real intent was specials, but a later conflict dominates and sticks.
        self.assertEqual(ChatSession.objects.get(session_token="s-multi").primary_intent, "conflict_resolution")

    def test_analytics_summary_surfaces_intent_breakdown(self):
        self._post({"session_token": "s-a", "message": "show me some flower"})
        self._post({"session_token": "s-b", "message": "my cart is broken, refund please"})
        r = self.client.post(
            "/api/v1/analytics/summary",
            data=json.dumps({"days": 30}),
            content_type="application/json",
            **{"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"},
        )
        self.assertEqual(r.status_code, 200)
        convo = r.json()["conversations"]
        turn = {row["intent"]: row["n"] for row in convo["by_turn_intent"]}
        conversation = {row["intent"]: row["n"] for row in convo["by_conversation_intent"]}
        self.assertEqual(turn.get("product_suggestion"), 1)
        self.assertEqual(turn.get("conflict_resolution"), 1)
        self.assertEqual(conversation.get("product_suggestion"), 1)
        self.assertEqual(conversation.get("conflict_resolution"), 1)
