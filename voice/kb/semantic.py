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


def _build_corpus(store: str | None):
    """Build the store-scoped corpus: a list of (chunk_id, chunk_text) and a parallel
    {chunk_id: row} map. Store filtering happens HERE so a Yakima caller never gets a
    Pullman-hours chunk (22-SPEC §4.1)."""
    items: list[tuple[str, str]] = []
    row_by_id: dict[str, object] = {}
    for prefix, Model in _models():
        for row in Model.objects.filter(is_active=True):
            if _store_scoped(prefix) and store:
                row_store = (getattr(row, "store", "") or "").strip()
                if row_store and row_store != store:
                    continue  # per-store row for a different store
            chunk_id = f"{prefix}{row.pk}"
            items.append((chunk_id, row.chunk_text()))
            row_by_id[chunk_id] = row
    return items, row_by_id


def _corpus_vectors(items: list[tuple[str, str]]) -> dict[str, list[float]]:
    """items: list of (id, text) -> {id: vector}, cached + content-hashed so it
    self-invalidates whenever the corpus text (or the embedding model/dim) changes —
    that is the no-redeploy live-edit property (22-SPEC §4.2, P0 acceptance C2)."""
    if not items:
        return {}
    h = hashlib.sha256()
    for _id, text in items:
        h.update(f"{_id}\x1f{text}".encode())
    # Bind the cache entry to the embedding space so query/corpus vectors can never be
    # compared across a different model or dimensionality.
    key = f"{_CORPUS_PREFIX}:{gemini.active_embedding_model()}:{constants.EMBED_DIM}:{h.hexdigest()[:16]}"
    cached = django_cache.get(key)
    if cached is not None:
        return cached
    vecs = gemini.embed([t for _, t in items], task_type="RETRIEVAL_DOCUMENT")
    out = {items[i][0]: vecs[i] for i in range(len(items))}
    django_cache.set(key, out, _CACHE_TTL)
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
    recall. Mirrors swedish-bot's "embedding error → keep a deterministic answer" pattern."""
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
        tiebreak = (getattr(row, "weight", 100) or 100) / 100.0
        scored.append((overlap + boost + tiebreak * 0.001, chunk_id))
    scored.sort(reverse=True)
    return [(row_by_id[cid], score) for score, cid in scored[:top_k]]


def rank_faq(query: str, store: str | None = None, top_k: int = 3) -> list[tuple[object, float]]:
    """Top-k KB rows for the query, store-scoped. Empty on no corpus; degrade-safe on
    embedding error (keyword fallback). Each element = (model_instance, cosine|keyword_score).

    Adapts swedish-bot rank_guides (corpus build → embed → cosine → top-k)."""
    if not (query or "").strip():
        return []
    items, row_by_id = _build_corpus(store)
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
            h = hashlib.sha256()
            for _id, text in items:
                h.update(f"{_id}\x1f{text}".encode())
            key = (
                f"{_CORPUS_PREFIX}:{gemini.active_embedding_model()}:"
                f"{constants.EMBED_DIM}:{h.hexdigest()[:16]}"
            )
            django_cache.delete(key)
            _corpus_vectors(items)
        except Exception:  # noqa: BLE001 — reindex must not crash on a transient embed error
            logger.warning("reindex embedding failed; corpus count still returned", exc_info=True)
    return len(items)
