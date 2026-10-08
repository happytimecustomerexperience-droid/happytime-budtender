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
            # FIX: compare by VALUE, not identity. A dict-branch nuke used to return
            # ``dict(_REDACTED)`` — a copy — so a list one level up whose items were
            # dicts checked ``is _REDACTED`` and never matched: the whole-result nuke
            # silently degraded to a per-item stub through a list. Equality bubbles the
            # nuke all the way up regardless of how many dict/list layers it passes
            # through.
            if scrubbed == _REDACTED:
                return dict(_REDACTED)
            cleaned[key] = scrubbed
        return cleaned
    if isinstance(payload, (list, tuple)):
        out = []
        for item in payload:
            scrubbed = scrub_leak(item)
            if scrubbed == _REDACTED:
                return dict(_REDACTED)
            out.append(scrubbed)
        return out
    if isinstance(payload, str) and _has_forbidden_substr(payload):
        return dict(_REDACTED)  # sentinel: bubble up to nuke the entire tool result
    return payload


# Phone-like digit run (7+ digits with optional +, spaces, dashes, parens) — masked before any
# tool-call arg / fetched transcript is PERSISTED (PII discipline, 23-SPEC §3.5: the DB keeps only
# the peppered hash; a number a caller spoke into a tool arg must never land in cleartext).
# Excludes a run immediately after "WAC "/"RCW " (case-insensitive) — a legal citation like
# "WAC 314-55-079" or dot-separated "RCW 69.50.535" is dash/dot/digit-shaped exactly like a phone
# number and would otherwise be swallowed whole. ``(?<!\d)`` blocks the match from re-anchoring
# mid-run one digit later (e.g. matching "14-55-079" once "WAC 3..." is excluded) — without it the
# WAC/RCW exclusion only protects the first attempt, not the whole citation.
_PHONE_RE = re.compile(r"(?<!\d)(?<!WAC )(?<!RCW )\+?\d[\d\-.\s()]{5,}\d", re.IGNORECASE)

# Date-of-birth-shaped value: MM/DD/YYYY, MM-DD-YYYY, or YYYY/MM/DD, YYYY-MM-DD — a caller reading
# out a birthdate for age verification. Requires a 4-digit year in the first or last slot so it
# can't fire on multi-part numeric codes that don't carry a plausible year (a legal citation like
# "WAC 314-55-079" has no 4-digit group and is untouched; "RCW 69.50.535" uses dots, not / or -,
# so it never reaches this pattern at all).
_DOB_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b"
)

# Street-address-shaped value: a house number, an optional compass prefix (N/S/E/W/NE/...), 1-3
# words (letters/digits — covers ordinals like "1st"), then a recognized street-type suffix. A
# caller's delivery/callback address said out loud on the call. KNOWN TRADEOFF: this also masks
# the STORE's own address when the agent reads it back (e.g. "1315 N 1st St") — accepted, since
# the redactor has no way to distinguish caller-address from store-address, and over-masking a
# non-secret is preferable to leaving a caller's home address in the clear.
_STREET_SUFFIX = (
    r"St(?:reet)?|Ave(?:nue)?|Rd|Road|Blvd|Boulevard|Dr(?:ive)?|Ln|Lane|Ct|Court|Way|"
    r"Pl(?:ace)?|Cir(?:cle)?|Ter(?:race)?|Pkwy|Parkway|Hwy|Highway|Loop|Trail"
)
_ADDRESS_RE = re.compile(
    rf"\b\d{{1,5}}\s+(?:[NSEW]{{1,2}}\s+)?(?:[A-Za-z0-9]+\s+){{1,3}}(?:{_STREET_SUFFIX})\b\.?",
    re.IGNORECASE,
)


# Email address — owner-editable KB rows already avoid these, but a caller reading one out
# ("email me at jane.doe@example.com") must not land in a stored transcript/turn in cleartext.
# Bounded runs (RFC 5321: local part <= 64, a label <= 63): the unbounded ``[\w.+-]+@`` rescanned
# the whole run from every start position, so a 20 KB "a.a.a.…" transcript took over a second.
_EMAIL_RE = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}\.[A-Za-z0-9.-]{1,255}\b")

# Spoken-digit phone number: a caller reading digits out loud one at a time ("five oh nine,
# two two two, one two three four") never matches ``_PHONE_RE`` (no actual digit characters).
# Require 7+ consecutive number-words separated by whitespace/commas/"and" — long enough that
# normal speech ("one eighth", "two for one") can't accidentally trip it.
# "triple five", "double four" are one spoken digit group each (voice/chat.py parses them the same way).
_DIGIT_WORD = r"(?:(?:double|triple)\s+)?(?:oh|zero|one|two|three|four|five|six|seven|eight|nine)"
_SPOKEN_PHONE_RE = re.compile(
    rf"\b{_DIGIT_WORD}(?:[\s,]+(?:and\s+)?{_DIGIT_WORD}){{6,}}\b", re.IGNORECASE
)

