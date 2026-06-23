"""THE shared Vapi webhook contract — POST /api/voice/vapi (10-P0-CHASSIS-FAQ.md §4).

Every Vapi server message arrives as ``{"message": {...}}``; we dispatch on ``message.type``:
``assistant-request`` | ``tool-calls`` | ``status-update`` | ``end-of-call-report``. This is the
contract P1 (suggest/inventory/pairing), P2 (eocr enrichment), and P3 (vendor callback) all
consume — its shapes are frozen here (§4).

Security (ADR-019): ``signing.verify_signature`` runs FIRST and the whole path fails closed —
a missing/bad signature returns 401 BEFORE any handler parses intent (constant-time compare,
secret never logged). The view is ``@csrf_exempt`` because it is HMAC/secret-authed, NOT cookie-
authed (a cookie CSRF token makes no sense for a server-to-server Vapi callback).

R-2 FIX (tool-call field name varies across Vapi versions): the tool-call list is read
TOLERANTLY from BOTH ``message.toolCalls`` AND ``message.toolCallList`` and normalized to ONE
internal shape (see ``_extract_tool_calls`` — the canonical field is documented there).
"""

from __future__ import annotations

import json
import logging
import time

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from voice import guardrails, outcomes, signing
from voice.tools import dispatch as dispatch_tool

logger = logging.getLogger(__name__)


# ── Body / store helpers ──────────────────────────────────────────────────────


def _parse_body(request) -> dict:
    """Parse the JSON body once. request.body is already cached by the signature check, so this
    re-read is free; a malformed body yields ``{}`` (the dispatcher then 400s on a missing type)."""
    try:
        return json.loads(request.body or b"{}")
    except Exception:  # noqa: BLE001
        return {}


def _resolve_store(message: dict) -> str:
    """Resolve the caller's store. P0 default: a single inbound number → ``HHT_DEFAULT_STORE``
    (O-4). When per-store numbers land, map ``call.phoneNumberId`` → store here (one place)."""
    from django.conf import settings

    return getattr(settings, "HHT_DEFAULT_STORE", "yakima") or "yakima"


# ── R-2: tolerant tool-call extraction (the cross-version normalizer) ──────────


def _extract_tool_calls(message: dict) -> list[dict]:
    """Read the tool-call list TOLERANTLY and normalize to ONE internal shape (R-2 FIX).

    Vapi has shipped the array under two field names across versions:
      * ``message.toolCalls``     — the CANONICAL field name we target.
      * ``message.toolCallList``  — an older/alternate spelling seen on some payloads.
    We accept either (preferring ``toolCalls`` when both are present, which never happens in
    practice) and normalize each entry to ``{"id", "name", "arguments"}``. ``arguments`` may
    arrive as a dict or a JSON string — both are coerced to a dict so handlers see one shape."""
    raw = message.get("toolCalls")
    if not raw:
        raw = message.get("toolCallList")  # alternate field on some Vapi versions
    if not isinstance(raw, list):
        return []

    normalized: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function") or {}
        name = fn.get("name") or entry.get("name") or ""
        args = fn.get("arguments")
        if args is None:
            args = entry.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:  # noqa: BLE001
                args = {}
        if not isinstance(args, dict):
            args = {}
        normalized.append(
            {
                "id": entry.get("id") or entry.get("toolCallId") or "",
                "name": name,
                "arguments": args,
            }
        )
    return normalized


# ── Per-event handlers ─────────────────────────────────────────────────────────


def handle_assistant_request(message: dict) -> JsonResponse:
    """Vapi asks which assistant/overrides to use for an inbound call. Return the Squad-fronting
    assistant id + hydrated ``variableValues`` so no literal ``{{store_name}}`` ever ships (§4.2)."""
    from django.conf import settings

    from kb.models import AgentPrompt, StoreFact

    store = _resolve_store(message)

    assistant_id = ""
    prompt = AgentPrompt.objects.filter(role="faq", is_active=True).first()
    if prompt and prompt.vapi_assistant_id:
        assistant_id = prompt.vapi_assistant_id

    store_name = {
        "yakima": "Happy Time Yakima",
        "mount-vernon": "Happy Time Mt Vernon",
        "pullman": "Happy Time Pullman",
    }.get(store, "Happy Time")

    hours = ""
    hours_fact = StoreFact.objects.filter(
        store=store, kind="hours", confirmed=True, is_active=True
    ).first()
    if hours_fact:
        hours = hours_fact.value

    transfer = {
        "yakima": getattr(settings, "HHT_TRANSFER_NUMBER_YAKIMA", ""),
        "mount-vernon": getattr(settings, "HHT_TRANSFER_NUMBER_MTVERNON", ""),
        "pullman": getattr(settings, "HHT_TRANSFER_NUMBER_PULLMAN", ""),
    }.get(store, "")

    overrides = {
        "variableValues": {
            "store_name": store_name,
            "store_hours": hours,
            "transfer_number": transfer,
        }
    }
    body: dict = {"assistantOverrides": overrides}
    if assistant_id:
        body["assistantId"] = assistant_id
    return JsonResponse(body)


