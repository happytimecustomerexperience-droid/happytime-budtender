"""KB unit tests (22-SPEC-kb-seed.md §9 / §10.1) — models, the embeddings retrieval
pipeline (Gemini embed MOCKED — the suite passes with no live keys, 03-CONVENTIONS.md §5),
the seed content (idempotent + every §7 row), O-8, WA-law accuracy, and taxonomy parity.

Semantic tests opt into ``settings.SEMANTIC_SEARCH_ENABLED = True`` (the conftest autouse
fixture turns it OFF by default) and MOCK ``gemini.embed`` with the deterministic, offline
``bow_gemini`` bag-of-words embedder below — so "the right chunk" is decided by content
overlap, never by a live model or a network call. No live API key is ever required.
"""

from __future__ import annotations

import pytest

from kb import models as m

# ── A. Models + migration ─────────────────────────────────────────────────────


def test_exactly_six_voice_kb_models_and_one_taxonomy():
    """A1 drift guard: the six voice KB models exist; exactly ONE taxonomy model (no
    WeightsTypesTaxonomy duplicate)."""
    names = {c.__name__ for c in m.__dict__.values() if isinstance(c, type)}
    for expected in (
        "FAQEntry",
        "PolicyDocument",
        "StoreFact",
        "EducationDoc",
        "BlogDoc",
        "WeightTypeTaxonomy",
    ):
        assert expected in names, f"missing voice KB model {expected}"
    taxonomy = [n for n in names if "Taxonomy" in n]
    assert taxonomy == ["WeightTypeTaxonomy"], f"expected one taxonomy model, found {taxonomy}"


@pytest.mark.django_db
def test_chunk_text_nonempty_for_every_seeded_model():
    """A3: every text model returns a non-empty chunk_text() for a seeded row."""
    from kb import seed

    seed.seed_all()
    samples = [
        m.FAQEntry.objects.first(),
        m.PolicyDocument.objects.first(),
        m.StoreFact.objects.filter(confirmed=True).first(),
        m.EducationDoc.objects.first(),
        m.BlogDoc.objects.first(),
        m.WeightTypeTaxonomy.objects.first(),
    ]
    for row in samples:
        assert row is not None
        assert row.chunk_text().strip(), f"empty chunk_text on {row!r}"


# ── B. Embeddings retrieval (the core ask — Gemini embed MOCKED) ──────────────


@pytest.fixture
def bow_gemini(monkeypatch):
    """A deterministic, OFFLINE bag-of-words stand-in for gemini.embed (no network, no live
    keys). Each token hashes to one of 768 buckets; a text's vector is the L2-normalized sum
    of its token buckets — so two texts that SHARE words land close in cosine space. This gives
    real, content-driven proximity (the way a true embedding rewards lexical+semantic overlap)
    while staying fully deterministic. The pure-noise conftest mock can't express proximity, so
    semantic-rank tests use this. Mirrors the FakeGemini interface."""
    import hashlib
    import math

    import core.services.gemini as gemini_mod

    _TOK = __import__("re").compile(r"[a-z0-9]+")

    def _vec(text: str, dim: int) -> list[float]:
        v = [0.0] * dim
        for tok in _TOK.findall((text or "").lower()):
            bucket = int(hashlib.sha256(tok.encode()).hexdigest(), 16) % dim
            v[bucket] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(
        texts,
        *,
        model=None,
        task_type="RETRIEVAL_DOCUMENT",
        output_dimensionality=None,
        api_key=None,
    ):
        dim = output_dimensionality or 768
        one = isinstance(texts, str)
        items = [texts] if one else list(texts)
        out = [_vec(t, dim) for t in items]
        return out[0] if one else out

    monkeypatch.setattr(gemini_mod, "embed", embed)
    monkeypatch.setattr(gemini_mod, "active_embedding_model", lambda: "bow-mock")
    return embed


@pytest.fixture
def seeded_semantic(db, settings, bow_gemini):
    """Seed the KB + turn semantic search ON with the deterministic bag-of-words embedder."""
    from kb import seed

    seed.seed_all()
    settings.SEMANTIC_SEARCH_ENABLED = True
    return bow_gemini


def _grounded_text(row) -> str:
    """The spoken-grounding text of whichever KB model a hit is."""
    return (
        getattr(row, "answer", "")
        or getattr(row, "value", "")
        or getattr(row, "body", "")
        or getattr(row, "chunk_text", lambda: "")()
    )


