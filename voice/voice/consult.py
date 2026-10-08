"""Consult before connecting: every transfer to a REAL PERSON asks that person first.

The caller is put on hold, Vapi dials the person, and a short transfer assistant says who is calling
and why and asks whether they will take the call. Only a clear yes connects the caller
(``transferSuccessful``); a no, no answer, voicemail or an automated greeting cancels
(``transferCancel``), and the caller hears the code-owned "not available" line below and stays with
the agent, who offers to take a message (``notify_staff_issue`` / ``notify_vendor_callback``).

Vapi mechanism (VapiAI/docs ``fern/calls/assistant-based-warm-transfer.mdx`` and ``fern/apis/api/
openapi.json`` ``TransferPlan`` / ``TransferAssistant`` / ``TransferFallbackPlan``): a transferCall
destination with ``transferPlan.mode = "warm-transfer-experimental"``, a ``transferAssistant`` and a
``fallbackPlan`` with ``endCallEnabled: false`` (the caller is returned to the original assistant).
The accept signal is the person's SPOKEN answer, judged by the transfer assistant; there is no DTMF.

Two routes use it:
  * store transfers (``store_transfer_plan``): the concierge / vendor / escalation agent's built-in
    transferCall to a store line. The announcement template is fixed here; the caller's first name and
    reason come only from the last few lines of the conversation (``CONTEXT_MESSAGES``), where the
    agent has just repeated them, so the system prompt with the caller notes is not in that context.
  * the vendor allowlist (``owner_consult_assistant``): an allowlisted vendor is answered by a small
    per-call assistant that says one line and is put through by a ``call.timeElapsed`` hook; the owner
    hears the allowlist entry's name, fully built in code.

``HHT_TRANSFER_CONSULT=0`` restores the old behaviour everywhere (warm summary transfer, and the
allowlist's direct no-AI forward). What happened is read back from the end-of-call report by
``consult_result`` (voice/outcomes.py maps it to the disposition and the outcome).
"""

from __future__ import annotations

import re

from django.conf import settings

from voice import constants as C

MODE = "warm-transfer-experimental"
# How much of the conversation the transfer assistant sees: the agent's "one moment" line (which
# repeats the caller's name and reason), the caller's answer to "who should I say is calling and what
# is it about?", and the question itself. Never the system prompt (it carries the CALLER line and notes).
CONTEXT_MESSAGES = 4
MAX_SECONDS = 60  # the whole operator conversation
SILENCE_SECONDS = 20  # nobody speaks → cancel (no answer)
# The allowlist route: seconds after the call starts at which the hook puts the vendor through, so the
# one-line greeting is finished first.
OWNER_HOOK_SECONDS = 5

# ── what the CALLER hears (code-owned; the eocr reads these back) ──────────────────
STORE_HOLD_LINE = "One moment while I see if someone from the team is free."
STORE_UNAVAILABLE_LINE = (
    "I'm sorry, nobody from the team can take the call right now. I can take a message and have "
    "someone call you back."
)
OWNER_HOLD_LINE = "Thanks for calling Happy Time. One moment while I see if the owner is free."
OWNER_UNAVAILABLE_LINE = (
    "I'm sorry, the owner isn't available right now. I can take a message and have them call you "
    "back. What would you like me to pass along?"
)
UNAVAILABLE_LINES = (STORE_UNAVAILABLE_LINE, OWNER_UNAVAILABLE_LINE)

# ── what the PERSON being asked hears ─────────────────────────────────────────────
_ACCEPT_RULES = (
    "Decide with the two tools you have. Call transferSuccessful ONLY after the person clearly says "
    "yes (yes, sure, okay, put them through, I'll take it). Call transferCancel when they say no, not "
    "now, they're busy, or ask for a message instead; when you hear voicemail, a recorded greeting, a "
    "beep, an automated menu or a hold message; or when nobody answers. If the answer is unclear, ask "
    "once: \"Should I put them through, yes or no?\" and decide on the reply. Never say a phone number. "
    "Never mention purchases, preferences, notes, history or anything else about the caller beyond "
    "the name and the reason. Keep every turn to one short sentence."
)


def enabled() -> bool:
    """``HHT_TRANSFER_CONSULT`` (default on)."""
    return bool(getattr(settings, "HHT_TRANSFER_CONSULT", True))


