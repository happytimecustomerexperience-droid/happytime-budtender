"""``faq_lookup`` — the grounded FAQ tool (10-P0-CHASSIS-FAQ.md §3.3 / §4.3).

Reads ``kb/`` live (canonical — a dashboard edit is answered on the very next call, no
redeploy) via ``kb.semantic.rank_faq``, which embeds the query + corpus (Gemini 768-dim) and
ranks by cosine, degrading to a deterministic keyword match when Gemini is unavailable — so the
answer is ALWAYS grounded in real KB rows, never hallucinated (Numbers-Guard, ADR-012).

Contract: ``faq_lookup(args, ctx) -> dict`` where ``args = {query, store?, topic?}`` — ``topic``
is one of ``hours_location``/``specials``/``return_policy``/``""`` (chat.py already classifies it
from the caller's words); when supplied it constrains retrieval to that subject so a caller
asking "what time do you close" never gets the specials row back. Returns
``{answer, grounded: true, sources: [{kind, id, title}], store}`` on a confident match; on no
match → ``{answer: null, grounded: false, fallback: "..."}`` so the assistant offers a human and
never invents a number/hour/price. The handler composes NO figure — every spoken value is the
KB row text verbatim-ish.
"""

from __future__ import annotations

import logging
import re

from voice.tools import register

logger = logging.getLogger(__name__)

# Cosine floor below which we treat the corpus as "no confident match" and hand to a human.
# Keyword-fallback scores (overlap counts) are >= 1 on any real hit, so this only gates the
# embedding path; the keyword path's own "no overlap → []" already filters non-matches.
_MIN_COSINE = 0.30

# Topics chat.py already classifies from the caller's own words (voice/chat.py::_faq_topic) and
# hands to faq_lookup so retrieval can be constrained to the subject actually asked about,
# instead of always returning its single global-best row. "" = unconstrained (today's behaviour).
_VALID_TOPICS = frozenset({"hours_location", "specials", "return_policy", ""})

# RELEVANCE FLOOR (unconstrained queries only — a topic already scopes the corpus to on-topic
# rows, so this floor would only cost recall there; see kb.semantic.relevant_enough). A single
# incidental shared word ("best" in "just give me your best guess", "bring" in "alright, I'll
# bring the box in") must not ground a confident answer just because it's the top-scoring row of
# an otherwise-irrelevant corpus.
_PROMPT_INJECTION = re.compile(
    r"\b(ignore|disregard|override|reveal|print|show|leak)\b.{0,80}\b"
    r"(instruction|prompt|system|developer|secret|tool|policy|rule)s?\b",
    re.IGNORECASE | re.DOTALL,
)

_FALLBACK = "I'm not certain on that one — let me get a team member who can help."

# Map a KB model class name to the stable ``kind`` string surfaced as a source.
_KIND_BY_MODEL = {
    "FAQEntry": "faq",
    "PolicyDocument": "policy",
    "StoreFact": "store_fact",
    "EducationDoc": "education",
    "BlogDoc": "blog",
    "WeightTypeTaxonomy": "taxonomy",
}


def _source_kind(row) -> str:
    return _KIND_BY_MODEL.get(type(row).__name__, type(row).__name__.lower())


def _row_title(row) -> str:
    """A short, speakable source title (label/question/title), never the full body."""
    for attr in ("label", "question", "title", "term"):
        val = getattr(row, attr, None)
        if val:
            return str(val)[:120]
    return str(row)[:120]


def _row_url(row) -> str:
    return str(getattr(row, "source_url", "") or "")[:500]


def _row_answer(row) -> str:
    """The grounded answer text from a KB row — the spoken value lives in the row, not the LLM."""
    # FAQEntry has a curated ``answer``; everything else speaks its ``chunk_text``.
    answer = getattr(row, "answer", None)
    if answer:
        return str(answer).strip()
    return row.chunk_text().strip()


def _looks_poisoned(text: str) -> bool:
    """True when KB content looks like instructions to hijack the assistant."""
    return bool(_PROMPT_INJECTION.search(text or ""))


def _grounded(query: str, store: str | None, topic: str = "") -> dict | None:
    """Run KB retrieval; return the grounded answer dict, or ``None`` on no confident match."""
    from kb import semantic

    ranked = semantic.rank_faq(query, store=store, top_k=3, topic=topic)
    if not ranked:
        return None
    top_row, top_score = ranked[0]
    # The embedding path returns cosine in [-1, 1]; gate weak cosines so a vague-but-nonzero
    # similarity hands to a human instead of speaking the wrong row. The keyword fallback
    # (semantic disabled) returns an overlap COUNT, not a cosine, and already filters non-matches
    # by returning [] on zero overlap — so the cosine floor applies ONLY to the embedding path.
    if semantic.enabled() and top_score < _MIN_COSINE:
        return None
    # Relevance floor — unconstrained queries only (a topic already scopes the corpus). Applies to
    # BOTH the keyword and embedding paths alike, since it re-derives relevance from the raw query
    # text against the winning row's chunk text rather than trusting either path's own score.
    if not topic and not semantic.relevant_enough(query, top_row):
        return None
    answer = _row_answer(top_row)
    if _looks_poisoned(answer):
        logger.warning("refusing suspicious KB row %s", getattr(top_row, "pk", ""))
        return None
    sources = [
        {
            "kind": _source_kind(row),
            "id": row.pk,
            "title": _row_title(row),
            "source_url": _row_url(row),
        }
        for row, _ in ranked
    ]
    return {
        "answer": answer,
        "grounded": True,
        "sources": sources,
        "store": store or "",
    }


@register("faq_lookup")
def faq_lookup(args: dict, ctx: dict) -> dict:
    """Answer hours/specials/returns/payment/pickup/limits/weights-types from the KB."""
    query = (args.get("query") or "").strip()
    # Prefer an explicit tool arg; fall back to the call's resolved store from ctx.
    store = (args.get("store") or ctx.get("store") or "").strip() or None
    topic = (args.get("topic") or "").strip()
    if topic not in _VALID_TOPICS:
        topic = ""  # an unrecognized topic is treated as unconstrained, never a filter-to-nothing
    if not query:
        return {"answer": None, "grounded": False, "fallback": _FALLBACK, "store": store or ""}

    result = _grounded(query, store, topic)
    if result is not None:
        return result
    # No confident KB match → offer a human; NEVER invent (10-P0 §4.3 Numbers-Guard).
    return {"answer": None, "grounded": False, "fallback": _FALLBACK, "store": store or ""}
