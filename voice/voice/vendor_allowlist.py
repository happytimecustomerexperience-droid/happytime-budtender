"""Vendor allowlist: an allowlisted vendor's call rings the owner's phone with no AI conversation.

The owner keeps a list of vendor numbers on /dashboard/vendor-allowlist/
(``dashboard.VendorAllowlistEntry``). On an inbound ``assistant-request`` the webhook asks
``direct_destination`` first. When ALL of these hold, it answers Vapi with a transfer destination
instead of an assistant:

  * the caller-ID normalises to a full US number (``+1`` + 10 digits, NANP-shaped),
  * that number EXACTLY equals an ACTIVE entry (no prefixes, no wildcards),
  * the owner's ``call.vendor_allowlist`` switch is on (default on),
  * the owner's phone (``HHT_OWNER_PHONE``, editable on the page as a Credential) is a valid US number.

Anything else (blank, withheld, anonymous, short or international caller-ID, no match, switch off,
no owner number, any error) returns ``None`` and the call is answered exactly as before: the AI
vendor agent asks the caller to hold, tries the store line, and takes a callback if nobody answers.
It never raises and never blocks a call.

Consult first (HHT_TRANSFER_CONSULT on, the default): the answer is a small per-call assistant
(``voice/consult.owner_consult_assistant``) that says one line, puts the vendor on hold and asks the
owner first, announcing the allowlist entry's name; the vendor is connected only if the owner says
yes, otherwise they hear the owner is unavailable and a callback message is taken. With the setting
off the answer is the old direct forward below.

The direct-forward shape is Vapi's documented "Transfer only (skip AI)" reply to ``assistant-request``:
``{"destination": {"type": "number", "number": "+1...", "message": "..."}}``; with a ``destination``
present Vapi ignores any assistant/squad and forwards the call
(https://docs.vapi.ai/server-url/events, "Retrieving Assistants" > "Transfer only (skip AI)").

Neither the caller's number nor the owner's is ever logged. The routed call is written as a
``VoiceCall`` with the peppered caller hash only, ``outcome=vendor_direct`` and ``reason="vendor"``.

Vapi only sends ``assistant-request`` while the phone number is bound to no assistant and no squad,
which is what provisioning does with HHT_DYNAMIC_GREETING on (``provision.phone_number_payload``).
With it off this module is never reached.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.conf import settings

from voice import capabilities

logger = logging.getLogger(__name__)

SWITCH = "call.vendor_allowlist"
REASON = "vendor"
# Spoken to the vendor by Vapi while it dials the owner.
TRANSFER_MESSAGE = "Thanks for calling Happy Time. Connecting you now, one moment."

# Digits plus the punctuation people write phone numbers with; anything else (letters, so
# "anonymous" / "restricted" / "unknown", or "sip:...") is not a number we route on.
_PHONE_CHARS = re.compile(r"[0-9\s().+\-]{1,32}")


def normalize_us_e164(value: object) -> str:
    """``+1XXXXXXXXXX`` for a full US/NANP number, else ``""``.

    Accepts ``(509) 555-1212``, ``509.555.1212``, ``15095551212`` and ``+1 509 555 1212``. Refuses
    blank, withheld/anonymous text, fewer or more digits, a ``+`` country code other than 1, an
    area code or exchange starting with 0 or 1, and anything with letters or other symbols."""
    raw = str(value or "").strip()
    if not raw or not _PHONE_CHARS.fullmatch(raw) or "+" in raw[1:]:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    elif len(digits) != 10 or raw.startswith("+"):  # "+509..." would be a non-US country code
        return ""
    if digits[0] in "01" or digits[3] in "01":
        return ""
    return "+1" + digits


def owner_number() -> str:
    """The owner's destination as US E.164, or ``""`` when unset/invalid. Never logged."""
    return normalize_us_e164(getattr(settings, "HHT_OWNER_PHONE", "") or "")