def store_operator_prompt(spoken_store: str) -> str:
    """The transfer assistant's instructions for a store line: the announcement template is fixed
    here; only the caller's first name and reason are filled in, from the conversation's last lines."""
    return (
        f"You are calling the Happy Time {spoken_store} store team for a caller who is on hold. Your "
        "first words are exactly: \"Hi, it's the Happy Time phone line. I have NAME on the line about "
        "REASON. Can you take the call?\" NAME is the caller's first name and REASON is their reason in "
        "a few words, both only as the caller gave them in the conversation above. If they gave no "
        "name, say \"a caller\"; if no reason, say \"a question for the team\". " + _ACCEPT_RULES
    )


def _transfer_model(prompt: str) -> dict:
    # The same provider/model constants as the phone agents (the owner decides those; not changed here).
    return {
        "provider": C.ASSISTANT_PROVIDER,
        "model": C.ASSISTANT_MODEL,
        "messages": [{"role": "system", "content": prompt}],
    }


def store_transfer_plan(spoken_store: str) -> dict:
    """``transferPlan`` for a store destination: ask the team first, connect only on a yes."""
    return {
        "mode": MODE,
        "transferAssistant": {
            "firstMessageMode": "assistant-speaks-first-with-model-generated-message",
            "maxDurationSeconds": MAX_SECONDS,
            "silenceTimeoutSeconds": SILENCE_SECONDS,
            "model": _transfer_model(store_operator_prompt(spoken_store)),
        },
        "contextEngineeringPlan": {"type": "lastNMessages", "maxMessages": CONTEXT_MESSAGES},
        "fallbackPlan": {"message": STORE_UNAVAILABLE_LINE, "endCallEnabled": False},
    }


# A run of 4+ digits (with phone punctuation) is never spoken in an announcement.
_DIGITS = re.compile(r"\+?\d[\d\s().-]{2,}\d")


def _speakable(value: object) -> str:
    from voice.caller import _safe  # one sanitiser for every string put into a spoken line

    return " ".join(_DIGITS.sub(" ", _safe(value)).split())


def owner_announcement(vendor_name: object, note: object = "") -> str:
    """What the owner hears: built in code from the allowlist entry (name, plus its note as the
    reason). No phone number, no history."""
    name = _speakable(vendor_name) or "A vendor on your allowlist"
    about = _speakable(note)
    reason = f", about {about}" if about else ""
    return f"Hi, it's the Happy Time phone line. {name} is calling Happy Time{reason}. Do you want to take the call?"


def owner_transfer_plan(vendor_name: object, note: object = "") -> dict:
    """``transferPlan`` for the owner's phone: the announcement is fixed text, no conversation context."""
    return {
        "mode": MODE,
        "transferAssistant": {
            "firstMessage": owner_announcement(vendor_name, note),
            "firstMessageMode": "assistant-speaks-first",
            "maxDurationSeconds": MAX_SECONDS,
            "silenceTimeoutSeconds": SILENCE_SECONDS,
            "model": _transfer_model(
                "You are calling the owner of Happy Time for a vendor on the owner's allowlist who is on "
                "hold. You have already said who is calling. " + _ACCEPT_RULES
            ),
        },
        "contextEngineeringPlan": {"type": "none"},
        "fallbackPlan": {"message": OWNER_UNAVAILABLE_LINE, "endCallEnabled": False},
    }


def _owner_prompt(vendor_name: str) -> str:
    who = vendor_name or "a vendor on the owner's allowlist"
    return (
        "You are the warm, brief voice of Happy Time Weed (family-owned WA cannabis) answering for the "
        f"owner. {who} is calling. You have just said you are checking whether the owner is free; the "
        "call is put through to the owner automatically, so do not try to transfer it yourself.\n"
        f"If you hear yourself say \"{OWNER_UNAVAILABLE_LINE}\", the owner did not take it: take a "
        "message. Ask what it is about (a delivery, a wholesale order, a manifest, a sample drop, an "
        "invoice, or something else) and who it is from if they have not said, then call "
        "notify_vendor_callback with {store, reason, summary, caller_name} and say the callback window "
        "the tool returns, word for word. Never invent a time.\n"
        "This is a business call: never ask their age, never help them shop, never say a phone "
        "number, and never mention assistants, agents or systems. Keep every turn short, and never go "
        "quiet: if a tool is running, say \"one sec\" and then say its result."
    )


