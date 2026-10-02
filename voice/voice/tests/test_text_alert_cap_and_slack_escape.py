"""W5b fixes 2 + 3 — the website-chat staff-alert flood cap, and Slack markup escaping — and W9 fix 1:
the cap is per VISITOR first.

2. One visitor rotating session ids minted a fresh VoiceCall + URGENT alert per id (the ledger
   only dedupes per call). Chat alerts are capped per clock hour; past the cap the delivery is
   recorded ``skipped`` / ``text_alert_cap`` and the VoiceCall row still exists for the dashboard.
   A phone call is never capped.
3. The visitor's raw words went into Slack's ``text`` unescaped, so ``<!channel>`` pinged the
   whole channel and ``<https://evil|login>`` rendered as a disguised link.
W9-1. A per-STORE cap alone let an attacker fill the store's hour with fake disputes from fresh
   session ids and mute every real one. The visitor IP (``X-HHT-Client-IP``) is counted first
   (default 2/hour), the store count (default 20) is only the backstop, and the first alert the
   backstop holds in a store-hour sends ONE roll-up email instead of silence.
"""

from __future__ import annotations

import json
import types

import pytest
from django.core import mail

from crm import sinks
from crm.models import AlertDelivery
from voice import api
from voice.models import VoiceCall

CLOCK = {"now": 1_800_000_000.0}


@pytest.fixture(autouse=True)
def _alert_env(settings, monkeypatch):
    # One fixed clock hour, so a run that straddles the top of the hour cannot flake.
    CLOCK["now"] = 1_800_000_000.0
    monkeypatch.setattr(sinks, "time", types.SimpleNamespace(time=lambda: CLOCK["now"]))
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.STAFF_ALERT_EMAIL = "staff@happytimeweed.com"
    settings.SLACK_WEBHOOK_URL = ""
    settings.N8N_WEBHOOK_URL = ""
    settings.HHT_TEXT_ALERT_CAP_PER_STORE_HOUR = "3"
    settings.HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR = "100"  # out of the way unless a test is about it


class _OkResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _escalation(call_id: str, store: str = "yakima") -> VoiceCall:
    return VoiceCall.objects.create(
        call_id=call_id, store=store, outcome="escalation", reason="dispute", ai_summary="refund now"
    )


def _alerts() -> list:
    """The per-call alert emails (the roll-up has its own subject)."""
    return [m for m in mail.outbox if "alerts held" not in m.subject]


def _rollups() -> list:
    return [m for m in mail.outbox if "alerts held" in m.subject]


@pytest.mark.django_db
def test_rotating_chat_session_ids_stop_paging_staff_past_the_cap():
    results = [sinks.dispatch(_escalation(f"s-mfx{i:04d}-abcd{i:04d}")) for i in range(5)]

    assert [r["email"] for r in results] == ["success"] * 3 + ["skipped"] * 2
    assert len(_alerts()) == 3, "only the first three chat alerts this hour reach a person"
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
    # escaped AND defanged: the link is neither a ping, a disguised link, nor a live URL
    assert "&lt;!channel&gt; &lt;hxxp://evil[.]example|login&gt; &amp; more" in text


# ── W9-1: per visitor first, the store is only the backstop ─────────────────────────────────────


@pytest.mark.django_db
def test_one_visitor_cannot_use_up_the_hour_for_everyone_else(settings):
    """The attack: six fake disputes from fresh ``s-`` ids. They used to eat the whole store budget
    (default 6), so the seventh — a real customer — was muted. Now the attacker runs out of their own
    two, and the store budget has room left for a real one."""
    settings.HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR = "2"
    settings.HHT_TEXT_ALERT_CAP_PER_STORE_HOUR = "6"

    with sinks.visitor_ip("203.0.113.7"):
        attacker = [sinks.dispatch(_escalation(f"s-mfx{i:04d}-fake{i:04d}"))["email"] for i in range(6)]
    with sinks.visitor_ip("198.51.100.9"):
        real = sinks.dispatch(_escalation("s-mfx0042-realcust"))["email"]

    assert attacker == ["success"] * 2 + ["skipped"] * 4
    assert real == "success", "the real customer's complaint reaches staff"
    assert len(_alerts()) == 3 and _rollups() == [], "the backstop never tripped, so no roll-up"
    held = AlertDelivery.objects.filter(sink="email", status="skipped")
    assert held.count() == 4 and set(held.values_list("last_error", flat=True)) == {"text_alert_cap"}


