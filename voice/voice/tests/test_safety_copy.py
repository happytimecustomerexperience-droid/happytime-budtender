"""Each chat.py answer helper must speak its safety_copy constant verbatim — text chat and voice
share one owner-approved sentence (voice/voice/safety_copy.py). The provisioned-prompt half of
this contract is covered in voice/voice/tests/test_provision.py, alongside the fake_vapi fixture
it needs.
"""

from __future__ import annotations

from voice import chat
from voice import safety_copy as S


def test_escalation_answer_starts_with_dispute_copy():
    assert chat._escalation_answer("Yakima", "509-555-0100").startswith(S.DISPUTE)


def test_cannot_answer_safely_answer_starts_with_its_copy():
    assert chat._cannot_answer_safely_answer("Yakima", "").startswith(S.CANNOT_ANSWER_SAFELY)


def test_under_21_answer_is_exactly_its_copy():
    assert chat._under_21_answer("Yakima", "509-555-0100") == S.UNDER_21


def test_poison_emergency_answer_starts_with_its_copy():
    assert chat._poison_emergency_answer("Yakima", "").startswith(S.POISON_EMERGENCY)