def handle_tool_calls(message: dict) -> JsonResponse:
    """The assistant invoked one or more tools. Route each by name through ``TOOL_REGISTRY``
    (centrally leak-scrubbed) and return the Vapi tool-result envelope (§4.3).

    P5 (#12) back-edge handling: when a tool-call's args carry a structured ``correction`` block
    (the caller revised a prior choice mid-flow), the server resets the affected slots
    DETERMINISTICALLY via ``voice.corrections`` BEFORE the tool runs — so the next
    ``suggest_products`` is internally consistent (a flower→edible switch clears size/strain_type,
    keeps effect/budget). Code owns the transition; the LLM only emits the signal."""
    store = _resolve_store(message)
    call = message.get("call") or {}
    # The raw caller number is passed transiently to ctx ONLY so P1's lazy returning-caller
    # recognition (voice/recognition.resolve_caller) can hash it + look up the budtender profile;
    # it is NEVER persisted (only the peppered hash is — PII discipline, 23-SPEC §3.5).
    customer = call.get("customer") or {}
    ctx = {
        "call_id": call.get("id", ""),
        "store": store,
        "caller_number": customer.get("number", ""),
    }

    results = []
    for tc in _extract_tool_calls(message):
        args = _apply_correction(tc["arguments"])
        result = dispatch_tool(tc["name"], args, ctx)
        # Belt-and-suspenders: the central scrub already ran in dispatch; assert the wall held.
        guardrails.assert_no_leak(result)
        results.append({"toolCallId": tc["id"], "result": result})
    return JsonResponse({"results": results})


def _apply_correction(args: dict) -> dict:
    """If the tool args carry a ``correction`` signal (§4.3), reset the affected slots in place and
    return the corrected args; otherwise return the args unchanged.

    The args already carry the slots the budtender member filled (category/size/effect/…); the
    ``correction`` block is ``{"kind","to","raw"}``. We treat the existing args as ``prev_slots``,
    build the plan from the signal, apply it, and drop the now-consumed ``correction`` key so it
    never reaches budtender. Pure + best-effort: a malformed signal leaves the args untouched
    (never raises into the turn)."""
    if not isinstance(args, dict):
        return args
    signal = args.get("correction")
    if not isinstance(signal, dict) or not signal:
        return args
    try:
        from voice import corrections

        prev = {k: v for k, v in args.items() if k != "correction"}
        plan = corrections.correction_from_signal(signal, prev)
        if plan is None:
            corrected = prev
        else:
            corrected = corrections.apply_correction(prev, plan)
        return corrected
    except Exception:  # noqa: BLE001 — a correction-parse error must not crash the turn
        logger.warning("correction signal apply failed; using raw args", exc_info=True)
        return {k: v for k, v in args.items() if k != "correction"}


def handle_status_update(message: dict) -> JsonResponse:
    """In-flight call state. Append a ``VoiceTurn`` when a transcript fragment is present so the
    P4 live-monitor + P5 latency p95 read from durable rows. Acks ``200 {}`` (§4.4)."""
    from voice.models import VoiceCall, VoiceTurn

    call = message.get("call") or {}
    call_id = call.get("id", "")
    transcript = message.get("transcript")
    role = message.get("role", "")
    if call_id and transcript:
        store = _resolve_store(message)
        vc, _ = VoiceCall.objects.get_or_create(call_id=call_id, defaults={"store": store})
        seq = vc.turns.count()
        VoiceTurn.objects.get_or_create(
            call=vc,
            seq=seq,
            defaults={"role": role or "assistant", "text": transcript},
        )
    return JsonResponse({})