@pytest.mark.django_db
def test_a_visitor_over_their_own_limit_does_not_spend_the_stores_backstop(settings):
    settings.HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR = "1"
    settings.HHT_TEXT_ALERT_CAP_PER_STORE_HOUR = "2"
    with sinks.visitor_ip("203.0.113.7"):
        for i in range(5):
            sinks.dispatch(_escalation(f"s-mfx{i:04d}-flood{i:04d}"))
    # five attempts, one counted at the store: one backstop slot is still free
    with sinks.visitor_ip("198.51.100.9"):
        assert sinks.dispatch(_escalation("s-mfx0050-realone"))["email"] == "success"
    with sinks.visitor_ip("198.51.100.10"):
        assert sinks.dispatch(_escalation("s-mfx0051-realtwo"))["email"] == "skipped", "now the backstop is full"


@pytest.mark.django_db
def test_the_defaults_are_two_per_visitor_and_twenty_per_store(settings):
    del settings.HHT_TEXT_ALERT_CAP_PER_STORE_HOUR
    del settings.HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR
    with sinks.visitor_ip("203.0.113.7"):
        visitor = [sinks.dispatch(_escalation(f"s-mfx{i:04d}-vis{i:05d}"))["email"] for i in range(3)]
    assert visitor == ["success", "success", "skipped"]

    sent = 0
    for i in range(30):  # no vouched visitor: only the backstop counts, 2 already used above
        sent += sinks.dispatch(_escalation(f"s-mfy{i:04d}-sto{i:05d}"))["email"] == "success"
    assert sent == 18 and len(_alerts()) == 20


@pytest.mark.django_db
def test_the_backstop_sends_one_rollup_per_store_hour_not_silence():
    results = [
        sinks.dispatch(_escalation(f"s-mfx{i:04d}-flood{i:04d}"))["email"] for i in range(6)
    ]  # no visitor block: the store backstop (3) is all that counts

    assert results == ["success"] * 3 + ["skipped"] * 3
    [rollup] = _rollups()
    assert rollup.subject == "[Happy Time voice] Yakima — website-chat alerts held"
    assert rollup.to == ["staff@happytimeweed.com"]
    assert rollup.body.startswith("1 more website-chat alert was held this hour — see the dashboard.")
    assert "limit of 3 website-chat alerts per hour" in rollup.body
    assert len(_alerts()) == 3, "and still exactly one roll-up after six alerts, not one per held alert"

    CLOCK["now"] += 3600  # next clock hour: a fresh budget and a fresh roll-up
    for i in range(5):
        sinks.dispatch(_escalation(f"s-mfz{i:04d}-flood{i:04d}"))
    assert len(_rollups()) == 2


@pytest.mark.django_db
def test_the_rollup_is_per_store_and_obeys_the_email_switch(settings, monkeypatch):
    from voice import capabilities

    for i in range(5):
        sinks.dispatch(_escalation(f"s-mfx{i:04d}-pull{i:04d}", store="pullman"))
    [rollup] = _rollups()
    assert "Pullman" in rollup.subject

    # Email off but Slack on: held alerts still trip the backstop, and the roll-up (an email) stays off.
    settings.SLACK_WEBHOOK_URL = "https://hooks.slack.test/alert"
    monkeypatch.setattr(sinks.urllib.request, "urlopen", lambda req, timeout: _OkResponse())
    capabilities.set_enabled("alerts.email", False)
    mail.outbox.clear()
    for i in range(5):
        sinks.dispatch(_escalation(f"s-mfx{i:04d}-mtv{i:04d}", store="mount-vernon"))
    assert mail.outbox == [], "email switched off: no alert, and no roll-up either"
    assert AlertDelivery.objects.filter(sink="slack", last_error="text_alert_cap").count() == 2, "the backstop did trip"


