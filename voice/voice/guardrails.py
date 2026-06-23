"""Code-owned voice safety — version-controlled, NOT a prompt, NOT a UI toggle (ADR-014).

A prompt is not a security boundary; this module is. P0 ships and wires the leak wall
(``scrub_leak`` applied centrally in ``voice/tools/__init__.dispatch`` to EVERY tool result —
no per-tool opt-in) plus the age/scope scaffolds the later phases extend. The deterministic
keyword vetoes are authoritative; a Gemini second opinion (P1) only catches phrasing the
keywords miss — it can only STRENGTHEN safety, never weaken it.

Leak-Guard (ADR-008 / 23-SPEC §3.2): cost/margin can never reach a response the agent speaks.
This is the INVERSE of budtender's ``PUBLIC_PRODUCT_FIELDS`` allowlist — budtender never
serializes these, and this scrubber is the second wall in case a regression ever did.
``faq_lookup`` returns no product fields in P0, but the wall is shipped now so it guards the
surface before P1 adds products.
"""

from __future__ import annotations

import re

# Forbidden keys/substrings that must NEVER reach a tool result the agent speaks.
_FORBIDDEN_KEYS = frozenset(
    {
        "cost",
        "margin",
        "margin_pct",
        "margin_z",
        "velocity",
        "bucket",
        "bucket_source",
        "price_z",
    }
)
# Case-insensitive substring veto on string VALUES (a "38% margin" string nukes the result).
_FORBIDDEN_SUBSTR = ("cost", "margin")

_REDACTED = {"error": "redacted", "reason": "leak_blocked"}


class LeakError(RuntimeError):
    """Raised by ``assert_no_leak`` when a forbidden key/substring survives a scrub. Used by
    the contract test (and optionally as a belt-and-suspenders assert in DEBUG dispatch)."""


def _has_forbidden_substr(value: str) -> bool:
    low = value.lower()
    return any(sub in low for sub in _FORBIDDEN_SUBSTR)


def scrub_leak(payload):
    """Recursively drop any ``_FORBIDDEN_KEYS`` key at any depth; if any STRING value contains a
    forbidden substring, replace the ENTIRE result with the redacted-error stub (a hard fail
    beats speaking a leaked number — 23-SPEC §4.2). Returns the cleaned structure; guaranteed:
    no forbidden key, no "cost"/"margin" substring in any string value.

    Applied CENTRALLY in ``voice/tools/__init__.dispatch`` so a new tool cannot forget it."""
    if isinstance(payload, dict):
        cleaned = {}
        for key, val in payload.items():
            if key in _FORBIDDEN_KEYS:
                continue  # drop the forbidden key
            scrubbed = scrub_leak(val)
            if scrubbed is _REDACTED:  # a nested string leak nukes the whole result
                return dict(_REDACTED)
            cleaned[key] = scrubbed
        return cleaned
    if isinstance(payload, (list, tuple)):
        out = []
        for item in payload:
            scrubbed = scrub_leak(item)
            if scrubbed is _REDACTED:
                return dict(_REDACTED)
            out.append(scrubbed)
        return out
    if isinstance(payload, str) and _has_forbidden_substr(payload):
        return _REDACTED  # sentinel: bubble up to nuke the entire tool result
    return payload


def assert_no_leak(payload) -> None:
    """Raise ``LeakError`` if any forbidden key/substring survives. The contract-test gate
    (23-SPEC §7 AC-5) + an optional DEBUG belt-and-suspenders assert in dispatch."""

    def _walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if key in _FORBIDDEN_KEYS:
                    raise LeakError(f"forbidden key in tool result: {key!r}")
                _walk(val)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)
        elif isinstance(node, str) and _has_forbidden_substr(node):
            raise LeakError("forbidden substring (cost/margin) in tool result")

    _walk(payload)


# ── Age gate + scope (deterministic scaffolds; P1 wires the LLM second opinion) ───────────

# The agent answers cannabis-retail / FAQ / product topics only. Off-domain instruction
# phrasing → decline (P1) / escalate. Tuned to instruction/claim phrasing, not mere mention.
_OUT_OF_SCOPE = re.compile(
    r"\b(invest\w*|stock tip|legal advice|lawsuit|sue\b|tax (advice|return)|"
    r"immigration|how to (grow|make) (your own )?(dab|shatter|bho|concentrate))\b",
    re.IGNORECASE,
)
# A crisis utterance is NOT a flat decline — it routes to a human with a 911/988 line
# (23-SPEC §3.2 carve-out). Returned as ``reason="crisis"`` for the webhook to map to escalation.
_CRISIS = re.compile(r"\b(suicid\w+|self-?harm|kill myself|medical emergency)\b", re.IGNORECASE)


def age_gate_required(ctx) -> bool:
    """True until the call context records a 21+ confirmation. A code boundary, not a prompt
    line: P1 withholds suggestion-tool results while this is True (ADR-018). P0 ships the
    helper; the FAQ surface carries no purchasable product so it is informational here."""
    ctx = ctx or {}
    return not bool(ctx.get("age_confirmed"))


def in_scope(text: str) -> tuple[bool, str]:
    """Return ``(ok, reason)``. ``ok=False`` with ``reason="crisis"`` means route to a human;
    any other ``reason`` is an off-domain decline. Keyword-deterministic + version-controlled."""
    if _CRISIS.search(text or ""):
        return False, "crisis"
    m = _OUT_OF_SCOPE.search(text or "")
    if m:
        return False, f"out of scope: {m.group(0)}"
    return True, ""
