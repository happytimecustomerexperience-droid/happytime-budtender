"""The transfer heads-up: one short note to the staff who are about to take a transferred call.

Vapi sends a ``status-update`` with ``status="forwarding"`` when a transferCall starts.
``voice.webhooks.handle_status_update`` hands that message to ``heads_up`` here, which composes ONE
deterministic line (no LLM) — store, the caller's first name only if they said it, the last four
digits of their number, what they want, whether they are a known customer — and sends it through
``crm.sinks.deliver_transfer_notice`` (Pushover push, Slack, email).

Gates, every one required, in this order:
  1. the owner switch ``call.sms_on_transfer`` (default OFF);
  2. the store: the dialled number must equal exactly one store's configured transfer number —
     otherwise we do not know whose staff to tell, and we say nothing (never guess a store);
  3. the test-session suppression in ``crm.sinks`` (eval / playground / simulator never page anyone);
  4. the ``AlertDelivery`` ledger, one row per ``(call, "xfer:<seq>")`` — a redelivered webhook never
     sends twice; at most ``PER_CALL_CAP`` notices per call and ``HHT_TRANSFER_NOTICE_DAILY_CAP`` in
     any 24 hours (counted from the ledger), over which we log and skip.

PII: the caller's number is read in-request for its last four digits and the known-customer lookup,
and is never stored; the name and summary go only into the outgoing text.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from crm import sinks
from voice import capabilities, guardrails, recognition
from voice import constants as C

logger = logging.getLogger(__name__)

PER_CALL_CAP = 3
DEFAULT_DAILY_CAP = 50
MAX_LEN = 300
SUMMARY_MAX = 140

_LEDGER_PREFIX = "xfer:"  # AlertDelivery.sink is max 24 chars: "xfer:" + a short sequence

# ── which store did the call go to ──────────────────────────────────────────────────────────────


def store_for_number(number) -> tuple[str, str] | None:
    """``(settings key, store slug)`` of the store whose configured transfer number is ``number``,
    compared as E.164. ``None`` when nothing matches or two stores share the number — never a guess."""
    dialled = recognition.normalize_e164(str(number or ""))
    if not dialled:
        return None
    hits = [
        (key, slug)
        for key, slug in C.TRANSFER_STORES
        if recognition.normalize_e164(getattr(settings, f"HHT_TRANSFER_NUMBER_{key}", "") or "") == dialled
    ]
    return hits[0] if len(hits) == 1 else None


# ── composing the line ──────────────────────────────────────────────────────────────────────────

# "my name is Maria" / "this is Maria" — the trigger is case-blind, the name must be capitalised and
# letters only (speech-to-text capitalises a spoken name, and "this is a problem" must not match).
_NAME_RE = re.compile(r"(?i:\bmy name is|\bmy name's|\bthis is)\s+([A-Z][a-z]{1,19})\b")
# Capitalised words that follow "this is" without being a name ("This is Yakima", "This is Not...").
_NOT_NAMES = frozenset(
    "Yakima Pullman Mount Vernon Happy Time Weed Store Calling Regarding About Concerning Just Still "
    "Not Really Very Sorry Okay Ok Hello Hi Hey Please Thanks Thank Today Tomorrow Monday Tuesday "
    "Wednesday Thursday Friday Saturday Sunday".split()
)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\S+\.(?:com|net|org|io|co|us|gov|edu|app)\b\S*", re.IGNORECASE)
_DIGIT_RUN_RE = re.compile(r"\+?\d[\d\-.\s()]{4,}\d")  # digits with phone separators; stripped at 7+ digits
_ASCII_FOLD = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
     "\u2026": "...", "\u00a0": " "}
)


def _ascii(text: str) -> str:
    """Plain 7-bit text: curly quotes, dashes and the ellipsis become their ASCII forms, accents are
    dropped, control characters and newlines become spaces."""
    text = text.translate(_ASCII_FOLD)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7e]", " ", text)).strip()


def first_name(messages) -> str:
    """The caller's first name if THEY said it ("my name is X" / "this is X" in a user turn), else
    ``""``. Deterministic: first match across the user's turns, letters only."""
    for msg in messages if isinstance(messages, list) else []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = msg.get("message") or msg.get("content") or ""
        if not isinstance(text, str):
            continue
        for match in _NAME_RE.finditer(text):
            if match.group(1) not in _NOT_NAMES:
                return match.group(1)
    return ""


def clean_summary(text) -> str:
    """What the caller wants, as one short plain line: URLs and phone-like digit runs removed, PII
    and cost/margin wording scrubbed with the shared guardrails, ASCII only, at most ``SUMMARY_MAX``."""
    text = _URL_RE.sub(" ", str(text or ""))
    text = _DIGIT_RUN_RE.sub(lambda m: " " if sum(c.isdigit() for c in m.group()) >= 7 else m.group(), text)
    text = guardrails.redact_pii(text)
    if isinstance(guardrails.scrub_leak(text), dict):  # a cost/margin word anywhere → drop the lot
        return ""
    text = _ascii(text)[:SUMMARY_MAX].rstrip(" ,;:-.!?")
    return text


def _last4(number) -> str:
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-4:] if len(digits) >= 4 else "unknown"


