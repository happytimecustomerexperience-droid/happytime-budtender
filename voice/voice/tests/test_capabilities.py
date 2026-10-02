"""The capability switchboard (voice/voice/capabilities.py): defaults, persistence, fail-closed,
and the guard that every declared switch is actually enforced somewhere."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from voice import capabilities as caps

VOICE_ROOT = Path(__file__).resolve().parents[2]  # voice/
_SKIP_DIRS = {"tests", "migrations", "__pycache__", "evals"}


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.delete(caps._CACHE_KEY)
    yield
    cache.delete(caps._CACHE_KEY)


def test_declared_keys_are_unique_and_well_formed():
    keys = [c.key for c in caps.CAPABILITIES]
    assert len(keys) == len(set(keys))
    for c in caps.CAPABILITIES:
        assert re.fullmatch(r"[a-z]+\.[a-z0-9_]+", c.key), c.key
        assert c.label and c.does and c.when_off, c.key


def test_every_gated_tool_exists():
    from voice.tools import TOOL_REGISTRY

    missing = [c.tool for c in caps.CAPABILITIES if c.tool and c.tool not in TOOL_REGISTRY]
    assert not missing, f"capabilities gate tools that are not registered: {missing}"


@pytest.mark.django_db
def test_defaults_apply_when_no_row_exists():
    for c in caps.CAPABILITIES:
        assert caps.is_enabled(c.key) is c.default, c.key


@pytest.mark.django_db
def test_set_enabled_persists_and_clears_the_cache():
    key = "tool.pair_upsell"
    assert caps.is_enabled(key) is True
    caps.set_enabled(key, False, by="owner")
    assert caps.is_enabled(key) is False
    assert caps.tool_allowed("pair_upsell") is False
    caps.set_enabled(key, True)
    assert caps.is_enabled(key) is True


@pytest.mark.django_db
def test_unknown_key_is_off_and_cannot_be_written():
    assert caps.is_enabled("tool.does_not_exist") is False
    with pytest.raises(KeyError):
        caps.set_enabled("tool.does_not_exist", True)


def test_unreadable_state_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(caps, "states", boom)
    assert caps.is_enabled("tool.faq_lookup") is False


def test_ungated_tool_is_allowed():
    assert caps.tool_allowed("notify_n8n") is True


def _source_files():
    for path in VOICE_ROOT.rglob("*.py"):
        if _SKIP_DIRS & set(path.relative_to(VOICE_ROOT).parts):
            continue
        if path.name == "capabilities.py":
            continue
        yield path


def test_every_capability_is_enforced():
    """A declared switch that no code checks would tell the owner something is off when it is
    not. Tool switches are enforced once, in the dispatcher (``tool_allowed``); every other key
    must appear as ``is_enabled("<key>")`` in real (non-test) code."""
    blob = "\n".join(p.read_text(encoding="utf-8") for p in _source_files())
    dispatcher = (VOICE_ROOT / "voice" / "tools" / "__init__.py").read_text(encoding="utf-8")
    unenforced = []
    for c in caps.CAPABILITIES:
        if c.tool:
            if "tool_allowed(" not in dispatcher:
                unenforced.append(c.key)
        elif f'is_enabled("{c.key}")' not in blob:
            unenforced.append(c.key)
    assert not unenforced, f"declared but never enforced: {unenforced}"


# ── enforcement: each switch changes what the bots actually do ─────────────────
@pytest.mark.django_db
def test_switched_off_tool_returns_the_disabled_shape_and_never_runs(monkeypatch):
    from voice import safety_copy, tools

    calls = []
    monkeypatch.setitem(
        tools.TOOL_REGISTRY, "pair_upsell", lambda args, ctx: calls.append(args) or {"ok": True}
    )
    assert tools.dispatch("pair_upsell", {}, {}) == {"ok": True}  # control: on → the handler runs
    caps.set_enabled("tool.pair_upsell", False)
    assert tools.dispatch("pair_upsell", {}, {}) == {
        "disabled": True,
        "tool": "pair_upsell",
        "answer": None,
        "grounded": False,
        "fallback": safety_copy.TOOL_DISABLED,
    }
    assert len(calls) == 1, "the handler must not run while its switch is off"


@pytest.fixture
def provisioned_tools(db):
    from voice import constants as C
    from voice.models import VapiObject

    for name in C.TOOL_SPECS:
        VapiObject.objects.create(kind="tool", name=name, vapi_id=f"id-{name}")


@pytest.mark.django_db
def test_provision_leaves_a_switched_off_tool_off_the_assistant(provisioned_tools):
    from kb.models import AgentPrompt
    from voice import provision

    AgentPrompt.objects.create(
        role="budtender", body="b", is_active=True, tool_names=["suggest_products", "pair_upsell"]
    )
    payload, _ = provision.build_assistant_payload("budtender")
    assert payload["model"]["toolIds"] == ["id-suggest_products", "id-pair_upsell"]  # control
    caps.set_enabled("tool.pair_upsell", False)
    payload, warnings = provision.build_assistant_payload("budtender")
    assert payload["model"]["toolIds"] == ["id-suggest_products"]
    assert "tool switched off: pair_upsell" in warnings


@pytest.mark.django_db
def test_provision_drops_the_transfer_tool_and_says_so_when_transfers_are_off(provisioned_tools):
    from kb.models import AgentPrompt
    from voice import provision

    AgentPrompt.objects.create(role="escalation", body="e", is_active=True)
    line = provision._NO_TRANSFER_LINE.strip()
    payload, _ = provision.build_assistant_payload("escalation")
    assert [t["type"] for t in payload["model"]["tools"]] == ["transferCall"]  # control
    assert line not in payload["model"]["messages"][0]["content"]
    caps.set_enabled("call.transfer", False)
    payload, _ = provision.build_assistant_payload("escalation")
    assert "tools" not in payload["model"]
    assert line in payload["model"]["messages"][0]["content"]


@pytest.mark.django_db
def test_recognition_off_treats_every_caller_as_new_with_no_lookup():
    from voice import recognition

    class Client:
        calls = 0

        def resume_by_phone(self, *a, **k):
            Client.calls += 1
            return {"profile_summary": {"has_history": True}, "session_token": "t"}

    ctx = recognition.resolve_caller("+15095551234", {}, client=Client())
    assert ctx["known"] is True and Client.calls == 1  # control: on → looked up, known
    caps.set_enabled("call.recognize_caller", False)
    ctx = recognition.resolve_caller("+15095551234", {}, client=Client())
    assert Client.calls == 1, "no profile lookup while recognition is off"
    assert ctx["known"] is False and ctx["session_token"] is None and ctx["_caller_phone"] is None


@pytest.mark.django_db
def test_website_chat_off_answers_offline_without_the_brain(client, settings, monkeypatch):
    from kb.models import AgentPrompt
    from voice import api, safety_copy

    settings.HHT_BACKEND_TOKEN = "test-token"
    auth = {"HTTP_AUTHORIZATION": "Bearer test-token"}
    brain = []
    monkeypatch.setattr(api, "answer_text_chat", lambda data: brain.append(data) or {"ok": True})
    AgentPrompt.objects.create(role="written", body="Website persona.", is_active=True)

    def chat():
        return client.post(
            "/api/voice/chat", data={"message": "hi"}, content_type="application/json", **auth
        )

    chat()
    assert len(brain) == 1  # control: on → the brain answers
    caps.set_enabled("channel.website_chat", False)
    resp = chat()
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True, "answer": safety_copy.CHAT_OFFLINE, "grounded": False, "disabled": True
    }
    assert len(brain) == 1, "the brain must not run while the website chat is off"
    persona = client.get("/api/voice/persona", **auth).json()
    assert persona["website_chat_enabled"] is False


@pytest.mark.django_db
def test_nightly_drift_check_off_does_not_run(monkeypatch):
    from kb.management.commands import check_store_facts
    from voice import tasks

    def boom():
        raise AssertionError("the comparison must not run while the switch is off")

    monkeypatch.setattr(check_store_facts, "diff_against_site", boom)
    caps.set_enabled("auto.nightly_drift_check", False)
    assert tasks.check_store_facts_nightly() == {"skipped": "capability off"}


@pytest.mark.django_db
def test_alert_sinks_and_n8n_follow_their_switches(settings, monkeypatch):
    from django.core import mail

    from crm import sinks
    from voice.models import VoiceCall
    from voice.tools import n8n

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.STAFF_ALERT_EMAIL = "staff@example.com"
    settings.SLACK_WEBHOOK_URL = "https://hooks.slack.test/x"
    settings.N8N_WEBHOOK_URL = "https://n8n.test/x"
    posted = []
    monkeypatch.setattr(sinks.urllib.request, "urlopen", lambda *a, **k: posted.append(a))
    vc = VoiceCall.objects.create(
        call_id="cap-sink-1", store="yakima", outcome="escalation", reason="defective_return"
    )
    email, slack, hook = sinks.EmailSink(), sinks.SlackSink(), sinks.N8nSink()
    assert email.enabled(vc) and slack.enabled(vc) and hook.enabled(vc)  # control: all on

    for key in ("alerts.email", "alerts.slack", "alerts.n8n"):
        caps.set_enabled(key, False)
    assert not (email.enabled(vc) or slack.enabled(vc) or hook.enabled(vc))
    sinks.send_staff_alert(subject="drift", markdown_table="| a |")
    assert mail.outbox == [] and posted == []
    assert n8n.notify_n8n({"event_type": "menu_link"}, {}) == {
        "ok": False, "reason": "n8n switched off"
    }
