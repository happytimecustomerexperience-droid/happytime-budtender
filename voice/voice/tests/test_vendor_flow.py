"""P3 — the pure vendor-flow helpers (13-P3 §7 B2; §8.1). Deterministic, no network.

``is_no_answer`` over the §4.4 reason set (a no-answer/busy/transfer-failed → True; a connected
transfer → False); ``normalize_reason`` folding free-text "why" into the stable enum;
``callback_window_text`` from config (Numbers-Guard default). Expected values hand-authored.
"""

from __future__ import annotations

import pytest

from voice import vendor_flow as vf

NO_ANSWER_SIGNALS = [
    "customer-did-not-answer",
    "assistant-forwarded-call-failed",
    "pipeline-error-twilio-failed-to-connect-call",
    "twilio-failed-to-connect-call",
    "no-answer",
    "busy",
    "voicemail",
    "transfer-failed",
    "declined",
    "operator-timed-out",
]

CONNECTED_SIGNALS = [
    "transfer-complete",
    "transfer-completed",
    "operator-connected",
    "warm-transfer-success",
    "destination-connected",
]


@pytest.mark.parametrize("signal", NO_ANSWER_SIGNALS)
def test_is_no_answer_true(signal):
    """B2: a no-answer/busy/transfer-failed disposition → True (capture the reason, log a callback)."""
    assert vf.is_no_answer(signal) is True


@pytest.mark.parametrize("signal", CONNECTED_SIGNALS)
def test_is_no_answer_false_when_connected(signal):
    """B2: a transfer that CONNECTED → False (the warm transfer succeeded; NO callback)."""
    assert vf.is_no_answer(signal) is False


def test_is_no_answer_empty_is_false():
    """An empty/unknown signal is not a no-answer (never logs a callback on no signal)."""
    assert vf.is_no_answer("") is False
    assert vf.is_no_answer(None) is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("dropping off a pallet", vf.REASON_DELIVERY),
        ("I'm the driver with a delivery", vf.REASON_DELIVERY),
        ("got a PO", vf.REASON_WHOLESALE),
        ("a wholesale order", vf.REASON_WHOLESALE),
        ("manifest correction", vf.REASON_MANIFEST),
        ("here's a transfer manifest", vf.REASON_MANIFEST),
        ("a sample drop", vf.REASON_SAMPLE),
        ("invoice question", vf.REASON_INVOICE),
        ("accounts payable", vf.REASON_INVOICE),
        ("delivery", vf.REASON_DELIVERY),  # already-valid enum passes through
        ("just calling to say hi", vf.REASON_OTHER),  # no match → other (never an exception)
        ("", vf.REASON_OTHER),
    ],
)
def test_normalize_reason(raw, expected):
    """The free-text "why" folds to the stable enum; unknown → 'other'."""
    assert vf.normalize_reason(raw) == expected


def test_normalize_reason_in_vendor_reasons():
    """Every folded reason is a member of the frozen VENDOR_REASONS set."""
    for raw in ("pallet", "PO", "manifest", "sample", "invoice", "nonsense"):
        assert vf.normalize_reason(raw) in vf.VENDOR_REASONS


def test_callback_window_default_and_override():
    """C3: empty config → the documented default; a configured value is used verbatim."""
    assert vf.callback_window_text("") == "one business day"
    assert vf.callback_window_text(None) == "one business day"
    assert vf.callback_window_text("two hours") == "two hours"
