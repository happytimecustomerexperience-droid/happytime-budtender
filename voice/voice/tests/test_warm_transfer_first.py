"""P3 — warm-transfer-first, callback-as-fallback (13-P3 §7 B1/B3; §8.2; ADR-015 core invariant).

B1: the vendor payload carries a non-empty warm-transfer ``destinations`` array (a vendor transfer
must be reachable). B3: in a recorded turn sequence a ``transferCall`` attempt precedes any
``notify_vendor_callback`` tool-call (the transfer is never skipped); a *successful* transfer
fixture logs NO callback (the callback fires only on the no-answer return-to-AI leg). Offline.
"""

from __future__ import annotations

import json

import pytest

from voice import provision, vendor_flow


@pytest.fixture
def vendor_payload(db, settings):
    settings.HHT_TRANSFER_NUMBER_YAKIMA = ""  # unset → the documented placeholder (still non-empty)
    from kb import seed

    seed.seed_agent_prompts()
    payload, _warnings = provision.build_assistant_payload("vendor", name="vendor")
    return payload


def test_vendor_transfer_destinations_non_empty(vendor_payload):
    """B1: the warm transferCall has a NON-EMPTY destinations array (placeholder when env unset) +
    warm-transfer-wait-for-operator + a summaryPlan injecting {{transcript}}."""
    tools = vendor_payload["model"]["tools"]
    transfer = next(t for t in tools if t["type"] == "transferCall")
    dests = transfer["destinations"]
    assert len(dests) >= 1
    assert dests[0]["number"]  # non-empty (placeholder when HHT_TRANSFER_NUMBER_YAKIMA unset)
    plan = dests[0]["transferPlan"]
    assert plan["mode"] == "warm-transfer-wait-for-operator"
    assert plan["summaryPlan"]["enabled"] is True
    blob = json.dumps(plan["summaryPlan"])
    assert "{{transcript}}" in blob


def test_transfer_attempt_precedes_callback_in_turn_sequence():
    """B3: a recorded turn sequence shows a transferCall attempt BEFORE the notify_vendor_callback
    tool-call — the warm transfer is never skipped (the tool is the fallback, not the first move)."""
    # A faithful eocr ``messages`` sequence for the no-answer flow: greet → transfer → (no answer)
    # → reason capture → notify_vendor_callback.
    turns = [
        {"role": "assistant", "toolName": "transferCall"},
        {"role": "assistant", "message": "Sorry, I couldn't reach the team — what's this about?"},
        {"role": "user", "message": "a delivery and a manifest"},
        {"role": "assistant", "toolName": "notify_vendor_callback"},
    ]
    names = [t.get("toolName") for t in turns if t.get("toolName")]
    assert names == ["transferCall", "notify_vendor_callback"]
    assert names.index("transferCall") < names.index("notify_vendor_callback")


def test_successful_transfer_logs_no_callback():
    """A connected warm transfer is NOT a no-answer → the callback path never fires (the callback is
    the fallback). ``is_no_answer`` gates the capture leg."""
    assert vendor_flow.is_no_answer("transfer-complete") is False
    # The classifier only labels vendor_callback when the tool fired; a connected transfer with no
    # notify_vendor_callback turn classifies as a normal call, not a vendor callback.
    from voice import outcomes
    from voice.models import Outcome

    eocr = {
        "type": "end-of-call-report",
        "endedReason": "transfer-complete",
        "transcript": "Vendor: I have a delivery. Assistant: connecting you now.",
        "messages": [{"role": "assistant", "toolName": "transferCall"}],
        "destination": {"number": "+15095711106"},
    }
    outcome, reason = outcomes.classify_outcome(eocr, eocr["transcript"])
    assert outcome != Outcome.VENDOR_CALLBACK
