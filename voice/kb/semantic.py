"""Embedding-backed semantic FAQ retrieval (22-SPEC-kb-seed.md §4) — an AUGMENTATION
with a deterministic fallback. It NEVER breaks the answer.

Ported from swedish-bot/kb/semantic.py (``_corpus_vectors``~L52 content-hash cache,
``_cos``~L44, ``rank_guides``~L103 → ``rank_faq``, the RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY
task split, the "embedding error → fail safe" contract) and retargeted to the voice KB.

Pipeline: build ONE heterogeneous corpus from every text KB model (each row → one
(chunk_id, chunk_text)); embed the corpus once with Gemini (768-dim Matryoshka,
RETRIEVAL_DOCUMENT) cached in the Django cache under a content-hash key bound to the
embedding model + dim (so it self-invalidates on any row edit — the live-edit property,
no redeploy); embed the query (RETRIEVAL_QUERY); rank by in-memory cosine; return top-k
(row, score). On disabled / no-auth / embedding error → deterministic keyword fallback
over the SAME chunk_text corpus (still grounded — returns real KB rows).

pgvector swap-seam (ADR-013, documented, NOT built): the cached-cosine corpus is the swap
seam. The KB is dozens of rows; in-memory cosine is fine. Past a few thousand rows, replace
``_corpus_vectors`` + the ``_cos`` loop with a pgvector ANN query (``CREATE EXTENSION vector``,
an ``embedding vector(768)`` column on each KB model — the seam is the nullable ``embedding``
JSON column already present — an HNSW index, ``ORDER BY embedding <=> query_vec LIMIT k``),
keeping the SAME ``rank_faq(query, store, top_k)`` signature so no caller changes. EXP item.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re

from django.conf import settings
from django.core.cache import cache as django_cache

from core import constants
from core.services import gemini

logger = logging.getLogger(__name__)

_CACHE_TTL = 3600
_CORPUS_PREFIX = "faq"


def enabled() -> bool:
    return bool(getattr(settings, "SEMANTIC_SEARCH_ENABLED", False))


def _cos(a, b) -> float:
    if len(a) != len(b):  # dimension mismatch (e.g. model/dim change) -> fail safe
        return 0.0
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


def _models():
    """Lazy import (avoid app-registry churn at module load). Returns the (prefix, Model)
    pairs that make up the heterogeneous retrieval corpus."""
    from kb import models as m

    return [
        ("faq", m.FAQEntry),
        ("pol", m.PolicyDocument),
        ("sf", m.StoreFact),
        ("edu", m.EducationDoc),
        ("blog", m.BlogDoc),
        ("tax", m.WeightTypeTaxonomy),
    ]


def _store_scoped(prefix: str) -> bool:
    """FAQEntry + StoreFact carry a per-store ``store`` column; everything else is global."""
    return prefix in ("faq", "sf")


# Topic constraint (voice/chat.py already classifies hours_location/specials/return_policy from
# the caller's own words; faq_lookup honours it here). Maps a topic name to the {ModelName:
# (field, {allowed values})} that row type uses to carry that subject. A model NOT listed for a
# topic has no clean topic-bearing field for it and is excluded outright rather than guessed at
# — e.g. EducationDoc/BlogDoc/WeightTypeTaxonomy carry no hours/specials/returns field, so a
# topic-constrained hours query never surfaces a taxonomy row.
#
# PolicyDocument is DATA-DRIVEN, not listed here: an owner-created PolicyCategory carries its
# own ``topic`` field (kb/models.py), so a policy row matches a topic when its category's
# ``topic`` equals the requested topic — see ``_topic_allows`` below. That is what lets the
# owner add a brand-new topic-scoped category (or a plain unconstrained one) with no code
# change here.
TOPIC_ROW_FIELDS: dict[str, dict[str, tuple[str, frozenset[str]]]] = {
    "hours_location": {
        "FAQEntry": ("topic", frozenset({"hours"})),
        "StoreFact": ("kind", frozenset({"hours", "address", "phone"})),
    },
    "specials": {
        "FAQEntry": ("topic", frozenset({"specials"})),
        "StoreFact": ("kind", frozenset({"special"})),
    },
    "return_policy": {
        "FAQEntry": ("topic", frozenset({"returns"})),
    },
}


def _model_has_topic_field(model_name: str, topic: str) -> bool:
    """True when this row type carries a way to answer whether it belongs to ``topic`` at
    all (used by ``_build_corpus`` to decide whether to even consider the model). PolicyDocument
    always qualifies — its per-row category.topic decides membership in ``_topic_allows``."""
    if model_name == "PolicyDocument":
        return True
    return model_name in TOPIC_ROW_FIELDS.get(topic, {})


# ``hours_location`` is really three different questions — where are you, when are you open, and
# what's your number — and the KB has a separate StoreFact for each. The topic scope alone put all
# three in one corpus, so "where are you located in yakima" was answered with the Yakima HOURS row
# (it shares "yakima" just as strongly, and outranks on weight). When the caller's words name
# exactly ONE of the three, narrow the corpus to it: the address row wins a where-question, the
# hours row wins a what-time question, and an ask that names two ("are you open, and where are
# you") stays unnarrowed, exactly as before. Derived from the query inside ``rank_faq``, so BOTH
# the keyword and the embedding path inherit it.
_HOURS_LOCATION_ASKS = (
    ("address", re.compile(r"\b(where|address|located|directions?|street|parking)\b", re.I)),
    ("hours", re.compile(r"\b(hours?|open|opening|close|closing|closed|what\s+time)\b", re.I)),
    ("phone", re.compile(r"\b(phone|call|number)\b", re.I)),
)


def _hours_location_kind(query: str) -> str:
    """The ONE StoreFact kind this hours_location query is about, or "" when it names none or
    more than one (ambiguous → don't narrow)."""
    hits = [kind for kind, pattern in _HOURS_LOCATION_ASKS if pattern.search(query or "")]
    return hits[0] if len(hits) == 1 else ""


def _topic_allows(topic: str, model_name: str, row, kind: str = "") -> bool:
    """True when ``row`` carries the field value that puts it in ``topic``. A model with no
    entry for this topic (no clean topic-bearing field) never passes. ``kind`` narrows an
    hours_location query to the one StoreFact kind it actually asked about (see
    ``_hours_location_kind``); an FAQEntry can only answer the hours half of that topic, so it
    is excluded when the caller asked for the address or the phone number."""
    if model_name == "PolicyDocument":
        # Data-driven: the owner's PolicyCategory.topic decides, not a hardcoded kind value.
        return (getattr(row.category, "topic", "") or "") == topic
    rule = TOPIC_ROW_FIELDS.get(topic, {}).get(model_name)
    if rule is None:
        return False
    field, allowed = rule
    if kind and topic == "hours_location":
        if model_name == "StoreFact":
            allowed = frozenset({kind})
        elif kind != "hours":  # an FAQEntry(topic=hours) cannot answer "where" or "what number"
            allowed = frozenset()
    return (getattr(row, field, "") or "") in allowed


def _build_corpus(store: str | None, topic: str = "", kind: str = ""):
    """Build the store-scoped (and, when ``topic`` is given, topic-scoped) corpus: a list of
    (chunk_id, chunk_text) and a parallel {chunk_id: row} map. Store filtering happens HERE so a
    Yakima caller never gets a Pullman-hours chunk (22-SPEC §4.1); topic filtering happens HERE
    too so BOTH the keyword and embedding ranking paths inherit it for free."""
    items: list[tuple[str, str]] = []
    row_by_id: dict[str, object] = {}
    for prefix, Model in _models():
        if topic and not _model_has_topic_field(Model.__name__, topic):
            continue  # no clean topic field for this row type — exclude, don't guess
        qs = Model.objects.filter(is_active=True)
        if Model.__name__ == "PolicyDocument":
            qs = qs.select_related("category")
        for row in qs:
            if _store_scoped(prefix) and store:
                row_store = (getattr(row, "store", "") or "").strip()
                if row_store and row_store != store:
                    continue  # per-store row for a different store
            if topic and not _topic_allows(topic, Model.__name__, row, kind):
                continue
            chunk_id = f"{prefix}{row.pk}"
            items.append((chunk_id, row.chunk_text()))
            row_by_id[chunk_id] = row
    return items, row_by_id


def _corpus_cache_key(items: list[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for _id, text in items:
        h.update(f"{_id}\x1f{text}".encode())
    return f"{_CORPUS_PREFIX}:{gemini.active_embedding_model()}:{constants.EMBED_DIM}:{h.hexdigest()[:16]}"


def _corpus_vectors(items: list[tuple[str, str]]) -> dict[str, list[float]]:
    """items: list of (id, text) -> {id: vector}, cached + content-hashed so it
    self-invalidates whenever the corpus text (or the embedding model/dim) changes —
    that is the no-redeploy live-edit property (22-SPEC §4.2, P0 acceptance C2)."""
    if not items:
        return {}
    key = _corpus_cache_key(items)
    cached = django_cache.get(key)
    if cached is not None:
        return cached
    vecs = gemini.embed([t for _, t in items], task_type="RETRIEVAL_DOCUMENT")
    out = {items[i][0]: vecs[i] for i in range(len(items))}
    # gemini.embed may resolve gemini-embedding-2 to a fallback model. Bind the
    # stored vectors to the resolved embedding space, not the pre-call preference.
    django_cache.set(_corpus_cache_key(items), out, _CACHE_TTL)
    return out


_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Generic linguistic stopwords only (no domain terms) — dropped from the keyword fallback so a
# distinctive token ("microdose", "eighth") outscores an incidental "what"/"do" overlap.
_STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from how i in is it me my no not of on or "
    "our s the to up us we what when where which who why with you your".split()
)


def _tokens(text: str, *, drop_stop: bool = False) -> list[str]:
    toks = _TOKEN_RE.findall((text or "").lower())
    return [t for t in toks if t not in _STOPWORDS] if drop_stop else toks


def _keyword_fallback(query: str, items: list[tuple[str, str]], row_by_id: dict, top_k: int):
    """Deterministic keyword/substring score over the same chunk_text corpus (22-SPEC §4.4).

    Used when retrieval is disabled OR Gemini auth/API is unavailable. Score = overlap of
    query tokens with chunk tokens + a small boost for paraphrase/synonym hits + the row's
    weight/100 as a tiebreak. STILL grounded — returns real KB rows, just lower paraphrase
    recall. Mirrors swedish-bot's "embedding error → keep a deterministic answer" pattern.

    The RAW score alone is not a relevance signal across rows of different subject matter — a
    single incidental shared word can still out-score a genuinely on-topic row (the "today's
    special" paraphrase problem). ``relevance_coverage`` below is the independent floor for that;
    it re-derives coverage from the query text directly rather than trusting this score."""
    q_tokens = set(_tokens(query, drop_stop=True))
    if not q_tokens:
        return []
    scored: list[tuple[float, str]] = []
    for chunk_id, text in items:
        c_tokens = set(_tokens(text, drop_stop=True))
        overlap = len(q_tokens & c_tokens)
        if overlap == 0:
            continue
        row = row_by_id[chunk_id]
        boost = 0.0
        # Paraphrases (FAQEntry) / synonyms (taxonomy) are a STRONG recall signal: when the user
        # names the exact term ("an eighth"), each matching alt phrasing decisively lifts the row
        # over an incidental keyword collision in another chunk's prose.
        for extra in (getattr(row, "paraphrases", None) or []) + (
            getattr(row, "synonyms", None) or []
        ):
            if q_tokens & set(_tokens(extra, drop_stop=True)):
                boost += 1.0
        # A taxonomy row whose TERM is the very word the caller named ("what does INDICA mean")
        # is what they asked about; every strain-type row repeats "indica/sativa/hybrid" in its
        # shared caveat, so raw overlap ties all three and the winner was whichever sorted first
        # — "what does indica mean" was answered by the hybrid row.
        term = str(getattr(row, "term", "") or "").strip().lower()
        if term and term in q_tokens:
            boost += 2.0
        tiebreak = (getattr(row, "weight", 100) or 100) / 100.0
        scored.append((overlap + boost + tiebreak * 0.001, chunk_id))
    scored.sort(reverse=True)
    return [(row_by_id[cid], score) for score, cid in scored[:top_k]]


# A first naive floor ("reject any match with fewer than 2 overlapping content tokens") broke
# legitimate SHORT queries — "what are your hours" has exactly one content word ("hours") once
# stopwords drop, so a flat >=2 threshold rejected its own only, correct, signal. It also let
# tokenizer debris through: "won't"/"I'll" split on the apostrophe into stray 1-2-letter
# fragments ("t", "ll") that happen to recur across unrelated rows and inflate overlap counts
# that mean nothing. ``_LEXICAL_MIN_LEN`` drops those fragments from the floor's own token set
# (ranking above is untouched — it still scores every token, including short ones).
_LEXICAL_MIN_LEN = 3
# ...but the flat cut also threw away real short words, and one of them is the single most
# load-bearing noun in the compliance FAQ: "ID". "what kind of ID do you accept" reduced to
# {kind, accept} — the question's actual subject was invisible to the floor, so the FAQEntry that
# answers it (passport / driver's licence / unexpired) could never clear the bar and the caller
# was told "I can't confirm that right now". An ALLOWLIST rather than a lower cut: lifting the
# length rule wholesale would also readmit the apostrophe debris it exists for ("t", "ll", "ve")
# and greeting noise ("hi"), both of which only dilute the coverage ratio below.
_SHORT_CONTENT_WORDS = frozenset({"id", "oz", "mg", "wa", "og"})


def _content_words(text: str) -> set[str]:
    return {
        t
        for t in _tokens(text, drop_stop=True)
        if len(t) >= _LEXICAL_MIN_LEN or t in _SHORT_CONTENT_WORDS
    }


# COVERAGE FLOOR (2026-09-01). "shares at least two content words" is blind to how much of the
# question those two words are: "is your weed cheaper than the shop down the street" shares
# exactly "weed" and "shop" with the Mount Vernon address blurb — two words out of six, none of
# them what the caller asked about — and was answered with the store's address as though it were
# a price comparison. Requiring the shared words to be a real SHARE of the question rejects that
# (2/6 = 0.33) while leaving every genuine hit alone: a defective-cart complaint covers 0.40 of
# its own words, an hours/address/phone question covers 0.75-1.00. The short-query escape below
# (``overlap == q``) is untouched, so "what are your hours" still grounds on its one word.
#
# Coverage alone would be too blunt, though: "what's the legal limit I can buy in one day?" also
# covers only 0.33 of itself against the WA purchase-limits row, and that is a real, correct hit.
# What separates the two is the row's own ALTERNATIVE PHRASINGS — the limits row is written with
# paraphrases like "how much can I buy"; the Mount Vernon address blurb has nothing resembling a
# price comparison. So a row whose paraphrases/synonyms answer this ask clears the floor on
# thinner coverage. This is the same signal the keyword ranker already boosts on, re-derived here
# so it gates the embedding path identically.
_MIN_COVERAGE = 0.4


def _paraphrase_hit(query_words: set[str], row) -> bool:
    """True when one of the row's own alternative phrasings (FAQEntry.paraphrases /
    WeightTypeTaxonomy.synonyms) shares a content word with the question."""
    for extra in (getattr(row, "paraphrases", None) or []) + (getattr(row, "synonyms", None) or []):
        if query_words & _content_words(extra):
            return True
    return False


def relevant_enough(query: str, row) -> bool:
    """RELEVANCE FLOOR for unconstrained retrieval (22-SPEC follow-up) — deliberately independent
    of the ranking score above; it re-derives relevance from the raw query text against the
    winning row's chunk text, so it gates BOTH the keyword and the embedding (cosine) ranking
    paths alike. Passes when the row covers the WHOLE of a short query ("what are your hours" ↔
    "hours"), or shares at least two of the query's distinctive content words AND those shared
    words are at least ``_MIN_COVERAGE`` of the question ("a cartridge ... won't fire" ↔ the WAC
    row's "a vape cart that won't fire"). Rejects a row that shares just ONE incidental word out
    of several ("best" in "just give me your best guess", "bring" in "alright, I'll bring the box
    in") and — since 2026-09-01 — a row that shares two words that are still only a fragment of
    what was asked ("weed"/"shop" out of "is your weed cheaper than the shop down the street").
    False when the query has no content words at all (never confident on nothing)."""
    q = _content_words(query)
    if not q:
        return False
    chunk_text = row.chunk_text() if hasattr(row, "chunk_text") else str(row)
    overlap = q & _content_words(chunk_text)
    if overlap == q:  # short query, every word of it is in the row
        return True
    # A defined-term row IS the answer when the caller names its term ("what does indica mean",
    # "how big is an eighth") — the definition never repeats the question's other words, so the
    # word-overlap rules below would reject it.
    term = str(getattr(row, "term", "") or "").strip().lower()
    if term and term in q:
        return True
    if len(overlap) < 2:
        return False
    return len(overlap) / len(q) >= _MIN_COVERAGE or _paraphrase_hit(q, row)


def rank_faq(
    query: str, store: str | None = None, top_k: int = 3, topic: str = ""
) -> list[tuple[object, float]]:
    """Top-k KB rows for the query, store-scoped and — when ``topic`` is one of
    ``hours_location``/``specials``/``return_policy`` — topic-scoped (empty/absent = today's
    unconstrained behaviour). Empty on no corpus; degrade-safe on embedding error (keyword
    fallback). Each element = (model_instance, cosine|keyword_score).

    Adapts swedish-bot rank_guides (corpus build → embed → cosine → top-k)."""
    if not (query or "").strip():
        return []
    topic = (topic or "").strip()
    kind = _hours_location_kind(query) if topic == "hours_location" else ""
    items, row_by_id = _build_corpus(store, topic, kind)
    if not items:
        return []
    if not enabled():
        return _keyword_fallback(query, items, row_by_id, top_k)
    try:
        qv = gemini.embed(query, task_type="RETRIEVAL_QUERY")
        vecs = _corpus_vectors(items)
    except Exception:  # noqa: BLE001 — never break the answer on an embedding error
        logger.warning("rank_faq embedding failed; keyword fallback", exc_info=True)
        return _keyword_fallback(query, items, row_by_id, top_k)
    scored = sorted(((_cos(qv, v), cid) for cid, v in vecs.items()), reverse=True)
    return [(row_by_id[cid], s) for s, cid in scored[:top_k]]


def reindex() -> int:
    """Force-rebuild the cosine cache for the current (global, unscoped) corpus; return the
    chunk count == the number of active KB rows. Called by the dashboard reindex button (P4)
    and ``seed_kb --reindex`` / ``reindex_kb`` (P0).

    The Vapi mirror (``vapi_files.mirror_all()``) is triggered at the command/button layer,
    NOT here — keep the Vapi dependency out of the retrieval module (22-SPEC §4.3)."""
    items, _ = _build_corpus(store=None)
    if not items:
        return 0
    if enabled():
        # Drop any stale entry then re-embed → repopulate the content-hash-keyed cache.
        try:
            django_cache.delete(_corpus_cache_key(items))
            _corpus_vectors(items)
        except Exception:  # noqa: BLE001 — reindex must not crash on a transient embed error
            logger.warning("reindex embedding failed; corpus count still returned", exc_info=True)
    return len(items)
