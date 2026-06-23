"""Pure helpers for the vendor transfer→no-answer→capture flow (13-P3 §3.1 / §4.4; ADR-015).

Deterministic, no-network functions so the tool + webhook stay thin and unit-testable. Three
helpers, each one place to update:

  * ``is_no_answer(signal)`` — maps a Vapi transfer-failure / no-answer / busy disposition to True
    (control returned to the AI member → capture the reason + log a callback). A transfer that
    CONNECTED maps to False (the warm transfer succeeded; NO callback is logged). The reason set
    is pinned here so the no-answer→return-to-AI semantics live in ONE function (§9 risk).
  * ``normalize_reason(raw)`` — folds the caller's free-text "why" into a stable ``reason`` enum
    (``delivery|wholesale_order|manifest|sample_drop|invoice|other``) so the durable record is
    correct even if the model's phrasing drifts. Independent of the LLM (server-side discipline,
    mirrors ``voice/outcomes.py``).
  * ``callback_window_text(cfg)`` — the spoken callback window, config/KB-sourced (Numbers-Guard,
    ADR-012): never an LLM-originated time. Defaults to "one business day".

Mirrors ``voice/outcomes.py``'s pure-function, code-owns-the-label style.
"""

from __future__ import annotations

import re

# The stable reason enum the durable VendorCallback + the tool envelope carry (§4.2/§4.3).
REASON_DELIVERY = "delivery"
REASON_WHOLESALE = "wholesale_order"
REASON_MANIFEST = "manifest"
REASON_SAMPLE = "sample_drop"
REASON_INVOICE = "invoice"
REASON_OTHER = "other"
VENDOR_REASONS = (
    REASON_DELIVERY,
    REASON_WHOLESALE,
    REASON_MANIFEST,
    REASON_SAMPLE,
    REASON_INVOICE,
    REASON_OTHER,
)

DEFAULT_CALLBACK_WINDOW = "one business day"

# ── no-answer disposition set (§4.4) — pinned here so the mapping is ONE place to update ──
# A Vapi transferCall that fails to connect (no answer / busy / declined / timeout) returns
# control to the calling assistant member; these substrings (case-insensitive, matched within the
# raw endedReason/status string) mean "no answer → capture the reason + log the callback".
_NO_ANSWER_TOKENS = (
    "customer-did-not-answer",
    "did-not-answer",
    "no-answer",
    "no_answer",
    "assistant-forwarded-call-failed",
    "transfer-failed",
    "transfer_failed",
    "failed-to-connect",
    "twilio-failed-to-connect-call",
    "voicemail",
    "busy",
    "declined",
    "timeout",
    "timed-out",
)
# A transfer that CONNECTED → not a no-answer (no callback). Checked first; a "connected" signal
# wins over an incidental "transfer" substring.
_CONNECTED_TOKENS = (
    "transfer-complete",
    "transfer-completed",
    "operator-connected",
    "warm-transfer-success",
    "destination-connected",
)


def is_no_answer(signal: str) -> bool:
    """True when a transfer disposition string means the store human did NOT pick up (so control
    returned to the vendor AI member and the callback path fires); False when the transfer
    connected or the signal is empty/unknown (a successful/unknown transfer never logs a callback).

    ``signal`` is the raw Vapi ``endedReason`` / status string (best-effort; the model's prompt
    also carries the reason-capture path so the flow recovers if a disposition string drifts)."""
    s = (signal or "").strip().lower()
    if not s:
        return False
    if any(tok in s for tok in _CONNECTED_TOKENS):
        return False
    return any(tok in s for tok in _NO_ANSWER_TOKENS)


# ── reason folding (§3.1) — free-text "why" → the stable enum ──────────────────
# Ordered most-specific first; the first family whose keyword matches wins. Lexicon mirrors the
# owner's real returns/manifest vocab (01-ARCHITECTURE §1.4).
_REASON_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (REASON_MANIFEST, re.compile(r"\b(manifest|metrc|ccrs|wcia|transfer\s+manifest)\b", re.I)),
    (REASON_SAMPLE, re.compile(r"\b(sample|samples|sample\s+drop|tester|testers)\b", re.I)),
    (
        REASON_INVOICE,
        re.compile(
            r"\b(invoice|invoices|accounts?\s+payable|a/?p\b|billing|payment\s+due)\b", re.I
        ),
    ),
    (
        REASON_WHOLESALE,
        re.compile(
            r"\b(wholesale|distributor|p\.?o\.?\b|purchase\s+order|wholesale\s+order|"
            r"reorder|restock\s+order|place\s+an?\s+order)\b",
            re.I,
        ),
    ),
    (
        REASON_DELIVERY,
        re.compile(
            r"\b(deliver\w*|drop(ping)?[\s-]?off|drop\s+off|pallet|i'?m\s+the\s+driver|"
            r"for\s+receiving|here\s+with\s+an?\s+order|unload\w*)\b",
            re.I,
        ),
    ),
)


def normalize_reason(raw: str) -> str:
    """Fold a caller's free-text reason into the stable enum. An already-valid enum value passes
    through; otherwise the first matching family wins; no match → ``"other"`` (never an exception,
    never a fabricated reason). Code owns the label (server-side; the LLM only fills the slot)."""
    s = (raw or "").strip().lower()
    if s in VENDOR_REASONS:
        return s
    for reason, pattern in _REASON_PATTERNS:
        if pattern.search(s):
            return reason
    return REASON_OTHER


def callback_window_text(cfg: str | None) -> str:
    """The spoken callback window — config/KB-sourced (Numbers-Guard). A non-empty ``cfg``
    (settings.HHT_VENDOR_CALLBACK_WINDOW or a StoreFact kind="vendor" value) is used verbatim;
    empty/None → the documented default. The LLM never originates this time (ADR-012)."""
    text = (cfg or "").strip()
    return text or DEFAULT_CALLBACK_WINDOW
