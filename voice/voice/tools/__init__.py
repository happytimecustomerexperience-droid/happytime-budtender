"""Tool-handler registry for Vapi/website shared tool dispatch."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

from voice import guardrails

logger = logging.getLogger(__name__)

TOOL_REGISTRY: dict[str, Callable[[dict, dict], dict]] = {}
_MAX_ARG_STRING = 500


def register(name: str):
    """Register a tool handler under its Vapi function name."""

    def _decorator(func: Callable[[dict, dict], dict]) -> Callable[[dict, dict], dict]:
        if name in TOOL_REGISTRY:
            logger.warning("tool %s already registered; overwriting", name)
        TOOL_REGISTRY[name] = func
        return func

    return _decorator


def dispatch(name: str, args: dict, ctx: dict) -> dict:
    """Route a tool call by name and scrub every result for leaks."""
    handler = TOOL_REGISTRY.get(name)
    if handler is None:
        logger.warning("unknown tool requested: %s", name)
        return {"error": "unknown_tool", "tool": name}
    try:
        result = handler(_sanitize_args(name, args or {}), ctx or {})
    except Exception:  # noqa: BLE001 - a handler error must not crash the webhook
        logger.exception("tool %s raised", name)
        return {"error": "tool_failed", "tool": name}
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


from voice.tools import faq  # noqa: E402,F401,I001
from voice.tools import suggest  # noqa: E402,F401,I001
from voice.tools import vendor  # noqa: E402,F401,I001
from voice.tools import escalation  # noqa: E402,F401,I001
from voice.tools import n8n  # noqa: E402,F401,I001
from voice.tools import phone_cart  # noqa: E402,F401,I001
