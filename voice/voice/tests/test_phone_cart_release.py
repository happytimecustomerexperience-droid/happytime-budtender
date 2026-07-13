import pytest

from voice import tasks, webhooks
from voice.models import VoiceToolCall


@pytest.mark.django_db
def test_end_of_call_releases_staged_phone_cart(monkeypatch):
    releases = []

    class FakeBudtender:
        def phone_cart_release(self, payload):
            releases.append(payload)
            return {"ok": True, "draft": {"status": "released"}}

    monkeypatch.setattr("voice.budtender_client.budtender", lambda: FakeBudtender())
    monkeypatch.setattr(tasks, "run_post_call", lambda pk: None)
    VoiceToolCall.objects.create(
        call_id="call-cart",
        tool_call_id="tc-cart",
        name="stage_phone_cart",
        args={"action": "add_item"},
        result={"ok": True},
        store="yakima",
    )

    resp = webhooks.handle_end_of_call_report({
        "type": "end-of-call-report",
        "call": {"id": "call-cart", "customer": {"number": "+15095551234"}},
        "transcript": "add two carts",
        "messages": [],
    })

    assert resp.status_code == 200
    assert releases == [{"call_id": "call-cart", "store": "yakima"}]
