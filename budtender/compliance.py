"""The ONE screen for operator-typed text before it can reach a customer or an LLM prompt.

Two independent checks, both fail-closed (a caller DROPS the field whole on any hit):

  therapeutic_hits  curative / therapeutic wording. happytimeweed/LCB-CONTENT-COMPLIANCE.md section 2.1
                    (WAC 314-55-155(1)(a)(iii)) forbids implying cannabis treats a condition; only
                    experiential words (relaxing, uplifting, calm, focused...) are allowed.
  injection_hits    text that tries to talk to a model: instruction overrides, role/prompt probes,
                    links, template and code markers, raw markup.

Pure: no Django, no I/O. Used by product_detail (every free-text `info` field and tag) and swept over
every string the lab/profile code can produce (tests/test_terpenes).
"""
from __future__ import annotations

import re

# Word-boundary patterns drawn from LCB-CONTENT-COMPLIANCE.md 2.1 / the vocabulary table in section 5.
# "cured"/"curing" (post-harvest drying) are deliberately NOT matched: LCB allows them.
_THERAPEUTIC = re.compile(r"\b(?:" + "|".join((
    r"treat(?:s|ed|ing|ment|ments)?", r"cures?", r"heal(?:s|ed|ing|er)?",
    r"medic(?:ine|ines|inal|ation|ations|al)", r"therap(?:eutic|eutics|y|ies|ist)", r"remed(?:y|ies)",
    r"relie(?:f|ve|ves|ved|ving)", r"pain(?:s|ful)?", r"anxi(?:ety|ous)", r"insomnia",
    r"sleep(?:s|ing|y|less)?", r"depress(?:ion|ed|ive|ing)?", r"ptsd", r"inflamm(?:ation|atory|ed)",
    r"symptoms?", r"seizures?", r"nause(?:a|ous)", r"cancer", r"diseases?",
    r"prescri(?:ption|ptions|be|bed)", r"doctors?", r"helps?\s+(?:with|you|to)",
)) + r")\b", re.IGNORECASE)

_INJECTION = re.compile("|".join((
    r"\b(?:ignore|disregard)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\b",
    r"\bsystem\s+prompt\b", r"\bassistant\s*:", r"\byou\s+are\b",
    r"https?:", r"\bwww\.", r"\{\{", r"`",
)), re.IGNORECASE)
_MARKUP = re.compile(r"[<>]")


def therapeutic_hits(text) -> list[str]:
    """The curative/therapeutic words in `text`, lower-cased, in order. [] for clean text or a non-string."""
    return [m.group(0).lower() for m in _THERAPEUTIC.finditer(text)] if isinstance(text, str) else []


def injection_hits(text, *, markup: bool = True) -> list[str]:
    """Prompt-injection / link / template markers in `text`. `markup=False` stops a bare `<` or `>`
    counting, for text that is never tag-stripped and may legitimately say "Soy (>1%)" (allergen
    lists); every other pattern still applies."""
    if not isinstance(text, str):
        return []
    hits = [m.group(0).lower() for m in _INJECTION.finditer(text)]
    if markup:
        hits += _MARKUP.findall(text)
    return hits
