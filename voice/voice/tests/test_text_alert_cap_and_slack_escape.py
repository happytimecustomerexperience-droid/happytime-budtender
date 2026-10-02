"""W5b fixes 2 + 3 — the website-chat staff-alert flood cap, and Slack markup escaping.

2. One visitor rotating session ids minted a fresh VoiceCall + URGENT alert per id (the ledger
   only dedupes per call). Chat alerts are now capped per store per clock hour; past the cap the
   delivery is recorded ``skipped`` / ``text_alert_cap`` and the VoiceCall row still exists for
   the dashboard. A phone call is never capped.
3. The visitor's raw words went into Slack's ``text`` unescaped, so ``<!channel>`` pinged the
   whole channel and ``<https://evil|login>`` rendered as a disguised link.
"""

from __future__ import annotations

import json
import types

import pytest
from django.core import mail

from crm import sinks
from crm.models import AlertDelivery
from voice.models import VoiceCall


@pytest.fixture(autouse=True)
def _alert_env(settings, monkeypatch):
    # One fixed clock hour, so a run that straddles the top of the hour cannot flake.
    monkeypatch.setattr(sinks, "time", types.SimpleNamespace(time=lambda: 1_800_000_000.0))
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.STAFF_ALERT_EMAIL = "staff@happytimeweed.com"
    settings.SLACK_WEBHOOK_URL = ""
    settings.N8N_WEBHOOK_URL = ""
    settings.HHT_TEXT_ALERT_CAP_PER_STORE_HOUR = "3"


def _escalation(call_id: str, store: str = "yakima") -> VoiceCall:
    return VoiceCall.objects.create(
        call_id=call_id, store=store, outcome="escalation", reason="dispute", ai_summary="refund now"
    )


@pytest.mark.django_db
def test_rotating_chat_session_ids_stop_paging_staff_past_the_cap():
    results = [sinks.dispatch(_escalation(f"s-mfx{i:04d}-abcd{i:04d}")) for i in range(5)]

    assert [r["email"] for r in results] == ["success"] * 3 + ["skipped"] * 2
    assert len(mail.outbox) == 3, "only the first three chat alerts this hour reach a person"
    capped = AlertDelivery.objects.filter(sink="email", status="skipped")
    assert capped.count() == 2 and set(capped.values_list("last_error", flat=True)) == {"text_alert_cap"}
    assert VoiceCall.objects.filter(call_id__startswith="s-").count() == 5, "every row is still logged"
    assert all(r["db"] == "success" for r in results), "the durable record is never capped"


@pytest.mark.django_db
def test_the_cap_is_per_store_and_never_touches_phone_calls():
    for i in range(3):
        sinks.dispatch(_escalation(f"s-mfx{i:04d}-yakima00"))
    assert sinks.dispatch(_escalation("s-mfx9999-yakima99"))["email"] == "skipped"

    assert sinks.dispatch(_escalation("s-mfx0001-pullman1", store="pullman"))["email"] == "success"
    # Vapi call ids are UUIDs — a phone call alerts no matter how many chats came first.
    phone = sinks.dispatch(_escalation("7f3b2c1e-1111-2222-3333-444455556666"))
    assert phone["email"] == "success"


@pytest.mark.django_db
def test_redispatching_one_session_does_not_burn_the_cap():
    vc = _escalation("s-mfx0000-samesess")
    for _ in range(5):  # a multi-turn dispute re-dispatches the same call every turn
        sinks.dispatch(vc)
    assert len(mail.outbox) == 1
    assert sinks.dispatch(_escalation("s-mfx0001-othersess"))["email"] == "success"


@pytest.mark.django_db
def test_slack_text_escapes_visitor_markup(settings, monkeypatch):
    settings.SLACK_WEBHOOK_URL = "https://hooks.slack.test/alert"
    sent = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        sent["payload"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(sinks.urllib.request, "urlopen", fake_urlopen)
    vc = VoiceCall.objects.create(
        call_id="s-mfx0000-slackxss", store="yakima", outcome="escalation", reason="dispute",
        ai_summary="<!channel> <https://evil.example|login> & more",
    )

    assert sinks.dispatch(vc)["slack"] == "success"
    text = sent["payload"]["text"]
    assert "<!channel>" not in text and "<https://" not in text
    assert "&lt;!channel&gt; &lt;https://evil.example|login&gt; &amp; more" in text
