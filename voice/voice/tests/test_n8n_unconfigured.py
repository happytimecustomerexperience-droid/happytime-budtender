"""`notify_n8n` with no webhook configured must degrade, never break a call.

This is the one registered tool that cannot be verified end to end, because `N8N_WEBHOOK_URL` is
unset in production — and it is unset for a real reason, not an oversight: the n8n instance on the
VPS has no workflow with a webhook to receive these events yet. There is nothing to point it at
until one is built, so "set the env var" is not the fix; building the workflow is.

What CAN be finished, and is finished here, is the behaviour in the meantime: an unconfigured sink
must be a clean no-op. It must not raise, must not stall a live phone call, and must not leak the
tool's internals to a caller. A tool that is inert for weeks and then explodes the day someone sets
a URL is worse than one that was never wired up.
"""

from __future__ import annotations

import pytest

from voice.tools import dispatch


@pytest.mark.django_db
def test_unconfigured_sink_is_a_clean_no_op(settings):
    settings.N8N_WEBHOOK_URL = ""
    out = dispatch(
        "notify_n8n",
        {"event_type": "system_test", "summary": "unconfigured-sink check"},
        {"store": "yakima"},
    )
    assert isinstance(out, dict)
    assert out.get("ok") is False
    # Says WHY, so an operator reading a tool log can tell "not wired up" from "tried and failed".
    assert "not configured" in str(out.get("reason", "")).lower()


@pytest.mark.django_db
def test_unconfigured_sink_never_raises_through_dispatch(settings):
    """A raising tool would surface as a generic tool_failed mid-call. Absence of a webhook is a
    known, expected state — it must not look like a malfunction."""
    settings.N8N_WEBHOOK_URL = ""
    for payload in (
        {},
        {"event_type": "x"},
        {"event_type": "x", "summary": "y", "store": "yakima"},
        {"event_type": None, "summary": None},
    ):
        out = dispatch("notify_n8n", payload, {"store": "yakima"})
        assert isinstance(out, dict)
        assert out.get("error") != "tool_failed", f"unconfigured sink raised on {payload!r}"


@pytest.mark.django_db
def test_unconfigured_sink_leaks_nothing(settings):
    """No URL, no token, no cost/margin — the same floor every other tool result has to clear."""
    settings.N8N_WEBHOOK_URL = ""
    out = dispatch("notify_n8n", {"event_type": "system_test"}, {"store": "yakima"})
    blob = str(out).lower()
    assert "http" not in blob
    assert "margin" not in blob and "cost" not in blob