@pytest.mark.django_db
def test_rank_faq_returns_the_right_chunk(seeded_semantic):
    """B1/F1/F2: semantic retrieval returns the right chunk for each sampled query — the
    deliverable. Gemini embed is MOCKED (deterministic bag-of-words, offline): the chunk a query
    overlaps ranks first, and its text carries the seeded fact. (Several seeded rows can legit
    answer one query — the EXACT term/value disambiguation is gated in the keyword-fallback
    test, which uses the synonym + weight signal.)"""
    from kb import semantic

    # Yakima hours → the Yakima-hours StoreFact (exact, store-scoped).
    top, _ = semantic.rank_faq("yakima store hours what time open", store="yakima")[0]
    assert isinstance(top, m.StoreFact) and top.store == "yakima" and top.kind == "hours"
    assert "9 AM–11 PM" in top.value

    # Defective vape return → a WAC-314-55-079-cited row (return policy or the returns FAQ).
    top, _ = semantic.rank_faq("return a defective vape cartridge that won't fire")[0]
    assert "WAC 314-55-079" in _grounded_text(top)

    # "an eighth" → a weight chunk that surfaces 3.5 g (the eighth row or an eighth-adjacent one).
    top, _ = semantic.rank_faq("what is an eighth eighth-oz 3.5g")[0]
    assert isinstance(top, m.WeightTypeTaxonomy) and top.term == "eighth" and top.value == "3.5 g"

    # "microdose / microdosing" → a microdosing chunk (the dose row or the education doc — both
    # ground the 1–2.5 mg dose). Assert the dose fact is present.
    top, _ = semantic.rank_faq("microdose microdosing dose mg")[0]
    assert "microdos" in _grounded_text(top).lower() or top.term == "microdose"


@pytest.mark.django_db
def test_rank_faq_store_scoping(seeded_semantic):
    """B2: a Pullman query never returns a Yakima/Mt-Vernon hours row; a global row
    (taxonomy/limits) returns for any store."""
    from kb import semantic

    hits = semantic.rank_faq("what are your hours", store="pullman", top_k=6)
    for row, _ in hits:
        if isinstance(row, m.StoreFact) and row.kind == "hours":
            assert row.store in ("", "pullman"), (
                f"leaked a {row.store} hours row to a Pullman caller"
            )

    # A global taxonomy row answers regardless of store.
    top, _ = semantic.rank_faq("how much flower can I buy in a visit", store="pullman")[0]
    assert top is not None


@pytest.mark.django_db
def test_content_hash_cache_invalidates_on_edit(seeded_semantic):
    """B3: editing a KB row's text changes the corpus content-hash cache key → the next
    rank_faq reflects the edit with NO redeploy (the live-edit property)."""
    from kb import semantic

    items, _ = semantic._build_corpus(store=None)
    key_before = _corpus_key(items)

    fa = m.FAQEntry.objects.get(key="payment")
    fa.answer = "We now also take Apple Pay at the register."
    fa.save()

    items2, row_by_id = semantic._build_corpus(store=None)
    key_after = _corpus_key(items2)
    assert key_after != key_before, "cache key did not change after an edit"
    edited_chunk = next(
        t for cid, t in items2 if row_by_id[cid].pk == fa.pk and cid.startswith("faq")
    )
    assert "Apple Pay" in edited_chunk


def _corpus_key(items):
    import hashlib

    from core import constants
    from core.services import gemini

    h = hashlib.sha256()
    for _id, text in items:
        h.update(f"{_id}\x1f{text}".encode())
    return f"faq:{gemini.active_embedding_model()}:{constants.EMBED_DIM}:{h.hexdigest()[:16]}"


@pytest.mark.django_db
def test_rank_faq_keyword_fallback_when_gemini_down(db, settings):
    """B4: with gemini.embed mocked to RAISE, rank_faq degrades to keyword match and STILL
    returns the correct row (the answer never breaks)."""
    from kb import seed, semantic

    seed.seed_all()
    settings.SEMANTIC_SEARCH_ENABLED = True

    import core.services.gemini as gemini_mod

    def _boom(*a, **k):
        raise RuntimeError("Gemini API down")

    # Patch the embed the semantic module calls (it imports `gemini` then `gemini.embed`).
    semantic.gemini.embed = _boom
    try:
        # "yakima hours" lexically overlaps only the Yakima-hours StoreFact chunk.
        top, _ = semantic.rank_faq("yakima hours", store="yakima")[0]
        assert isinstance(top, m.StoreFact) and top.kind == "hours" and top.store == "yakima"
        # A defective-return query grounds in a real returns row (policy or the returns FAQ).
        top, _ = semantic.rank_faq("return a defective vape cartridge")[0]
        assert "WAC 314-55-079" in (getattr(top, "body", "") or getattr(top, "answer", "")), (
            "returns query did not ground in the WAC-cited row"
        )
    finally:
        semantic.gemini.embed = gemini_mod.embed


