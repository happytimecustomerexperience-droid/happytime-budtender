"""A chat session's token is the credential its visitor's browser writes with.

The staff history list must not hand those out, and the endpoints that take a token
(persist, product search) must not create or address a session under a string the
caller invented.
"""
import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from budtender.models import ChatMessage, ChatSession

TOKEN = "test-token"
LIVE_TOKEN = "s-victim-0123456789abcdefghijklmnop"


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class TranscriptKeyTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.session = ChatSession.objects.create(
            session_token=LIVE_TOKEN, location_slug="yakima", channel="chat", phone="+15095551234"
        )
        ChatMessage.objects.create(session=self.session, role="user", content="private question")
        ChatMessage.objects.create(session=self.session, role="assistant", content="private answer")

    def _post(self, path, payload):
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )

    # -- the list hands out ids, never tokens ------------------------------------------------

    def test_history_list_never_contains_a_session_token(self):
        r = self._post("/api/v1/chat/history", {"limit": 10})

        self.assertEqual(r.status_code, 200)
        self.assertNotIn(LIVE_TOKEN, r.content.decode())
        row = r.json()["sessions"][0]
        self.assertEqual(row["id"], self.session.pk)
        self.assertNotIn("session_token", row)

    def test_the_id_from_the_list_opens_that_transcript_without_echoing_the_token(self):
        listed = self._post("/api/v1/chat/history", {"limit": 10}).json()["sessions"][0]

        r = self._post("/api/v1/chat/history", {"id": listed["id"], "message_limit": 50})

        self.assertEqual(r.status_code, 200)
        self.assertNotIn(LIVE_TOKEN, r.content.decode())
        sessions = r.json()["sessions"]
        self.assertEqual([s["id"] for s in sessions], [self.session.pk])
        self.assertEqual([m["content"] for m in sessions[0]["messages"]], ["private question", "private answer"])

    def test_an_unknown_id_opens_nothing(self):
        r = self._post("/api/v1/chat/history", {"id": 999999})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sessions"], [])

    def test_a_bad_id_does_not_fall_back_to_the_list(self):
        r = self._post("/api/v1/chat/history", {"id": "not-a-number"})

        self.assertEqual(r.json()["sessions"], [])
        self.assertNotIn("private", r.content.decode())

    def test_the_visitors_own_token_still_opens_their_transcript_but_is_not_echoed(self):
        r = self._post("/api/v1/chat/history", {"session_token": LIVE_TOKEN})

        sessions = r.json()["sessions"]
        self.assertEqual(sessions[0]["messages"][0]["content"], "private question")
        self.assertNotIn("session_token", sessions[0])

    # -- persist and product search apply the minted-token rule -----------------------------

    def test_persist_will_not_create_a_session_under_a_caller_chosen_token(self):
        r = self._post("/api/v1/chat/persist/", {
            "session_token": "victim-token", "messages": [{"role": "user", "content": "planted"}],
        })

        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json(), {"ok": False})
        self.assertFalse(ChatSession.objects.filter(session_token="victim-token").exists())
        self.assertFalse(ChatMessage.objects.filter(content="planted").exists())

    def test_persist_still_snapshots_a_server_minted_token(self):
        r = self._post("/api/v1/chat/persist/", {
            "session_token": "s-fresh-minted", "messages": [{"role": "user", "content": "kept"}],
        })

        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(ChatMessage.objects.get(session__session_token="s-fresh-minted").content, "kept")

    def test_persist_still_updates_a_session_we_already_know_even_with_an_older_token_shape(self):
        ChatSession.objects.create(session_token="legacy1", location_slug="yakima", channel="chat")

        r = self._post("/api/v1/chat/persist/", {
            "session_token": "legacy1", "messages": [{"role": "user", "content": "snapshot"}],
        })

        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(ChatMessage.objects.get(session__session_token="legacy1").content, "snapshot")

    def test_product_search_answers_but_creates_no_session_for_a_caller_chosen_token(self):
        with patch("budtender.views.rank_products", return_value=[]), patch(
            "budtender.views.inventory_is_stale", return_value=False
        ):
            r = self._post("/api/v1/products/search/", {
                "slots": {"store": "yakima", "category": "flower"}, "session_token": "victim-token",
            })

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"], [])
        self.assertFalse(ChatSession.objects.filter(session_token="victim-token").exists())

    def test_product_search_still_records_for_a_server_minted_token(self):
        with patch("budtender.views.rank_products", return_value=[]), patch(
            "budtender.views.inventory_is_stale", return_value=False
        ):
            self._post("/api/v1/products/search/", {
                "slots": {"store": "pullman"}, "session_token": "s-quiz-guest",
            })

        self.assertEqual(ChatSession.objects.get(session_token="s-quiz-guest").channel, "questionnaire")

    # -- resume-by-phone is left alone: pin what it returns ----------------------------------

    def test_resume_by_phone_returns_only_the_token_transcript_and_profile_summary_it_always_did(self):
        r = self._post("/api/v1/chat/resume-by-phone", {"phone": "+15095551234"})

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(
            set(body),
            {"resumed", "session_token", "stage", "slots", "messages", "prior_suggestions", "profile_summary"},
        )
        self.assertNotIn("+15095551234", r.content.decode())
