"""Fetch the authoritative full conversation from Vapi (P6 ``/full-conversation``).

The webhook's ``end-of-call-report`` already stores a transcript + reconstructed turns, but the
canonical record (the exact transcript + the per-message tool-call timeline) lives on Vapi. This
module pulls ``GET /call/{id}`` and persists:
  * the full transcript + summary onto the ``VoiceCall`` row (created if missing), PII-masked;
  * every tool invocation + its result (from ``artifact.messages``) into ``VoiceToolCall``
    (``source="vapi_fetch"``), so the dashboard tool-call audit is complete even for calls whose
    live ``tool-calls`` webhook never landed.

Used by the ``full_conversation`` management command and the dashboard "Fetch full conversation"
action. Read-only against Vapi; safe to re-run (idempotent upserts).
"""

from __future__ import annotations

import json
import logging

from core.services import vapi
from voice import guardrails

logger = logging.getLogger(__name__)


def _coerce_args(value) -> dict:
    """Tool-call arguments arrive as a dict or a JSON string — normalize to a dict."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def parse_tool_calls(messages: list) -> list[dict]:
    """Extract ``{tool_call_id, name, args, result}`` rows from a Vapi ``artifact.messages`` list.

    Tolerant of the exact role spelling (the spec doesn't enum-constrain the literals): a message
    carrying ``toolCalls`` is an invocation; a message carrying ``toolCallId`` + ``result`` is a
    result. Results are matched to invocations by ``toolCallId``."""
    invocations: dict[str, dict] = {}
    order: list[str] = []
    results: dict[str, object] = {}

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        # An invocation message (one or more tool calls).
        for tc in msg.get("toolCalls") or msg.get("toolCallList") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            tcid = tc.get("id") or tc.get("toolCallId") or ""
            name = fn.get("name") or tc.get("name") or ""
            key = tcid or f"{name}:{len(order)}"
            if key not in invocations:
                order.append(key)
            invocations[key] = {
                "tool_call_id": tcid,
                "name": name,
                "args": _coerce_args(fn.get("arguments", tc.get("arguments"))),
            }
        # A result message.
        rid = msg.get("toolCallId")
        if rid is not None and ("result" in msg or msg.get("role") == "tool_call_result"):
            results[rid] = msg.get("result")

    rows = []
    for key in order:
        inv = invocations[key]
        rows.append({**inv, "result": results.get(inv["tool_call_id"])})
    return rows


def fetch_full_conversation(call_id: str) -> dict:
    """Pull ``GET /call/{id}`` and persist transcript + summary + tool calls. Returns a dict
    ``{call_id, transcript, summary, messages, tool_calls, persisted}`` for the caller to render.

    Raises ``vapi.VapiError`` on a transport/HTTP failure (the caller surfaces it); a successful
    fetch with an empty artifact returns empty fields rather than raising."""
    raw = vapi.get_call(call_id) or {}
    artifact = raw.get("artifact") or {}
    transcript = guardrails.redact_pii(artifact.get("transcript") or "")
    messages = artifact.get("messages") or []
    summary = (raw.get("analysis") or {}).get("summary") or ""
    tool_calls = parse_tool_calls(messages)

    persisted = _persist(call_id, raw, transcript, summary, tool_calls)
    return {
        "call_id": call_id,
        "transcript": transcript,
        "summary": summary,
        "messages": messages,
        "tool_calls": tool_calls,
        "persisted": persisted,
    }


def _persist(call_id: str, raw: dict, transcript: str, summary: str, tool_calls: list[dict]) -> dict:
    """Upsert the VoiceCall transcript/summary + the tool-call rows. Best-effort, idempotent."""
    from voice.models import VoiceCall, VoiceToolCall

    vc, _ = VoiceCall.objects.get_or_create(
        call_id=call_id,
        defaults={"assistant_id": raw.get("assistantId", "") or ""},
    )
    fields: list[str] = []
    if transcript:
        vc.transcript = transcript
        fields.append("transcript")
    if summary and not vc.ai_summary:
        vc.ai_summary = summary
        fields.append("ai_summary")
    if fields:
        fields.append("updated_at")
        vc.save(update_fields=fields)

    n = 0
    for tc in tool_calls:
        VoiceToolCall.objects.update_or_create(
            call_id=call_id,
            tool_call_id=tc.get("tool_call_id") or "",
            name=tc.get("name") or "",
            defaults={
                "args": guardrails.redact_pii(guardrails.scrub_leak(tc.get("args") or {})),
                "result": guardrails.scrub_leak(tc.get("result") or {}),
                "store": vc.store or "",
                "source": "vapi_fetch",
            },
        )
        n += 1
    return {"voice_call": vc.pk, "tool_calls": n}
