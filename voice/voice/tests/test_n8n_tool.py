"""P6: the bot-callable notify_n8n tool posts a leak-safe event when configured, degrades safely
when not, and is auto-provisioned when bound to a bot from the dashboard. Offline.
"""

from __future__ import annotations

import json

import pytest


def test_notify_n8n_is_registered():
    from voice.tools import TOOL_REGISTRY

    assert "notify_n8n" in TOOL_REGISTRY


def test_notify_n8n_degrades_when_unconfigured(settings):
    from voice.tools.n8n import notify_n8n

    settings.N8N_WEBHOOK_URL = ""
    out = notify_n8n({"event_type": "send_menu_link"}, {"store": "yakima"})
    assert out["ok"] is False and out["reason"] == "n8n not configured"


def test_notify_n8n_posts_event(settings, monkeypatch):
    import urllib.request

    from voice.tools.n8n import notify_n8n

    settings.N8N_WEBHOOK_URL = "https://n8n.example/webhook/x"
    sent = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake(req, timeout=10):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    out = notify_n8n(
        {"event_type": "callback_request", "summary": "wants a callback", "store": "pullman"},
        {"call_id": "c9", "store": "pullman"},
    )
    assert out["ok"] is True and out["queued"] is True
    assert sent["body"]["event_type"] == "callback_request"
    assert sent["body"]["store_spoken"] == "Pullman"
    assert sent["body"]["call_id"] == "c9"
    # leak/PII: no cost/margin; no raw caller number key.
    blob = json.dumps(sent["body"]).lower()
    assert "cost" not in blob and "margin" not in blob
    assert "number" not in sent["body"]


@pytest.mark.django_db
def test_binding_a_tool_auto_provisions_it_on_publish(monkeypatch):
    """Adding notify_n8n to a bot from the dashboard → publish provisions the Vapi tool (no separate
    provision run needed)."""
    from core.services import vapi
    from dashboard import publish
    from kb.models import AgentPrompt
    from voice.models import VapiObject
    from voice.tests.test_provision import FakeAccount

    acct = FakeAccount()
    monkeypatch.setattr(vapi, "configured", lambda: True)
    for name in ("find_tool_by_name", "get_tool", "create_tool", "patch_tool",
                 "find_assistant_by_name", "get_assistant", "create_assistant", "patch_assistant"):
        monkeypatch.setattr(vapi, name, getattr(acct, name))

    p = AgentPrompt.objects.create(role="budtender", body="hi", tool_names=["notify_n8n"], is_active=True)
    assert not VapiObject.objects.filter(kind="tool", name="notify_n8n").exists()

    publish.publish_assistant(p)
    rec = VapiObject.objects.filter(kind="tool", name="notify_n8n").first()
    assert rec and rec.vapi_id  # the bound tool was provisioned during publish
