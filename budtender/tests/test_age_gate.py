"""An "I'm 19" from an earlier turn of a website chat keeps the retail decline on later turns."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import Client, TestCase, override_settings

from budtender import age_gate
from budtender.models import AnalyticsEvent, ChatMessage, ChatSession

TOKEN = "test-token"
SESSION = "s-agegate-session0"


def _brain(answer="brain answer"):
    return patch("budtender.views.generate_chat_reply_with_source", return_value=(answer, "brain", ""))


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class AgeCarryTests(TestCase):
    def _say(self, message, token=SESSION):
        return Client().post("/api/v1/chat/message", data=json.dumps({"session_token": token, "message": message}),
                             content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    def test_said_19_earlier_then_asks_for_products_is_declined_without_asking_the_brain(self):
        with _brain() as brain:
            self._say("im 19 but i want weed")                # the brain declines this turn itself
            reply = self._say("ok show me some flower")
            another = self._say("what about gummies")         # still declined, however it is phrased
        self.assertEqual(brain.call_count, 1)                 # only the turn that SAID it reached the brain
        for r in (reply, another):
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["message"]["content"], age_gate.UNDER_21_DECLINE)
            self.assertEqual(r.json()["source"], "guard")

    def test_a_deal_ask_is_still_a_shopping_ask(self):
        with _brain() as brain:
            self._say("I'm only 18")
            r = self._say("any deals on gummies?")
        self.assertEqual(brain.call_count, 1)
        self.assertEqual(r.json()["message"]["content"], age_gate.UNDER_21_DECLINE)

    def test_a_general_question_after_the_admission_still_gets_the_brain(self):
        with _brain("We close at 10.") as brain:
            self._say("I'm 19")
            r = self._say("what time do you close?")
        self.assertEqual(brain.call_count, 2)
        self.assertEqual(r.json()["message"]["content"], "We close at 10.")
        self.assertEqual(r.json()["source"], "brain")

    def test_an_adult_who_says_nothing_about_age_is_unaffected(self):
        with _brain("Here are some flowers.") as brain:
            self._say("hello")
            r = self._say("show me some flower")
            minutes = self._say("I'm 20 minutes away, got any gummies?")   # a distance, not an age
            adult = self._say("I'm 25, what edibles do you recommend")
        self.assertEqual(brain.call_count, 4)
        for x in (r, minutes, adult):
            self.assertEqual(x.json()["source"], "brain")

    def test_somebody_elses_age_does_not_stick_to_the_session(self):
        with _brain() as brain:
            self._say("my friend who's 19 wants to come in")
            r = self._say("show me flower")
        self.assertEqual(brain.call_count, 2)
        self.assertEqual(r.json()["source"], "brain")

    def test_another_session_is_not_affected(self):
        with _brain() as brain:
            self._say("i'm seventeen")
            other = self._say("show me flower", token="s-someone-else-0")
        self.assertEqual(brain.call_count, 2)
        self.assertEqual(other.json()["source"], "brain")

    def test_the_decline_asks_for_and_stores_no_personal_data(self):
        with _brain():
            self._say("I'm 19")
            r = self._say("show me flower")
        text = r.json()["message"]["content"].lower()
        self.assertNotIn("?", text)
        for ask in ("phone", "name", "email", "birth", "date of", "address", "number"):
            self.assertNotIn(ask, text)
        session = ChatSession.objects.get(session_token=SESSION)
        self.assertEqual((session.phone, session.customer_id, session.identity_via), ("", None, ""))
        self.assertEqual(session.messages.count(), 4)                       # the two turns, nothing extra
        self.assertEqual(
            list(ChatMessage.objects.filter(session=session, role="user").values_list("content", flat=True)),
            ["I'm 19", "show me flower"])
        keys = {k for e in AnalyticsEvent.objects.filter(session_token=SESSION) for k in e.props}
        self.assertEqual(keys, {"role", "message_id", "intent", "source"})   # no age flag recorded anywhere


@pytest.mark.parametrize("text", [
    "I'm 19", "i am only 17", "I just turned 20", "I'm nineteen", "I'm under 21", "i'm not 21 yet",
    "I'll be 21 in March", "I was born in %d" % (__import__("datetime").date.today().year - 18),
])
def test_first_person_admissions_are_recognised(text):
    assert age_gate.declared_underage([type("M", (), {"role": "user", "content": text})()])


@pytest.mark.parametrize("text", [
    "I'm 21", "I'm 25", "my brother is 19", "I'm 20 minutes out", "I want 20 pre-rolls", "hello",
])
def test_everything_else_is_not_an_admission(text):
    assert not age_gate.declared_underage([type("M", (), {"role": "user", "content": text})()])


def test_an_assistant_turn_never_counts():
    assert not age_gate.declared_underage([type("M", (), {"role": "assistant", "content": "I'm 19"})()])


def test_the_decline_text_is_the_voice_services_under_21_text():
    src = Path(__file__).resolve().parents[2] / "voice" / "voice" / "safety_copy.py"
    if not src.exists():
        pytest.skip("voice/ is not part of this checkout")
    for node in ast.parse(src.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "UNDER_21" for t in node.targets):
            assert age_gate.UNDER_21_DECLINE == ast.literal_eval(node.value)
            return
    pytest.fail("UNDER_21 not found in voice/voice/safety_copy.py")
