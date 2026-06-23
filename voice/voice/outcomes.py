"""Deterministic outcome classification for the end-of-call-report (12-P2 §4.4).

Code owns the label — NO LLM. The model only fills slots (it emits
``analysis.structuredData.reason`` / ``human_requested``); the precedence table here decides the
durable ``VoiceCall.outcome`` + ``escalation_reason`` + the immediate-alert flag. This mirrors the
swedish-bot "deterministic CRM enrichment, never LLM" discipline.

Precedence is highest-severity-wins so an immediate-alert outcome (defective / repeated-human /
dispute / vendor-callback) is never masked by a softer one (suggested / faq / abandoned):

  1. defective return            → outcome=escalation, reason=defective_return  (immediate)
  2. >=2 explicit human requests → outcome=escalation, reason=repeated_request  (immediate)
  3. return dispute (no defect)  → outcome=escalation, reason=dispute           (immediate)
  4. vendor callback (P3 label)  → outcome=vendor_callback                      (immediate)
  5. a suggestion was made       → outcome=suggested                           (not immediate)
  6. only informational/faq      → outcome=faq_answered                        (not immediate)
  7. < ~15s, no progress         → outcome=abandoned                           (not immediate)
  8. otherwise                   → outcome=faq_answered / error                (not immediate)

The durable ``Outcome`` enum (voice/models.py, FROZEN) has no ``defective_return`` member — a
defective return IS an ``escalation`` outcome whose ``escalation_reason="defective_return"`` (the
model already carries ``reason`` for exactly this). The reason is the canonical defective/dispute
signal P4's dashboard + the email subject read.
"""

from __future__ import annotations

import re

# Reasons stamped onto VoiceCall.reason (escalation_reason). "" when the outcome isn't an escalation.
REASON_DEFECTIVE = "defective_return"
REASON_REPEATED = "repeated_request"
REASON_DISPUTE = "dispute"
ESCALATION_REASONS = (REASON_DEFECTIVE, REASON_REPEATED, REASON_DISPUTE)

# Deterministic transcript lexicons (the model also emits structuredData.reason; we OR the two so
# classification never relies on regex alone). Tuned to the claim, not a mere mention.
_DEFECTIVE = re.compile(
    r"\b(defect\w*|broken|won'?t (fire|hit|charge|work)|doesn'?t (fire|hit|work)|"
    r"malfunction\w*|dead (cart|cartridge|battery|pen)|stopped working|faulty|"
    r"not working|leaking)\b",
    re.IGNORECASE,
)
# A return/billing dispute with NO defect signal — the caller contests a sale/charge.
_DISPUTE = re.compile(
    r"\b(charged me (for )?(twice|two|double)|overcharged|wrong (item|order|change|price)|"
    r"only got one|didn'?t get|never (got|received)|dispute|i want (my )?(money|a refund) back|"
    r"refund|short(ed| change)|messed up my order|double charged)\b",
    re.IGNORECASE,
)

# Endings that mean a transfer was attempted + how it landed (best-effort; the raw ended_reason is
# always kept on the row so a later correction is a data fix, not a re-call — 12-P2 §9).
_CONNECTED_ENDINGS = (
    "transfer",
    "forwarded",
    "operator",
    "assistant-forwarded-call",
)
_NO_ANSWER_ENDINGS = (
    "no-answer",
    "no_answer",
    "transfer-failed",
    "voicemail",
    "busy",
)


def escalation_reason_of(message: dict, transcript: str) -> str:
    """The escalation reason from the eocr structuredData + the transcript, or ``""``.

    structuredData.reason (model-emitted) wins when it is a known escalation reason; otherwise the
    deterministic lexicon decides. Precedence: defective > repeated_request > dispute."""
    sd = _structured_data(message)
    sd_reason = str(sd.get("reason") or "").strip().lower()
    if sd_reason in ESCALATION_REASONS:
        return sd_reason

    human = _human_requested_count(message, transcript)
    if _DEFECTIVE.search(transcript or ""):
        return REASON_DEFECTIVE
    if human >= 2:
        return REASON_REPEATED
    if _DISPUTE.search(transcript or ""):
        return REASON_DISPUTE
    return ""