# "my name is <Name>" — a caller self-identifying. Narrow on purpose (1-2 capitalized-looking
# words right after the trigger phrase) so it doesn't eat the rest of the sentence.
_NAME_RE = re.compile(r"\bmy name is\s+([A-Za-z'-]+(?:\s+[A-Za-z'-]+){0,1})", re.IGNORECASE)


def redact_pii(payload):
    """Structure-preserving mask of phone-like digit runs, DOB-shaped dates, and street addresses
    in every string value. Defense-in-depth for stored tool-call args + transcripts fetched from
    Vapi. Deliberately narrow: prices, weights/doses, legal citations, hours, and percentages are
    NOT touched (see the must-not-redact regression tests) — a greedy matcher here would mangle
    every stored call log. Returns a cleaned copy."""
    if isinstance(payload, dict):
        return {k: redact_pii(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [redact_pii(v) for v in payload]
    if isinstance(payload, str):
        masked = _ADDRESS_RE.sub("[redacted]", payload)
        masked = _DOB_RE.sub("[redacted]", masked)
        masked = _PHONE_RE.sub("[redacted]", masked)
        masked = _EMAIL_RE.sub("[redacted]", masked)
        masked = _SPOKEN_PHONE_RE.sub("[redacted]", masked)
        masked = _NAME_RE.sub("my name is [redacted]", masked)
        return masked
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
# 2026-10-01: widened to the ways people actually say it ("I want to die", "end my life", "no
# reason to live") and to a lethality question ("how many would it take to kill me", "will a
# whole bag of gummies kill me"). A bare "these gummies are gonna kill me" is an idiom and is
# NOT a crisis on its own — "kill me" counts only beside a despair cue (``_DESPAIR``) or in a
# lethality question. Fail-safe by design: a nervous "would one gummy kill me" also gets the
# crisis line, which is conditional ("If you're thinking about hurting yourself...").
_CRISIS = re.compile(
    r"\b(suicid\w*|self[\s-]?harm\w*|kill(?:ing)?\s+myself|end(?:ing)?\s+(?:my\s+(?:own\s+)?life|it\s+all)|"
    r"take\s+my\s+(?:own\s+)?life|(?:want|wanna|wanting)\s+(?:to\s+)?die|"
    r"don'?t\s+want\s+to\s+(?:live|be\s+alive|be\s+here|wake\s+up)|better\s+off\s+dead|"
    r"(?:hurt|harm)\s+myself|nothing\s+(?:left\s+)?to\s+live\s+for|"
    r"no\s+(?:reason|point)\s+(?:to|in)\s+(?:live|living|going\s+on)|"
    # Same clause only (no comma): "can I get something cheaper, these prices kill me" is an idiom.
    r"how\s+(?:many|much)\b[^.?!,]{0,60}\b(?:to\s+die|kill\s+me|lethal|(?:to\s+)?overdose)|"
    r"(?:will|would|could|can|enough\s+to)\b[^.?!,]{0,60}\bkill\s+me|"
    r"medical emergency)\b",
    re.IGNORECASE,
)
_KILL_ME = re.compile(r"\bkill(?:s|ing)?\s+me\b", re.IGNORECASE)
_DESPAIR = re.compile(
    r"\b(?:done\s+with\s+(?:everything|it\s+all|life)|can'?t\s+(?:go\s+on|do\s+this\s+anymore|"
    r"take\s+(?:it|this)\s+anymore)|give\s+up\s+on\s+(?:life|everything)|hopeless)\b",
    re.IGNORECASE,
)


def _is_crisis(text: str) -> bool:
    text = text or ""
    return bool(_CRISIS.search(text) or (_KILL_ME.search(text) and _DESPAIR.search(text)))


def age_gate_required(ctx) -> bool:
    """True until the call context records a 21+ confirmation. A code boundary, not a prompt
    line: P1 withholds suggestion-tool results while this is True (ADR-018). P0 ships the
    helper; the FAQ surface carries no purchasable product so it is informational here."""
    ctx = ctx or {}
    return not bool(ctx.get("age_confirmed"))


def in_scope(text: str) -> tuple[bool, str]:
    """Return ``(ok, reason)``. ``ok=False`` with ``reason="crisis"`` means route to a human;
    any other ``reason`` is an off-domain decline. Keyword-deterministic + version-controlled."""
    if _is_crisis(text):
        return False, "crisis"
    m = _OUT_OF_SCOPE.search(text or "")
    if m:
        return False, f"out of scope: {m.group(0)}"
    return True, ""