def owner_consult_assistant(entry, owner_number: str, store: str) -> dict:
    """The per-call assistant that answers an allowlisted vendor: one line, then (by hook) the owner
    is asked first; a decline / no answer / voicemail comes back here for a message."""
    from kb.models import AgentPrompt
    from voice import provision
    from voice.models import VapiObject

    name = _speakable(getattr(entry, "name", ""))
    destination = {
        "type": "number",
        "number": owner_number,
        "message": "",  # the caller already heard the hold line
        "description": "The owner of Happy Time.",
        "transferPlan": owner_transfer_plan(getattr(entry, "name", ""), getattr(entry, "note", "")),
    }
    tool_ids = []
    rec = VapiObject.objects.filter(kind="tool", name="notify_vendor_callback").first()
    if rec and rec.vapi_id:
        tool_ids.append(rec.vapi_id)
    voice_row = AgentPrompt.objects.filter(role=C.entry_role(), is_active=True).first()
    body = provision._with_runtime_safety(_owner_prompt(name), "vendor", store or None)
    return {
        "name": "allowlist_consult",
        "firstMessageMode": "assistant-speaks-first",
        "firstMessage": OWNER_HOLD_LINE,
        "model": {
            "provider": C.ASSISTANT_PROVIDER,
            "model": C.ASSISTANT_MODEL,
            "temperature": C.ASSISTANT_TEMPERATURE,
            "maxTokens": C.ASSISTANT_MAX_TOKENS,
            "messages": [{"role": "system", "content": body}],
            "toolIds": tool_ids,
        },
        "voice": provision._voice_block(voice_row),
        "transcriber": dict(C.DEEPGRAM_TRANSCRIBER),
        "server": provision._server_block(),
        "serverMessages": list(C.SERVER_MESSAGES),
        "hooks": [
            {
                "on": "call.timeElapsed",
                "options": {"seconds": OWNER_HOOK_SECONDS},
                "do": [{"type": "tool", "tool": {"type": "transferCall", "destinations": [destination]}}],
            }
        ],
    }


# ── reading back what happened (end-of-call report) ──────────────────────────────────
ACCEPTED, DECLINED, NO_ANSWER, VOICEMAIL, UNAVAILABLE = "accepted", "declined", "no_answer", "voicemail", "unavailable"
FAILED = (DECLINED, NO_ANSWER, VOICEMAIL, UNAVAILABLE)
_DECLINED_ENDINGS = ("warm-transfer-assistant-cancelled", "declined")
_NO_ANSWER_ENDINGS = (
    "no-answer", "did-not-answer", "operator-busy", "busy", "warm-transfer-silence-timeout",
    "warm-transfer-microphone-timeout", "warm-transfer-max-duration",
)
_WORDS = re.compile(r"[^a-z ]+")


def _plain(text: str) -> str:
    return " ".join(_WORDS.sub(" ", (text or "").lower().replace("’", "'").replace("'", "")).split())


def _texts(message: dict) -> str:
    parts = [str(message.get("transcript") or "")]
    for msg in message.get("messages") or []:
        if isinstance(msg, dict):
            parts.append(str(msg.get("message") or msg.get("content") or ""))
    return " ".join(parts)


def _transfer_called(message: dict) -> bool:
    for msg in message.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        blob = str(msg.get("name") or msg.get("toolName") or "") + str(msg.get("toolCalls") or "")
        if "transferCall" in blob or "transfer_call" in blob:
            return True
    return False


def fallback_heard(message: dict) -> bool:
    """The caller heard one of our "not available" lines (the transfer was cancelled)."""
    said = _plain(_texts(message))
    return any(_plain(line)[:60] in said for line in UNAVAILABLE_LINES)


def consult_result(message: dict) -> str:
    """``accepted`` | ``declined`` | ``no_answer`` | ``voicemail`` | ``unavailable`` (cancelled, Vapi
    did not say why) | ``""`` (no consult transfer happened, or nothing can be told)."""
    ended = str(message.get("endedReason") or "").lower()
    heard = fallback_heard(message)
    attempted = heard or bool(message.get("destination")) or _transfer_called(message) or "warm-transfer" in ended
    if not attempted:
        return ""
    if "voicemail" in ended:
        return VOICEMAIL
    if any(tok in ended for tok in _DECLINED_ENDINGS):
        return DECLINED
    if any(tok in ended for tok in _NO_ANSWER_ENDINGS):
        return NO_ANSWER
    if heard:
        return UNAVAILABLE
    if "forwarded-call" in ended or message.get("destination"):
        return ACCEPTED
    return ""