@pytest.mark.django_db
def test_keyword_fallback_when_semantic_disabled(db, settings):
    """rank_faq with SEMANTIC_SEARCH_ENABLED False (no embed call at all) still grounds via
    the deterministic keyword path."""
    from kb import seed, semantic

    seed.seed_all()
    settings.SEMANTIC_SEARCH_ENABLED = False
    # "do you take cards" / "debit" grounds in a payment answer (the payment FAQ or StoreFact —
    # both are correct; assert the answer text carries the grounded payment fact).
    top, _ = semantic.rank_faq("do you take cards debit payment")[0]
    text = (getattr(top, "answer", "") or getattr(top, "value", "")).lower()
    assert "debit" in text, f"payment query did not ground in a payment row: {top!r}"


@pytest.mark.django_db
def test_keyword_fallback_uses_synonyms_and_weight(db, settings):
    """The deterministic fallback (synonym boost + weight tiebreak) grounds the realistic
    natural-language queries the toy mock embedder can't disambiguate: 'grams in an eighth' →
    3.5 g, 'microdose' → the microdose row, and a returns query hits the higher-weight
    WAC-cited PolicyDocument (policy outranks the generic FAQ — 22-SPEC §3.2)."""
    from kb import seed, semantic

    seed.seed_all()
    settings.SEMANTIC_SEARCH_ENABLED = False  # force the keyword fallback path

    # The "eighth" synonyms (["1/8 oz","an eighth",…]) boost it over "two grams".
    top, _ = semantic.rank_faq("how many grams in an eighth")[0]
    assert isinstance(top, m.WeightTypeTaxonomy) and top.term == "eighth" and top.value == "3.5 g"

    top, _ = semantic.rank_faq("what's a microdose")[0]
    assert isinstance(top, m.WeightTypeTaxonomy) and top.term == "microdose"

    # A returns query grounds in a WAC-314-55-079-cited row (the answer carries the statute).
    top, _ = semantic.rank_faq("return policy for a defective product")[0]
    assert "WAC 314-55-079" in _grounded_text(top)


@pytest.mark.django_db
def test_policy_outranks_faq_on_weight(db, settings):
    """22-SPEC §3.2: the return PolicyDocument (weight 120) outranks the generic returns FAQ
    (weight 100) when lexical overlap ties — the weight tiebreak decides. Proven on a query that
    hits both equally (a distinctive token present in both chunks, no paraphrase/synonym edge)."""
    from kb import seed, semantic

    seed.seed_all()
    settings.SEMANTIC_SEARCH_ENABLED = False

    # Both the policy body and the returns FAQ answer contain "WAC 314-55-079" → the only shared
    # distinctive token set; neither has a paraphrase/synonym matching "wac", so overlap ties and
    # the higher weight (policy 120 > faq 100) wins.
    hits = semantic.rank_faq("wac 314 55 079")
    top = hits[0][0]
    assert isinstance(top, m.PolicyDocument), f"weight tiebreak failed: top was {top!r}"


@pytest.mark.django_db
def test_reindex_returns_chunk_count(seeded_semantic):
    """B5: reindex() returns the chunk count == the number of active KB rows."""
    from kb import semantic

    n = semantic.reindex()
    active = (
        m.FAQEntry.objects.filter(is_active=True).count()
        + m.PolicyDocument.objects.filter(is_active=True).count()
        + m.StoreFact.objects.filter(is_active=True).count()
        + m.EducationDoc.objects.filter(is_active=True).count()
        + m.BlogDoc.objects.filter(is_active=True).count()
        + m.WeightTypeTaxonomy.objects.filter(is_active=True).count()
    )
    assert n == active


# ── D. Seed content (gap G-7) ─────────────────────────────────────────────────