def handle_end_of_call_report(message: dict) -> JsonResponse:
    """The durable record + summary + staff alert (ADR-017). Order is binding (§4.5):
    (1) synchronous idempotent ``VoiceCall`` upsert (record never lost) + turns + phone-hash;
    (2) inline summary; (3) ``crm.sinks.dispatch`` email digest. Always returns ``200 {}``."""
    from crm.models import phone_hash
    from voice.models import VoiceCall, VoiceTurn

    call = message.get("call") or {}
    call_id = call.get("id", "")
    if not call_id:
        return JsonResponse({})  # nothing addressable to persist

    store = _resolve_store(message)
    customer = call.get("customer") or {}
    raw_number = customer.get("number", "")  # transient — hashed, never stored raw
    transcript = message.get("transcript", "") or ""
    duration = message.get("durationSeconds")

    # P2 deterministic classification (code owns the label; the model only fills slots).
    outcome, reason = outcomes.classify_outcome(message, transcript)
    human_count = outcomes.human_requested_count(message, transcript)
    transferred, disposition = outcomes.transfer_disposition(message, reason)
    transfer_key = _transfer_key_for_store(store)

    # (1) Synchronous durable write — idempotent on call_id. The record survives even if the
    #     summary/email steps below fail.
    vc, _ = VoiceCall.objects.update_or_create(
        call_id=call_id,
        defaults={
            "store": store,
            "caller_phone_hash": phone_hash(raw_number),
            "transcript": transcript,
            "duration_s": duration,
            "outcome": outcome,
            "reason": reason,
            "escalated": transferred,
            "human_requested_count": human_count,
            "transfer_disposition": disposition,
            "transfer_number_key": transfer_key if transferred else "",
            "assistant_id": call.get("assistantId", "") or "",
        },
    )
    for idx, msg in enumerate(message.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        text = msg.get("message") or msg.get("content") or ""
        VoiceTurn.objects.update_or_create(
            call=vc,
            seq=idx,
            defaults={"role": msg.get("role", ""), "text": text},
        )

    # (2+3) Post-call work — summary + staff digest. The durable write above is ALREADY done, so
    # this is non-critical: ``run_post_call`` enqueues it on Celery when HHT_USE_CELERY=1 (the
    # webhook returns fast), else runs it INLINE exactly as P2 did (sync fallback; broker-free).
    # Never raises — a summary/email failure must not lose the durable record (ADR-017).
    try:
        from voice import tasks

        tasks.run_post_call(vc.pk)
    except Exception:  # noqa: BLE001 — post-call work must never lose the durable record
        logger.warning("eocr post-call work failed for %s", call_id, exc_info=True)

    return JsonResponse({})


_STORE_TRANSFER_KEY = {
    "yakima": "YAKIMA",
    "mount-vernon": "MTVERNON",
    "pullman": "PULLMAN",
}


def _transfer_key_for_store(store: str) -> str:
    """The HHT_TRANSFER_NUMBER_<KEY> key for the caller's store (default YAKIMA). Recorded on the
    VoiceCall so the staff email + dashboard show which store line the warm transfer targeted."""
    return _STORE_TRANSFER_KEY.get(store, "YAKIMA")


_DISPATCH = {
    "assistant-request": handle_assistant_request,
    "tool-calls": handle_tool_calls,
    "status-update": handle_status_update,
    "end-of-call-report": handle_end_of_call_report,
}


@csrf_exempt
@require_POST
def vapi_webhook(request):
    """The single Vapi webhook entrypoint. HMAC/secret-verify FIRST (fail-closed 401), then
    dispatch on ``message.type``. Stamps each handler's wall-time onto the most-recent turn so
    P5's latency p95 is durable."""
    ok, why = signing.verify_signature(request)
    if not ok:
        logger.warning("vapi webhook rejected: %s", why)  # never logs the secret
        return JsonResponse({"error": "unauthorized"}, status=401)

    body = _parse_body(request)
    message = body.get("message") or {}
    msg_type = message.get("type", "")
    handler = _DISPATCH.get(msg_type)
    if handler is None:
        logger.warning("unhandled vapi message type: %s", msg_type)
        return JsonResponse({"error": "unknown_type", "type": msg_type}, status=400)

    started = time.monotonic()
    response = handler(message)
    _stamp_latency(message, started)
    return response


def _stamp_latency(message: dict, started: float) -> None:
    """Record server-side handler time on the latest persisted turn (best-effort; never raises)."""
    try:
        from voice.models import VoiceCall

        call = message.get("call") or {}
        call_id = call.get("id", "")
        if not call_id:
            return
        vc = VoiceCall.objects.filter(call_id=call_id).first()
        if not vc:
            return
        turn = vc.turns.order_by("-seq").first()
        if turn and turn.latency_ms is None:
            turn.latency_ms = int((time.monotonic() - started) * 1000)
            turn.save(update_fields=["latency_ms"])
    except Exception:  # noqa: BLE001
        pass