@pytest.mark.django_db
def test_a_failed_rollup_is_retried_by_the_next_held_alert(monkeypatch):
    real_send = sinks.EmailMultiAlternatives.send
    fail = {"on": True}

    def flaky(self, *a, **k):
        if "alerts held" in self.subject and fail["on"]:
            raise OSError("smtp down")
        return real_send(self, *a, **k)

    monkeypatch.setattr(sinks.EmailMultiAlternatives, "send", flaky)
    for i in range(4):  # the 4th trips the backstop; its roll-up fails
        sinks.dispatch(_escalation(f"s-mfx{i:04d}-flood{i:04d}"))
    assert _rollups() == []

    fail["on"] = False
    sinks.dispatch(_escalation("s-mfx0009-floodnext"))  # the next held alert tries again
    assert len(_rollups()) == 1


@pytest.mark.django_db
def test_a_phone_call_is_never_capped_even_from_a_flooded_visitor(settings):
    settings.HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR = "0"
    settings.HHT_TEXT_ALERT_CAP_PER_STORE_HOUR = "0"
    with sinks.visitor_ip("203.0.113.7"):
        assert sinks.dispatch(_escalation("7f3b2c1e-1111-2222-3333-444455556666"))["email"] == "success"
        assert sinks.dispatch(_escalation("s-mfx0000-chatcall"))["email"] == "skipped"


@pytest.mark.django_db
def test_ipv6_visitors_share_one_budget_per_slash_64(settings):
    settings.HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR = "1"
    out = []
    for i, ip in enumerate(("2001:db8:1:2::1", "2001:db8:1:2:ffff::9", "2001:db8:1:3::1", "::ffff:203.0.113.7")):
        with sinks.visitor_ip(ip):
            out.append(sinks.dispatch(_escalation(f"s-mfx{i:04d}-six{i:05d}"))["email"])
    assert out == ["success", "skipped", "success", "success"], "same /64 shares; another /64 and a mapped IPv4 do not"
    with sinks.visitor_ip("203.0.113.7"):  # the mapped address IS that IPv4 visitor
        assert sinks.dispatch(_escalation("s-mfx0009-six00009"))["email"] == "skipped"


@pytest.mark.django_db
def test_a_cache_failure_lets_the_alert_through(monkeypatch):
    class _Dead:
        def __getattr__(self, name):
            raise ConnectionError("redis down")

    monkeypatch.setattr(sinks, "cache", _Dead())
    with sinks.visitor_ip("203.0.113.7"):
        assert sinks.dispatch(_escalation("s-mfx0000-redisdown"))["email"] == "success"


# ── the visitor reaches the cap through the real view ───────────────────────────────────────────


def _post(client, session, ip=None):
    headers = {"HTTP_AUTHORIZATION": "Bearer website-only-token"}
    if ip:
        headers["HTTP_X_HHT_CLIENT_IP"] = ip
    body = json.dumps({"message": "refund me", "session_token": session})
    return client.post("/api/voice/chat", data=body, content_type="application/json", **headers)


@pytest.fixture
def chat_view(settings, monkeypatch):
    """The view with a stand-in brain that does what ``notify_staff_issue`` does: log an escalation for
    the session and dispatch the staff alert, inside the request."""
    settings.HHT_VOICE_TOKEN = "website-only-token"
    settings.HHT_VOICE_RATE_LIMIT = "1000"
    settings.HHT_VOICE_GLOBAL_RATE_LIMIT = "1000"
    settings.HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR = "2"
    settings.HHT_TEXT_ALERT_CAP_PER_STORE_HOUR = "50"

    def brain(data):
        sinks.dispatch(_escalation(data["session_token"]))
        return {"ok": True, "answer": "ok"}

    monkeypatch.setattr(api, "answer_text_chat", brain)


@pytest.mark.django_db
def test_the_view_counts_alerts_against_the_visitor_the_website_vouched_for(client, chat_view):
    for i in range(5):
        assert _post(client, f"s-mfx{i:04d}-fakefake", ip="203.0.113.7").status_code == 200
    assert _post(client, "s-mfx0099-realcust", ip="198.51.100.9").status_code == 200

    assert len(_alerts()) == 3, "two from the flooder, one real customer"


@pytest.mark.django_db
def test_without_a_vouched_ip_the_view_applies_only_the_store_backstop(client, chat_view):
    """No ``X-HHT-Client-IP``: the proxy's address is shared by every visitor, so it must not become a
    two-per-hour cap for the whole site."""
    for i in range(5):
        assert _post(client, f"s-mfx{i:04d}-noheader").status_code == 200
    assert len(_alerts()) == 5
    assert sinks._visitor.get() == "", "and the visitor mark never leaks out of the request"