def is_immediate_alert(outcome: str, reason: str) -> bool:
    """True for the staff-must-know-now outcomes: any escalation (defective / repeated / dispute)
    and a vendor callback. These get the ``— URGENT`` email subject + (when enabled) Slack."""
    from voice.models import Outcome

    return outcome in (Outcome.ESCALATION, Outcome.VENDOR_CALLBACK) or reason in ESCALATION_REASONS


def classify_outcome(message: dict, transcript: str) -> tuple[str, str]:
    """Return ``(outcome, escalation_reason)`` for the eocr — the deterministic precedence table.

    ``outcome`` is a ``voice.models.Outcome`` value (FROZEN enum); ``escalation_reason`` is one of
    ESCALATION_REASONS or ``""``. P0's plain path (faq_answered / abandoned / error) is preserved
    for non-escalation calls — this only LIFTS a call to escalation/vendor when a real signal is
    present, never suppresses one."""
    from voice.models import Outcome

    transcript = (transcript or "").strip()
    msgs = message.get("messages") or []

    # 1-3) escalation (highest severity wins; defective > repeated > dispute).
    reason = escalation_reason_of(message, transcript)
    if reason:
        return Outcome.ESCALATION, reason

    # 4) vendor callback — P3 owns the write; P2 recognizes the label so the alert fires.
    sd = _structured_data(message)
    if str(sd.get("outcome") or "").strip().lower() == Outcome.VENDOR_CALLBACK:
        return Outcome.VENDOR_CALLBACK, ""

    # abandoned — empty, no dialogue.
    if not transcript and not msgs:
        return Outcome.ABANDONED, ""

    ended = (message.get("endedReason") or "").lower()
    if "error" in ended or "failed" in ended:
        return Outcome.ERROR, ""

    # 5) a suggestion was made (a suggest_products tool fired / the model emitted the label).
    if str(sd.get("outcome") or "").strip().lower() == Outcome.SUGGESTED or _suggestion_made(
        message
    ):
        return Outcome.SUGGESTED, ""

    # 6-8) informational / faq (the P0 default for a real, non-error call).
    return Outcome.FAQ_ANSWERED, ""


def transfer_disposition(message: dict, reason: str) -> tuple[bool, str]:
    """Return ``(transferred, disposition)`` from the eocr.

    A transfer is attempted when the eocr carries a ``destination`` (Vapi populates it on a
    transferCall) OR the call escalated. Disposition is inferred from ``endedReason``:
    connected | no_answer | not_attempted (the raw reason stays on the row, 12-P2 §9)."""
    has_destination = bool(message.get("destination"))
    transferred = has_destination or bool(reason)
    if not transferred:
        return False, "not_attempted"
    ended = (message.get("endedReason") or "").lower()
    if any(tok in ended for tok in _NO_ANSWER_ENDINGS):
        return True, "no_answer"
    if has_destination or any(tok in ended for tok in _CONNECTED_ENDINGS):
        return True, "connected"
    return True, "not_attempted"


def human_requested_count(message: dict, transcript: str) -> int:
    """Public accessor for the running human-request count (model slot OR transcript count)."""
    return _human_requested_count(message, transcript)


# ── internals ─────────────────────────────────────────────────────────────────

_HUMAN_REQUEST = re.compile(
    r"(talk to|speak (to|with)|get me|connect me to|i want|can i (talk|speak)|"
    r"give me)\s+(a\s+)?(real\s+|actual\s+)?(person|human|manager|someone|representative|rep|"
    r"associate|staff)",
    re.IGNORECASE,
)


def _structured_data(message: dict) -> dict:
    analysis = message.get("analysis") or {}
    sd = analysis.get("structuredData")
    return sd if isinstance(sd, dict) else {}


def _human_requested_count(message: dict, transcript: str) -> int:
    """The number of times the caller asked for a person — the model's
    ``structuredData.human_requested`` if present, else a transcript count of request phrasings."""
    sd = _structured_data(message)
    raw = sd.get("human_requested")
    try:
        if raw is not None:
            return max(0, int(raw))
    except (TypeError, ValueError):
        pass
    return len(_HUMAN_REQUEST.findall(transcript or ""))


def _suggestion_made(message: dict) -> bool:
    """A suggest_products tool fired during the call (eocr messages carry tool turns)."""
    for msg in message.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        name = str(msg.get("name") or msg.get("toolName") or "")
        if name == "suggest_products":
            return True
    return False