def _known_customer(number, call_id: str, call_store: str) -> str:
    """``yes`` / ``no`` from the returning-caller lookup; ``unknown`` whenever no lookup really
    happened (no usable number, recognition switched off, budtender not configured, any error) —
    never a ``no`` that means "we did not look"."""
    if not recognition.normalize_e164(str(number or "")):
        return "unknown"
    if not capabilities.is_enabled("call.recognize_caller"):
        return "unknown"
    if not (getattr(settings, "HHT_BUDTENDER_BASE_URL", "") and getattr(settings, "HHT_BACKEND_TOKEN", "")):
        return "unknown"
    try:
        ctx = recognition.resolve_caller(str(number), {"call_id": call_id, "store": call_store})
    except Exception:  # noqa: BLE001 — a lookup error is "unknown", never a failed notice
        logger.warning("transfer heads-up: caller lookup failed", exc_info=True)
        return "unknown"
    return "yes" if ctx.get("known") else "no"


def compose(message: dict, store_slug: str, call_store: str) -> str:
    """The one-line heads-up for a ``forwarding`` status-update. ASCII only, at most ``MAX_LEN``."""
    call = message.get("call") or {}
    call_id = str(call.get("id") or "")
    number = (message.get("customer") or {}).get("number") or (call.get("customer") or {}).get("number") or ""
    name = first_name(message.get("messages"))
    if isinstance(guardrails.scrub_leak(name), dict):  # a name that contains "cost"/"margin"
        name = ""
    name = name or "name not given"
    summary = clean_summary(message.get("summary")) or "no summary"
    text = (
        f"HT {C.spoken_store(store_slug)}: transfer incoming. {name} (ends {_last4(number)}). "
        f"Wants: {summary}. Known customer: {_known_customer(number, call_id, call_store)}. ref {call_id[:6]}"
    )
    return _ascii(text)[:MAX_LEN]


# ── the gates + the ledger ──────────────────────────────────────────────────────────────────────


def _is_transfer_entry(msg) -> bool:
    if not isinstance(msg, dict):
        return False
    if "transferCall" in (msg.get("toolName"), msg.get("name")):
        return True
    calls = msg.get("tool_calls") or msg.get("toolCalls") or []
    return any(
        isinstance(tc, dict) and "transferCall" in (tc.get("name"), (tc.get("function") or {}).get("name"))
        for tc in calls
    )


def _sequence(message: dict) -> str:
    """Which transfer of this call this is: the number of transferCall entries in the conversation so
    far (a redelivery of the same status-update carries the same list → the same number). Falls back
    to the message timestamp (digits only) when the conversation has none."""
    messages = message.get("messages")
    count = sum(_is_transfer_entry(m) for m in messages) if isinstance(messages, list) else 0
    if count:
        return str(count)
    return re.sub(r"\D", "", str(message.get("timestamp") or ""))[:19] or "0"


def _daily_cap() -> int:
    try:
        return max(0, int(str(getattr(settings, "HHT_TRANSFER_NOTICE_DAILY_CAP", DEFAULT_DAILY_CAP)).strip()))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def heads_up(message: dict, *, call_store: str) -> str:
    """Send the transfer heads-up for one ``forwarding`` status-update. Returns a short outcome word
    (``sent`` / ``off`` / ``duplicate`` / ``capped`` / ...) for logs and tests. NEVER raises: the
    webhook must answer Vapi whatever happens here."""
    try:
        return _heads_up(message, call_store)
    except Exception:  # noqa: BLE001
        logger.warning("transfer heads-up failed", exc_info=True)
        return "error"


def _heads_up(message: dict, call_store: str) -> str:
    from crm.models import AlertDelivery
    from voice.models import VoiceCall

    if not capabilities.is_enabled("call.sms_on_transfer"):
        return "off"
    call_id = str((message.get("call") or {}).get("id") or "")
    if not call_id:
        return "no-call-id"
    match = store_for_number((message.get("destination") or {}).get("number"))
    if match is None:
        logger.warning("transfer heads-up for %s skipped: the dialled number is no store's transfer number", call_id)
        return "unknown-store"
    store_key, store_slug = match

    voice_call, _ = VoiceCall.objects.get_or_create(call_id=call_id, defaults={"store": call_store})
    suppressed = sinks._suppression_reason(voice_call)
    if suppressed:
        logger.info("transfer heads-up for %s suppressed: %s", call_id, suppressed)
        return "suppressed"

    sink = f"{_LEDGER_PREFIX}{_sequence(message)}"[:24]
    ledger = AlertDelivery.objects.filter(sink__startswith=_LEDGER_PREFIX)
    if ledger.filter(voice_call=voice_call, sink=sink).exists():
        return "duplicate"
    if ledger.filter(voice_call=voice_call).count() >= PER_CALL_CAP:
        logger.warning("transfer heads-up for %s skipped: %d notices already sent for this call", call_id, PER_CALL_CAP)
        return "capped"
    day_start = timezone.now() - timedelta(hours=24)
    if ledger.filter(status__in=("success", "failed"), created_at__gte=day_start).count() >= _daily_cap():
        logger.warning("transfer heads-up for %s skipped: daily cap of %d reached", call_id, _daily_cap())
        return "capped"

    row, created = AlertDelivery.objects.get_or_create(voice_call=voice_call, sink=sink)
    if not created:  # a concurrent delivery of the same status-update claimed it first
        return "duplicate"
    row.attempts = 1
    try:
        results = sinks.deliver_transfer_notice(store_key, store_slug, compose(message, store_slug, call_store))
    except Exception as exc:  # noqa: BLE001
        row.status, row.last_error = "failed", str(exc)[:500]
        row.save()
        raise
    row.status = (
        "success" if "sent" in results.values()
        else "failed" if any(r.startswith("failed") for r in results.values())
        else "skipped"
    )
    row.last_error = "; ".join(f"{ch}: {r}" for ch, r in results.items() if r != "sent")[:500]
    row.save()
    return {"success": "sent", "failed": "failed", "skipped": "no-channel"}[row.status]