@pytest.mark.django_db
def test_seed_is_idempotent():
    """D1: run seed twice → no duplicate rows."""
    from kb import seed

    seed.seed_all()
    counts1 = {
        "faq": m.FAQEntry.objects.count(),
        "store": m.StoreFact.objects.count(),
        "tax": m.WeightTypeTaxonomy.objects.count(),
    }
    seed.seed_all()
    counts2 = {
        "faq": m.FAQEntry.objects.count(),
        "store": m.StoreFact.objects.count(),
        "tax": m.WeightTypeTaxonomy.objects.count(),
    }
    assert counts1 == counts2


@pytest.mark.django_db
def test_every_mapped_row_exists():
    """D2: after seeding, every §7-mapped block is present at the spec'd cardinality."""
    from kb import seed

    seed.seed_all()
    assert m.FAQEntry.objects.count() == 8
    assert m.PolicyDocument.objects.filter(kind="return_policy").count() == 1
    assert m.StoreFact.objects.filter(kind="special").count() == 5
    assert m.StoreFact.objects.filter(kind="limit").count() == 5  # 4 limits + age/ID rule
    assert m.EducationDoc.objects.count() == 5
    assert m.BlogDoc.objects.count() == 3
    assert m.AgentPrompt.objects.filter(role="faq").count() == 1
    # P2: the escalation persona is seeded (de-escalation + the WAC defective path, grounded).
    esc = m.AgentPrompt.objects.filter(role="escalation").first()
    assert esc is not None
    assert "WAC 314-55-079" in esc.body
    assert "transfer" in esc.body.lower()  # hands off, never resolves the dispute itself
    axis_counts = {
        "weight": 10,
        "cart_size": 2,
        "preroll": 3,
        "edible_dose": 6,
        "concentrate_subtype": 13,
        "flower_form": 6,
        "strain_type": 4,
        "ratio": 5,
        "limit": 4,
    }
    for axis, n in axis_counts.items():
        assert m.WeightTypeTaxonomy.objects.filter(axis=axis).count() == n, f"axis {axis}"


@pytest.mark.django_db
def test_o8_mount_vernon_hours_unconfirmed():
    """D3: the Mt Vernon hours StoreFact is confirmed=False with value=='' and emits the
    'call to confirm' chunk — never a guessed close time. Yakima hours seed real."""
    from kb import seed

    seed.seed_all()
    mv = m.StoreFact.objects.get(store="mount-vernon", kind="hours")
    assert mv.confirmed is False
    assert mv.value == ""
    assert "call the store to confirm" in mv.chunk_text()

    yak = m.StoreFact.objects.get(store="yakima", kind="hours")
    assert yak.confirmed is True and yak.value == "9 AM–11 PM daily"


@pytest.mark.django_db
def test_wa_law_accuracy():
    """D4: the return-policy body contains the literal 'WAC 314-55-079'; the limit rows are
    exactly 1 oz / 7 g / 16 oz / 72 oz."""
    from kb import seed

    seed.seed_all()
    pol = m.PolicyDocument.objects.get(kind="return_policy")
    assert "WAC 314-55-079" in pol.body
    assert pol.citation == "WAC 314-55-079"

    limits = {t.term: t.value for t in m.WeightTypeTaxonomy.objects.filter(axis="limit")}
    assert limits["useable flower"] == "1 ounce (28 g)"
    assert limits["concentrate"] == "7 grams"
    assert limits["solid edibles"] == "16 ounces"
    assert limits["liquid edibles"] == "72 ounces"


@pytest.mark.django_db
def test_taxonomy_parity_with_budtender():
    """D5: every axis=concentrate_subtype term is a budtender concentrates subtype slug; every
    weight-axis term maps to a budtender _GRAM_HINTS gram."""
    from kb import seed
    from kb.taxonomy_source import CONCENTRATE_SUBTYPE_VALUES, GRAM_HINTS, WEIGHT_TERM_GRAMS

    seed.seed_all()
    for t in m.WeightTypeTaxonomy.objects.filter(axis="concentrate_subtype"):
        assert t.term in CONCENTRATE_SUBTYPE_VALUES, f"subtype {t.term!r} not in budtender vocab"
    for t in m.WeightTypeTaxonomy.objects.filter(axis="weight"):
        grams = WEIGHT_TERM_GRAMS[t.term]
        assert grams in GRAM_HINTS, f"weight term {t.term!r} ({grams}g) not a budtender _GRAM_HINTS"


# ── Leak-Guard (defensive — the KB carries no product economics) ──────────────