@dataclass(frozen=True)
class Decision:
    route: bool
    why: str  # owner-facing, for the dashboard test box (never contains a number)
    number: str = ""  # the normalised caller number ("" when it is not one)
    entry: object | None = None


def evaluate(number: object) -> Decision:
    """The one matcher: the webhook and the dashboard test box both call this. Reads only; it
    places nothing and records nothing."""
    from dashboard.models import VendorAllowlistEntry

    e164 = normalize_us_e164(number)
    if not e164:
        return Decision(False, "Not a full US phone number (blank, withheld, short or international), so the AI answers.")
    entry = VendorAllowlistEntry.objects.filter(phone=e164).first()
    if entry is None:
        return Decision(False, "Not on the allowlist, so the AI vendor agent answers.", e164)
    if not entry.active:
        return Decision(False, f"On the list as {entry.name}, but deactivated, so the AI answers.", e164, entry)
    if not capabilities.is_enabled("call.vendor_allowlist"):
        return Decision(False, f"On the list as {entry.name}, but the allowlist switch is off, so the AI answers.", e164, entry)
    if not owner_number():
        return Decision(False, f"On the list as {entry.name}, but no owner phone is set, so the AI answers.", e164, entry)
    return Decision(True, f"Rings the owner's phone directly (on the list as {entry.name}).", e164, entry)


def direct_destination(message: dict, store: str) -> dict | None:
    """The ``assistant-request`` answer for an allowlisted vendor, or ``None`` to answer as before.
    Never raises."""
    try:
        call = message.get("call") or {}
        raw = (call.get("customer") or {}).get("number", "")
        decision = evaluate(raw)
        owner = owner_number()
        if not decision.route or not owner:
            return None
        _record(str(call.get("id") or ""), store, raw, decision.entry)
        from voice import consult

        if consult.enabled():  # ask the owner first; a decline comes back for a callback message
            return {"assistant": consult.owner_consult_assistant(decision.entry, owner, store)}
        return {"destination": {"type": "number", "number": owner, "message": TRANSFER_MESSAGE}}
    except Exception:  # noqa: BLE001 - the allowlist must never cost a call; the AI answers instead
        logger.warning("vendor allowlist check failed; answering with the assistant", exc_info=True)
        return None


def _record(call_id: str, store: str, raw_number: str, entry) -> None:
    """Bump the entry's match stats and write the call row. Best-effort: a failed write is logged
    and the vendor still reaches the owner."""
    from django.db.models import F
    from django.utils import timezone

    from crm.models import phone_hash
    from dashboard.models import VendorAllowlistEntry
    from voice.models import Outcome, VoiceCall

    try:
        VendorAllowlistEntry.objects.filter(pk=entry.pk).update(
            match_count=F("match_count") + 1, last_matched_at=timezone.now()
        )
    except Exception:  # noqa: BLE001
        logger.warning("vendor allowlist: could not update match stats", exc_info=True)
    if not call_id:
        return
    try:
        VoiceCall.objects.update_or_create(
            call_id=call_id[:64],
            defaults={
                "store": store or "",
                # same hash the end-of-call report writes for this caller; never the raw number
                "caller_phone_hash": phone_hash(raw_number),
                "outcome": Outcome.VENDOR_DIRECT,
                "reason": REASON,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("vendor allowlist: could not write the call row", exc_info=True)


def routing_status() -> dict:
    """What the dashboard status line says: is assistant-request routing live, and if not, why."""
    from dashboard.models import VendorAllowlistEntry
    from voice import caller

    checks = {
        "dynamic_greeting": caller.dynamic_greeting(),
        "switch": capabilities.is_enabled("call.vendor_allowlist"),
        "owner": bool(owner_number()),
        "entries": VendorAllowlistEntry.objects.filter(active=True).count(),
    }
    checks["active"] = bool(
        checks["dynamic_greeting"] and checks["switch"] and checks["owner"] and checks["entries"]
    )
    return checks
