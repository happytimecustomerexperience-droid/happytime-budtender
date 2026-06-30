"""The tool-handler registry (ADR-020) — the parallel-safe dispatch scaffold.

Each phase adds ONE module + ONE import line here; no phase edits another's handler body.
P0 ships ``faq`` (self-registers ``faq_lookup``); P1 appends ``from . import suggest``, P3
``from . import vendor``. ``dispatch`` routes by the Vapi ``function.name`` through
``TOOL_REGISTRY`` and applies ``guardrails.scrub_leak`` to EVERY result centrally — one leak
choke-point, no per-tool opt-in (23-SPEC §3.6). An unknown tool returns a structured
``{"error": "unknown_tool"}`` (never a 500).

A handler signature is ``handler(args: dict, ctx: dict) -> dict`` where ``ctx`` carries call
context (``call_id``, ``store``, ``caller_phone_hash``, …). Handlers return KB/budtender values
only — they never compose a figure (Numbers-Guard, ADR-012).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

from voice import guardrails

logger = logging.getLogger(__name__)

# name -> handler. Populated by ``@register`` at import time.
TOOL_REGISTRY: dict[str, Callable[[dict, dict], dict]] = {}
_MAX_ARG_STRING = 500


def register(name: str):
    """Decorator: register a tool handler under its Vapi ``function.name``."""

    def _decorator(func: Callable[[dict, dict], dict]) -> Callable[[dict, dict], dict]:
        if name in TOOL_REGISTRY:
            logger.warning("tool %s already registered; overwriting", name)
        TOOL_REGISTRY[name] = func
        return func

    return _decorator


def dispatch(name: str, args: dict, ctx: dict) -> dict:
    """Route a tool call by name through the registry; scrub every result for leaks.

    Unknown tool → ``{"error": "unknown_tool"}`` (never raises a 500). A handler exception is
    caught and returned as a structured error so one bad tool never crashes the webhook."""
    handler = TOOL_REGISTRY.get(name)
    if handler is None:
        logger.warning("unknown tool requested: %s", name)
        return {"error": "unknown_tool", "tool": name}
    try:
        result = handler(_sanitize_args(name, args or {}), ctx or {})
    except Exception:  # noqa: BLE001 — a handler error must not crash the webhook
        logger.exception("tool %s raised", name)
        return {"error": "tool_failed", "tool": name}
    # Layer-2 leak wall, applied centrally to every tool result (23-SPEC §3.6).
    return guardrails.scrub_leak(result)


def _sanitize_args(name: str, args: dict) -> dict:
    """Minimal server-side schema wall for Vapi tool args."""
    from voice.constants import TOOL_SPECS

    spec = ((TOOL_SPECS.get(name) or {}).get("parameters") or {}).get("properties") or {}
    if not spec:
        return args if isinstance(args, dict) else {}
    clean = {}
    for key, rule in spec.items():
        if key not in args:
            continue
        value = args[key]
        typ = rule.get("type")
        enum = set(rule.get("enum") or [])
        if typ == "string":
            value = " ".join(str(value or "").split())[:_MAX_ARG_STRING]
            if enum and value not in enum:
                continue
        elif typ == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                continue
        elif typ == "boolean":
            if not isinstance(value, bool):
                continue
        clean[key] = value
    return clean


# Self-register P0's handlers. Each later phase appends ONE import line below (kept as separate
# single-line imports so a parallel worktree's addition is a one-line diff, not a merge conflict).
from voice.tools import faq  # noqa: E402,F401,I001  (registers faq_lookup)
from voice.tools import suggest  # noqa: E402,F401,I001  (P1 — suggest_products/check_inventory/pair_upsell)
from voice.tools import vendor  # noqa: E402,F401,I001  (P3 — notify_vendor_callback)
from voice.tools import escalation  # noqa: E402,F401,I001  (Phase 1 — notify_staff_issue)
from voice.tools import n8n  # noqa: E402,F401,I001  (P6 — notify_n8n, bot-callable n8n trigger)