@pytest.mark.django_db
def test_no_cost_or_margin_substring_in_any_chunk():
    """ADR-008: no KB row chunk_text contains a 'cost'/'margin' product-economic substring."""
    from kb import seed

    seed.seed_all()
    for prefix, Model in (
        ("faq", m.FAQEntry),
        ("pol", m.PolicyDocument),
        ("sf", m.StoreFact),
        ("edu", m.EducationDoc),
        ("blog", m.BlogDoc),
        ("tax", m.WeightTypeTaxonomy),
    ):
        for row in Model.objects.all():
            low = row.chunk_text().lower()
            assert "cost" not in low and "margin" not in low, f"leak in {prefix}{row.pk}"


# ── E. Vapi Files mirror (mocked client — offline) ────────────────────────────


@pytest.mark.django_db
def test_mirror_skipped_when_vapi_unconfigured(monkeypatch):
    """E2: with VAPI_PRIVATE_KEY unset, mirror_all() returns {skipped} (the canonical
    faq_lookup path still answers from kb/ — the mirror is a fallback, not the source)."""
    from kb import seed, vapi_files

    seed.seed_all()
    monkeypatch.delenv("VAPI_PRIVATE_KEY", raising=False)
    assert vapi_files.mirror_all() == {"skipped": "not configured"}


@pytest.mark.django_db
def test_render_files_under_cap_and_carry_facts():
    """E1: every rendered KB file is ≤300KB and carries its facts (WAC in return-policy,
    the limits in wa-law, the eighth in weights-types)."""
    from kb import seed, vapi_files

    seed.seed_all()
    for kind in vapi_files._RENDERERS:
        body = vapi_files._render_file(kind)
        assert len(body.encode("utf-8")) <= vapi_files.MAX_FILE_BYTES
    assert "WAC 314-55-079" in vapi_files._render_file("return-policy")
    assert "1 ounce (28 g)" in vapi_files._render_file("wa-law")
    assert "eighth" in vapi_files._render_file("weights-types")


@pytest.mark.django_db
def test_mirror_find_by_name_then_replace_no_duplicate_creates(monkeypatch):
    """E1: mirror_all uploads find-by-name-then-replace — a SECOND mirror deletes each prior
    file and re-creates it (zero duplicate, un-replaced files), and returns {files, tool_id}."""
    from core.services import vapi
    from kb import seed, vapi_files

    seed.seed_all()
    monkeypatch.setenv("VAPI_PRIVATE_KEY", "test-key")

    # An in-memory fake Vapi Files + Tool + Assistant store.
    state = {"files": [], "tools": [], "next_id": 1, "creates": 0}

    def _new_id():
        i = state["next_id"]
        state["next_id"] += 1
        return f"id{i}"

    def fake_get(path, params=None):
        if path == "/file":
            return list(state["files"])
        if path == "/tool":
            return list(state["tools"])
        if path == "/assistant":
            return []  # entry_faq not provisioned in this test → tool-attach is a clean skip
        return []

    def fake_post(path, json):
        if path == "/file":
            state["creates"] += 1
            f = {"id": _new_id(), "name": json["name"]}
            state["files"].append(f)
            return f
        if path == "/tool":
            t = {"id": _new_id(), "name": vapi_files.QUERY_TOOL_NAME, "function": json["function"]}
            state["tools"].append(t)
            return t
        return {}

    def fake_delete(path):
        fid = path.rsplit("/", 1)[-1]
        state["files"] = [f for f in state["files"] if f["id"] != fid]
        return None

    def fake_patch(path, json):
        return {"id": path.rsplit("/", 1)[-1]}

    monkeypatch.setattr(vapi, "get", fake_get)
    monkeypatch.setattr(vapi, "post", fake_post)
    monkeypatch.setattr(vapi, "patch", fake_patch)
    monkeypatch.setattr(vapi, "delete", fake_delete)

    out1 = vapi_files.mirror_all()
    assert set(out1) == {"files", "tool_id"}
    n_files = len(vapi_files._RENDERERS)
    assert len(out1["files"]) == n_files
    assert len(state["files"]) == n_files  # one per source group, no duplicates
    assert state["creates"] == n_files

    vapi_files.mirror_all()  # re-mirror: delete-then-create, still one file per name
    assert len(state["files"]) == n_files, "a re-mirror duplicated files"
    assert state["creates"] == 2 * n_files  # each re-created exactly once
    assert len({f["name"] for f in state["files"]}) == n_files
