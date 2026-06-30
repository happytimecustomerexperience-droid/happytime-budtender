"""P6: the n8n outbound sink posts a leak-safe call event when N8N_WEBHOOK_URL is set, and is
skipped (not failed) when it isn't. Offline — the HTTP POST is monkeypatched.
"""

from __future__ import annotations

import json

import pytest


@pytest.mark.django_db
def test_n8n_sink_skipped_when_unset(settings):
    from crm.sinks import N8nSink
    from voice.models import VoiceCall

    settings.N8N_WEBHOOK_URL = ""
    vc = VoiceCall.objects.create(call_id="c1", store="yakima", outcome="suggested")
    assert N8nSink().enabled(vc) is False


@pytest.mark.django_db
def test_n8n_sink_posts_leak_safe_payload(settings, monkeypatch):
    import urllib.request

    from crm.sinks import N8nSink
    from voice.models import VoiceCall

    settings.N8N_WEBHOOK_URL = "https://n8n.example/webhook/abc"
    vc = VoiceCall.objects.create(
        call_id="c2", store="pullman", outcome="suggested",
        caller_phone_hash="deadbeef" * 8, duration_s=42, suggested_skus=["SKU1"],
        ai_summary="Caller wanted edibles.",
    )

    sent = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=10):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    assert N8nSink().enabled(vc) is True
    N8nSink().deliver(vc)

    assert sent["url"] == "https://n8n.example/webhook/abc"
    body = sent["body"]
    assert body["event"] == "voice_call"
    assert body["call_id"] == "c2"
    assert body["store"] == "pullman"
    assert body["suggested_skus"] == ["SKU1"]
    # leak/PII discipline: no cost/margin keys; caller is a truncated hash, never a raw number.
    blob = json.dumps(body).lower()
    assert "cost" not in blob and "margin" not in blob
    assert "number" not in body
