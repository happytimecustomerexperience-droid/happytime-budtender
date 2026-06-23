# 22 — SPEC — KNOWLEDGE BASE: MODELS · EMBEDDINGS · SEED CONTENT — Executable Spec

> **Status:** EXECUTABLE SPEC (authoritative for the KB plane). Written 2026-06-22.
> **Subsystem:** S1 (Chassis + FAQ) — the KB plane (`01-ARCHITECTURE.md` §4). **Consumed by:** P0 (`10-P0-CHASSIS-FAQ.md` §3.4 / §4.7 / §5 — this doc is the deep KB-seed + embeddings spec P0 references), P1 (the `faq_lookup` tool reads `kb/` live alongside `suggest_products`), P4 (`14-P4-dashboard-publish.md` §3.1/§3.2 KB-source manager edits these rows + the reindex button calls this pipeline), P5 (`15-P5-polish-brand.md` §3.1 persona copy + §3.2 cartridge taxonomy parity reads the taxonomy rows).
> **Read order before executing (mandatory):** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → `10-P0-CHASSIS-FAQ.md` (the phase that builds these files) → this file.
> **Implements ADRs (binding, never contradicted here):** ADR-001 (fork swedish-bot chassis), ADR-008 (leak-safe), ADR-009 (speak OTD — prices spoken out-the-door), ADR-010 (gpt-4.1-mini assistants / Gemini server-side embeddings+grounding), ADR-012 (seed ALL listed sources; canonical = Django `kb/`, mirrored to Vapi Files), ADR-013 (swedish-bot embeddings engine: Gemini 768-dim Matryoshka + cached cosine; pgvector swap-seam documented), ADR-018 (spoken 21+ confirm; drop "peek at ID").
> **Ports from:** `swedish-bot` `kb/models.py` (`FAQEntry`~L150, `PolicyDocument`~L312, `SiteFAQ`~L368, `AgentPrompt`~L226, `FlowConfig`~L276), `kb/semantic.py` (`_corpus_vectors`~L52 content-hash cache, `rank_guides`~L103, `_cos`~L44, `RETRIEVAL_DOCUMENT`/`RETRIEVAL_QUERY` task split), `kb/ingest.py` (`parse_pdf_text`~L24, `ingest_pdf_bytes`~L39, `MAX_PDF_BYTES`~L15), `chat/context.py` (`collect_knowledge`~L20 — the cap+wrap+fail-safe retrieval idiom), `core/services/gemini.py` (`embed`~L284, `active_embedding_model`~L274), `core/constants.py` (`EMBED_DIM=768`~L45). **Net-new:** `kb/seed.py` (the seed content authored below), `kb/vapi_files.py` (Vapi Files mirror + Query Tool), the voice KB models (`StoreFact`/`EducationDoc`/`BlogDoc`/`WeightTypeTaxonomy`), the `rank_faq`/`reindex` voice entrypoints in `kb/semantic.py`.
>
> **One-line goal:** the `faq` (P0: `entry_faq`) assistant speaks **every** Happy Time fact — hours, payment, pickup, returns (incl. WAC 314-55-079), WA purchase limits, the FULL weights/types taxonomy, store facts for all 3 stores, weekly specials, and the distilled education/blog content — **grounded in concrete `kb/` rows seeded by `kb/seed.py`**, retrieved by the swedish-bot embeddings engine (Gemini 768-dim Matryoshka + cached cosine), mirrored to Vapi Files for a low-latency fallback — with the LLM never originating a figure (Numbers-Guard) and cost/margin physically absent from the surface (Leak-Guard).

---

## 0. Why this doc exists (gap G-5 + gap G-7)

`99-PLAN-REVIEW.md` flagged two gaps this spec closes:

- **G-7 (KB-seed content map):** "fold the KB-seed content map + embeddings acceptance into P0 so C1 and the embeddings engine become execution-ready." P0 §4.7 sketches the row map at table-summary depth and **explicitly defers the full content to this spec**. This doc writes the **actual rows out concretely** (the literal FAQ Q&As, the return-policy body, every store-fact, every WA limit, and the FULL weights/types taxonomy table) so seeding is copy-from-spec, not author-from-memory — and resolves G-7 with **§7's section→model→rows mapping table** (every research/brief section → the model + the exact rows it seeds).
- **G-5 (`20`–`24` spec docs):** the reviewer recommended extracting cross-cutting contracts into pinned `2X-SPEC` docs that the phase docs cite. P0 §10/§11 cites `20-SPEC-vapi-deploy.md` (the Vapi CRUD + signature scheme). **This is `22-SPEC-kb-seed.md`** — the KB contract: the models, the embeddings retrieval pipeline, the `faq_lookup` read path, the Vapi Files mirror, and the seed content. (The webhook/HMAC contract lives in `10-P0-CHASSIS-FAQ.md` §4 + `23-SPEC-security-guardrails.md`; `21` is the budtender contract, `24` reserved.)

**Boundary with P0:** P0 *builds* `kb/models.py`, `kb/semantic.py`, `kb/seed.py`, `kb/vapi_files.py`, the `seed_kb` command, and `voice/tools/faq.py`; **this doc is the authoritative content + contract those files implement.** Where P0 and this doc both state a shape, **this doc is the source of truth for KB content and the embeddings pipeline detail**; P0 is the source of truth for the webhook envelope and provisioning. Neither contradicts the other (cross-checked in §11).

**Model-name note (drift guard):** the task brief names the taxonomy model `WeightsTypesTaxonomy`; P0 §3.4/§4.7 names it `WeightTypeTaxonomy`. **The canonical class name is `WeightTypeTaxonomy`** (matches P0, which builds the migration). `WeightsTypesTaxonomy` is treated as a synonym in prose only — do **not** create two models. A unit test (§9) asserts exactly one taxonomy model exists.

---

## 1. Goal & scope

### 1.1 In scope (this spec defines all of)

1. **The KB models** — the final field shapes for the six voice KB models: `FAQEntry`, `PolicyDocument`, `StoreFact`, `EducationDoc`, `BlogDoc`, `WeightTypeTaxonomy` (§3). Forked/slimmed from swedish-bot where a parent exists; net-new where it does not.
2. **The EMBEDDINGS retrieval pipeline** — Gemini `embed()` (768-dim Matryoshka, `RETRIEVAL_DOCUMENT` for chunks / `RETRIEVAL_QUERY` for the query) + `kb/semantic.py`'s content-hash-keyed cached in-memory cosine; the `rank_faq()` / `reindex()` voice entrypoints; the deterministic keyword fallback; the **pgvector swap-seam**; a **reindex management command** (§4). File-tasks + acceptance.
3. **The `faq_lookup` tool read path** — how `voice/tools/faq.py` reads `kb/` live (canonical, instant edits), assembles a grounded answer, and never composes a figure (§5).
4. **The Vapi Files mirror** — `kb/vapi_files.py` renders the curated KB into ≤300KB markdown files, uploads them, and attaches a Vapi **Query Tool** to the `faq` assistant as the low-latency grounded fallback (§6).
5. **The SEED CONTENT, written out concretely** — the literal rows `kb/seed.py` creates: the FAQ Q&As (§8.1), the return policy incl. WAC 314-55-079 (§8.2), store-facts for all 3 stores + specials (§8.3), the WA purchase limits (§8.4), the FULL weights/types taxonomy table (§8.5), and the education + blog ingestion rows (§8.6 / §8.7), plus the `entry_faq` `AgentPrompt` persona row (§8.8).
6. **The G-7 mapping table** — every research/brief section → the model + the rows it seeds (§7).

### 1.2 Out of scope (other docs)

- The webhook envelope, HMAC verification, and the `tool-calls` result shape → `10-P0-CHASSIS-FAQ.md` §4 + `23-SPEC-security-guardrails.md`. This doc states only what `faq_lookup` puts **inside** the `result` object.
- The full `core/services/vapi.py` CRUD + the Files-API endpoint shapes → `20-SPEC-vapi-deploy.md`. This doc states what `vapi_files.py` calls, not how the client is built.
- Product suggestions / Dutchie / budtender ranking → P1 + `_research-suggestion-engine.md`. The KB carries **no product inventory** — products come live from budtender; the KB carries education/taxonomy/policy only.
- The dashboard KB-source-manager UI + reindex button → P4 (`14` §3.1/§3.2). This doc defines the pipeline the button calls.
- The brand/persona theming pass → P5 (`15` §3.1). This doc seeds the persona `AgentPrompt` body P5 finalizes.
- Verbatim house education copy (blocked by the Vercel wall) → seeded `provisional`, re-runnable; see §8.6 + the §10 risk.

### 1.3 Non-negotiable boundaries (binding)

- **Numbers-Guard (ADR-012, `_research-education-blogs.md` §1 house rule).** Every figure the agent can speak — hours, prices, limits, mg doses, gram weights, THC:CBD ratios — **lives in a KB row**. The LLM only phrases KB row text; it never originates a number. `faq_lookup` returns row text verbatim-ish; `grounded:false` on no match → the assistant offers a human, never invents.
- **Leak-Guard (ADR-008).** The KB contains **no cost/margin** (it has no product economics at all). The `faq_lookup` response is still run through `voice/guardrails.assert_no_leak` (defensive — guards the surface before P1 adds products). No KB row may contain a `"cost"`/`"margin"` substring describing a product economic — a seed test asserts this.
- **OTD pricing (ADR-009).** Any price the KB mentions (e.g. ATM fee, a special's discount framing) is described as out-the-door / what the customer pays. The KB does **not** store product prices (those are live from budtender, OTD).
- **Canonical = Django `kb/`, instant edits (ADR-012).** A KB row edit is live on the next call with no redeploy (the content-hash cache self-invalidates). The Vapi Files mirror is a **fallback**, re-pushed by the reindex button — never the source of truth.
- **Conservative-on-dosing house rule.** Education rows that touch dosing/medical claims carry the conservative framing (start low, wait 2h, never over-promise strain-type effects, cite the source). The persona prompt enforces it; the rows supply the anchored numbers.
- **WA-law accuracy.** The return-policy row cites **WAC 314-55-079** (defective-product exception). The purchase-limit rows are the standard adult-use limits (per WAC 314-55-095 / WSLCB). These are quoted, never paraphrased loosely.

---

## 2. Dependencies (what MUST exist first)

This spec is **authored alongside P0** (it is the content P0's `kb/seed.py` carries). Its only hard prerequisites are the swedish-bot source to fork and the chassis P0 stands up.

| # | Dependency | Where it comes from | Graceful-degradation if absent |
|---|---|---|---|
| D1 | swedish-bot `kb/` source (`models.py`, `semantic.py`, `ingest.py`) + `chat/context.py` retrieval idiom | `C:\Users\vladi\OneDrive\Desktop\swedish-bot\kb\*` (confirmed present, §12) | n/a — the fork source. |
| D2 | `core/services/gemini.py` (verbatim lift) + `core/constants.py` (`EMBED_DIM=768`) | P0 §3.2 (lifted verbatim, ADR-001) | If Gemini auth is absent, `rank_faq` falls back to deterministic keyword match over the same rows (still grounded — §4.4). |
| D3 | The Django cache backend (LocMem dev / Redis prod) | P0 chassis `config/settings.py` (ported) | The content-hash cosine cache needs a cache backend; LocMem is fine at this scale (the corpus is dozens of rows). No pgvector. |
| D4 | `core/services/vapi.py` Files CRUD (upload/list/delete file; attach Query Tool) | P0 §3.2 (stub) + `20-SPEC-vapi-deploy.md` (full) | `vapi_files.mirror_all()` degrades to `{skipped:"not configured"}` when `VAPI_PRIVATE_KEY` unset; `rank_faq` still serves from `kb/` (the canonical path). |
| D5 | `voice/guardrails.assert_no_leak` | P0 §3.3 | The Leak-Guard wrap on the `faq_lookup` result. |
| D6 | `voice/tools/__init__.py::register` (the tool registry, ADR-020) | P0 §3.3 | `faq.py` self-registers `faq_lookup`. |
| D7 | Owner confirmation of Mt Vernon hours (**O-8**) | owner | Seed Mt Vernon hours `confirmed=False` "call to confirm"; never speak a guessed close time (§8.3). |
| D8 | Verbatim house education copy (Vercel wall, **O-10/provenance**) | owner (browser/computer-use MCP capture) | Seed the `[SITE]`-distilled education rows `provisional=True`; re-run `seed_kb` to update with verbatim copy later (§8.6). |

**Graceful-degradation rule (inherited from P0):** every external dependency is read at call time, never required at import. Seeding works with no Gemini auth (rows are plain text); retrieval degrades to keyword match; the Vapi mirror skips cleanly. The FAQ answer is correct either way.

---

## 3. The KB models (file-by-file: `kb/models.py`)

**Format:** each model → responsibility → final field shape → port-from (swedish-bot path) or net-new. The migration lands in `kb/migrations/` (P0 ships it; `makemigrations --check` exit 0 is a P0 gate). All text models carry a shared retrieval-priority `weight` (int) + a `kind`/`topic`/`axis` tag so `rank_faq` can build one heterogeneous corpus.

### 3.1 `FAQEntry` — the Q&A surface

Responsibility: one row per spoken FAQ (hours/payment/pickup/returns/limits/specials/general). The single most-hit KB model. **Forked + simplified** from swedish-bot's `FAQEntry`+`FAQEntryText` (which is HVAC-category-scoped + i18n-split) into a flat single-row English model (this tenant is English-only; no per-category FK — voice FAQs are store-or-global).

```python
class FAQEntry(models.Model):
    key        = models.SlugField(max_length=64, unique=True)   # natural key for idempotent seed ("hours-close")
    question   = models.TextField()                              # canonical spoken question
    answer     = models.TextField()                              # grounded answer — Numbers-Guard: every figure is here
    paraphrases = models.JSONField(default=list, blank=True)     # alt phrasings to widen embedding recall
    store      = models.CharField(max_length=32, blank=True)     # "" = global; else yakima|mount-vernon|pullman
    topic      = models.CharField(max_length=32, blank=True)     # hours|payment|pickup|returns|limits|specials|general|age
    weight     = models.IntegerField(default=100)                # retrieval priority tiebreak (higher first)
    is_active  = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def chunk_text(self) -> str:                                 # the (id,text) the embedder/keyword matcher sees
        extra = (" / ".join(self.paraphrases)) if self.paraphrases else ""
        return f"Q: {self.question}\n{extra}\nA: {self.answer}".strip()
```
**Port-from:** `swedish-bot/kb/models.py` `FAQEntry`~L150 + `FAQEntryText`~L166 (collapse the i18n split into the flat model). `chunk_text()` mirrors `kb/semantic.rank_guides`~L116 `f"Q: {q}\nA: {a}"`.

### 3.2 `PolicyDocument` — the return policy (+ any future policy)

Responsibility: company-level policy bodies the FAQ cites — primarily the **return policy with WAC 314-55-079**. **Forked** from swedish-bot's `PolicyDocument`~L312 (which is PDF-backed Swedish terms) into a body-text model with an optional PDF attachment (so a future PDF can be ingested via `kb/ingest.py`).

```python
class PolicyDocument(models.Model):
    POLICY_KINDS = [("return_policy", "Return policy"), ("privacy", "Privacy"),
                    ("loyalty", "Loyalty terms"), ("other", "Other policy")]
    kind        = models.CharField(max_length=32, choices=POLICY_KINDS, unique=True)  # one per kind → idempotent
    title       = models.CharField(max_length=200)
    body        = models.TextField()                            # the spoken-grounding body (WAC cite lives here)
    citation    = models.CharField(max_length=64, blank=True)  # "WAC 314-55-079" — surfaced as a source
    source_url  = models.URLField(blank=True)
    pdf         = models.FileField(upload_to="policy/", blank=True, null=True)  # optional; ingested via kb/ingest
    sha256      = models.CharField(max_length=64, blank=True)
    weight      = models.IntegerField(default=120)             # policy outranks generic FAQ on a returns query
    is_active   = models.BooleanField(default=True)
    updated_at  = models.DateTimeField(auto_now=True)

    def chunk_text(self) -> str:
        cite = f" ({self.citation})" if self.citation else ""
        return f"{self.title}{cite}: {self.body}"
```
**Port-from:** `swedish-bot/kb/models.py` `PolicyDocument`~L312 (keep `kind`/`title`/`pdf`/`sha256`/`parsed_text`→`body`; drop the Swedish `cite_on` JSON). PDF path reuses `kb/ingest.ingest_pdf_bytes`~L39.

### 3.3 `StoreFact` — per-store + global facts

Responsibility: the operational facts the agent localizes — address, phone, hours, email, payment, pickup, specials, limits. **Net-new**, shaped from swedish-bot's `SiteFAQ`~L368 (a flat fact-store with `is_active`+`updated_at`). The **`confirmed` flag** carries O-8 (Mt Vernon hours stay unspoken until confirmed).

```python
class StoreFact(models.Model):
    KINDS = [("address","Address"),("phone","Phone"),("hours","Hours"),("email","Email"),
             ("payment","Payment"),("pickup","Pickup"),("special","Weekly special"),
             ("limit","WA purchase limit"),("age","Age requirement")]
    store      = models.CharField(max_length=32, blank=True)   # "" = applies to all stores
    kind       = models.CharField(max_length=16, choices=KINDS)
    label      = models.CharField(max_length=120)              # human label ("Yakima hours")
    value      = models.TextField()                            # the spoken value ("9 AM–11 PM daily")
    confirmed  = models.BooleanField(default=True)             # O-8: False => "call to confirm", never spoken as fact
    weight     = models.IntegerField(default=110)
    is_active  = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("store", "kind", "label")]         # natural key for idempotent seed

    def chunk_text(self) -> str:
        scope = f"{self.store} " if self.store else ""
        if not self.confirmed:
            return f"{scope}{self.label}: not confirmed — ask the caller to call the store to confirm."
        return f"{scope}{self.label}: {self.value}"
```
**Port-from:** `swedish-bot/kb/models.py` `SiteFAQ`~L368 (the flat-fact shape: `slug`/`question`/`answer`/`is_active`/`updated_at` → `kind`/`label`/`value`/`confirmed`).

### 3.4 `EducationDoc` — distilled education content

Responsibility: one row per `happytimeweed.com/education/*` page (edibles, microdosing, strain types, storage, THC/CBD) — the longer-form teaching content the agent draws on for "how much should I take," "what's a hybrid," "how do I store this." **Net-new**; an education analogue of `swedish-bot/kb/models.GenericGuide`~L176.

```python
class EducationDoc(models.Model):
    slug       = models.SlugField(max_length=120, unique=True)
    title      = models.CharField(max_length=200)
    topic      = models.CharField(max_length=48)               # edibles|microdosing|strains|storage|thc-cbd|concentrates
    body       = models.TextField()                            # distilled [SITE] content (conservative dosing framing)
    source_url = models.URLField(blank=True)
    provisional = models.BooleanField(default=True)            # True until verbatim house copy lands (Vercel wall)
    weight     = models.IntegerField(default=80)               # education ranks below operational FAQ
    is_active  = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def chunk_text(self) -> str:
        return f"[{self.topic}] {self.title}: {self.body}"
```
**Port-from:** `swedish-bot/kb/models.py` `GenericGuide`~L176 (the `kind`/`body`/per-category guide shape → `topic`/`body`/`slug`).

### 3.5 `BlogDoc` — distilled blog content

Responsibility: one row per `happytimeweed.com/blog/*` post (disposable-vape how-to, Yakima dispensary SEO posts) — lighter than education, used for "how do I use a disposable" and brand/community questions. **Net-new** (same shape as `EducationDoc` minus `topic`, plus `published` framing).

```python
class BlogDoc(models.Model):
    slug       = models.SlugField(max_length=160, unique=True)
    title      = models.CharField(max_length=200)
    body       = models.TextField()                            # distilled post content
    source_url = models.URLField(blank=True)
    provisional = models.BooleanField(default=True)
    weight     = models.IntegerField(default=60)               # blog ranks lowest (least authoritative for facts)
    is_active  = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def chunk_text(self) -> str:
        return f"[blog] {self.title}: {self.body}"
```

### 3.6 `WeightTypeTaxonomy` — the FULL weights/types reference

Responsibility: the canonical, structured weights + types + dose + ratio table — the single source the agent quotes for "how many grams in an eighth," "what's a microdose," "what sizes do carts come in," "what's the flower limit," "what's solventless." **Net-new.** One row per (axis, term). **Taxonomy parity (binding, `15` §3.2 / `_research-education-blogs.md` §11 TODO 2):** the `axis` + `term` vocabulary stays **identical** to budtender `ranking.py`'s `CATEGORY_BY_SLOTKEY` / `_SUBTYPE_KEYWORDS` / `_GRAM_HINTS` so the spoken vocabulary and the suggestion API speak the same language.

```python
class WeightTypeTaxonomy(models.Model):     # canonical name (P0). "WeightsTypesTaxonomy" is a prose synonym only.
    AXES = [("weight","Flower/concentrate weight"),("cart_size","Cartridge size"),
            ("preroll","Pre-roll format"),("edible_dose","Edible dosing"),
            ("concentrate_subtype","Concentrate subtype"),("flower_form","Flower form"),
            ("strain_type","Strain type"),("ratio","THC:CBD ratio"),("limit","WA purchase limit")]
    axis       = models.CharField(max_length=32, choices=AXES)
    term       = models.CharField(max_length=64)               # "eighth" / "microdose" / "live resin"
    value      = models.CharField(max_length=120, blank=True)  # "3.5 g" / "1–2.5 mg THC" / "" for descriptive rows
    notes      = models.TextField(blank=True)                  # "the default flower unit customers shop by"
    synonyms   = models.JSONField(default=list, blank=True)    # ["1/8 oz","eight-ball"] — widen recall + spoken match
    weight     = models.IntegerField(default=90)
    is_active  = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("axis", "term")]                   # natural key for idempotent seed

    def chunk_text(self) -> str:
        syn = (" (also: " + ", ".join(self.synonyms) + ")") if self.synonyms else ""
        val = f" = {self.value}" if self.value else ""
        return f"[{self.axis}] {self.term}{val}{syn}. {self.notes}".strip()
```

### 3.7 `AgentPrompt` / `FlowConfig` (referenced, owned by P0)

`AgentPrompt` (the persona/system-prompt row, swedish-bot `kb/models.py`~L226 + the P4 voice fields per `14` §4.1) and `FlowConfig` (the singleton flow graph, ~L276) are **forked by P0**, not re-defined here. This spec only seeds the **one `entry_faq` `AgentPrompt` row** (§8.8) — the persona body + Numbers-Guard system prompt. P5 finalizes the Koptza copy (`15` §3.1).

---

## 4. The EMBEDDINGS retrieval pipeline (`kb/semantic.py` — gap G-6 detail)

The retrieval engine is lifted from swedish-bot and retargeted to the voice KB. **It is an augmentation with a deterministic fallback — it never breaks the answer** (the swedish-bot fail-safe contract: any embedding error → empty result → caller keeps the deterministic answer).

### 4.1 The corpus builder

`rank_faq(query, store=None, top_k=3)` builds ONE heterogeneous corpus from every text KB model, each row → one `(chunk_id, chunk_text)`:

| Model | Included rows | `chunk_id` prefix | Store scoping |
|---|---|---|---|
| `FAQEntry` | `is_active=True` | `faq{pk}` | rows with `store==""` always; store-specific rows only when `store` matches |
| `PolicyDocument` | `is_active=True` | `pol{pk}` | always (global) |
| `StoreFact` | `is_active=True` | `sf{pk}` | rows with `store==""` always; per-store rows only when `store` matches; **`confirmed=False` rows emit the "call to confirm" chunk** |
| `EducationDoc` | `is_active=True` | `edu{pk}` | always |
| `BlogDoc` | `is_active=True` | `blog{pk}` | always |
| `WeightTypeTaxonomy` | `is_active=True` | `tax{pk}` | always |

Each row's `chunk_text()` (defined per-model in §3) is the embedded/matched text — Q+paraphrases+A for FAQ, label+value for facts, axis+term+value+synonyms for taxonomy. **Store filtering happens at corpus-build time** so a Yakima caller never gets a Pullman-hours chunk.

### 4.2 Embedding + the content-hash cosine cache (port verbatim-ish)

Lifted from `swedish-bot/kb/semantic._corpus_vectors`~L52:

1. Build a content hash over every `(chunk_id, chunk_text)` pair (`sha256`).
2. Cache key = `f"faq:{gemini.active_embedding_model()}:{constants.EMBED_DIM}:{content_sha[:16]}"` — **bound to the embedding model + dim** so query/corpus vectors can never be compared across a model/dim change, and **self-invalidating** the instant any row text changes (no redeploy — that's the live-edit property, P0 acceptance C2).
3. On cache miss: `gemini.embed([t for _,t in items], task_type="RETRIEVAL_DOCUMENT")` (768-dim Matryoshka), store `{chunk_id: vector}` in the Django cache (TTL 3600s).
4. The query is embedded `gemini.embed(query, task_type="RETRIEVAL_QUERY")`; `_cos`~L44 in-memory cosine ranks; top-`k` returned as `[(row, cosine), …]` (resolve `chunk_id`→row via a `{prefix→Model}` map).

**768-dim Matryoshka note:** `core/constants.EMBED_DIM=768` is the truncation dim of Gemini's Matryoshka embedding (the model emits a longer vector; we use the first 768 dims — the swedish-bot default). The cache key pins `EMBED_DIM` so a future dim change invalidates cleanly.

### 4.3 The `rank_faq` / `reindex` signatures (the voice entrypoints)

```python
def rank_faq(query: str, store: str | None = None, top_k: int = 3) -> list[tuple[object, float]]:
    """Top-k KB rows for the query, store-scoped. Empty on no corpus; degrade-safe on
    embedding error (keyword fallback). Each element = (model_instance, cosine|keyword_score)."""

def reindex() -> int:
    """Force-rebuild the cosine cache for the current corpus; return the chunk count.
    Called by the dashboard reindex button (P4) and `seed_kb --reindex` (P0)."""
```
`rank_faq` adapts `swedish-bot/kb/semantic.rank_guides`~L103 (corpus build → embed → cosine → top-k). `reindex` is net-new (re-embeds the corpus, repopulating the cache, returns `len(items)`) and also triggers `vapi_files.mirror_all()` at the command/button layer (not inside `semantic.py` — keep the Vapi dependency out of the retrieval module).

### 4.4 The deterministic keyword fallback (degrade-safe)

If `enabled()` is False (no `SEMANTIC_SEARCH_ENABLED`) OR `gemini.embed` raises (no auth / API down), `rank_faq` falls back to a **deterministic keyword/substring score** over the same `chunk_text()` corpus:
- Tokenize the query (lowercase, strip punctuation); score each chunk by (overlap of query tokens with chunk tokens) + a small boost for `FAQEntry.paraphrases` / `synonyms` hits + the row's `weight`/100 as a tiebreak.
- Return the top-`k` by that score. **Still grounded** (the answer is a real KB row), just lower recall on paraphrases.
- This mirrors swedish-bot's "embedding error → empty → caller keeps the trigram answer" pattern (`semantic.py` docstring), adapted: here the *keyword* match IS the fallback path (there's no trigram index), so it returns rows rather than empty.

**Acceptance (C3, §9):** with `gemini.embed` mocked to raise, `rank_faq("what time do you close","yakima")` still returns the Yakima-hours `StoreFact` as top row.

### 4.5 The pgvector swap-seam (ADR-013 — documented, not built)

Documented in the `kb/semantic.py` module docstring (carried from swedish-bot's seam note): *"The cached-cosine corpus is the swap seam. The KB is dozens of rows; in-memory cosine is fine. Past a few thousand rows, replace `_corpus_vectors` + the `_cos` loop with a pgvector ANN query (`CREATE EXTENSION vector`, an `embedding vector(768)` column on each KB model, an HNSW index, `ORDER BY embedding <=> query_vec LIMIT k`) — keeping the SAME `rank_faq(query, store, top_k)` signature so no caller changes."* This is an **EXP item** (`16` backlog), not P0.

### 4.6 The reindex management command

`kb/management/commands/reindex_kb.py` (net-new; sibling to P0's `seed_kb.py`):
```
python manage.py reindex_kb            # semantic.reindex() → prints "{n} chunks reindexed"
python manage.py reindex_kb --mirror   # also kb.vapi_files.mirror_all() → "{m} files mirrored, tool {id}"
```
The dashboard "Reindex" button (P4 `14` §3.2 `kb_reindex`) calls the same `semantic.reindex()` + `vapi_files.mirror_all()` pair. **Bounded work** (the corpus is small — `14` §9 risk): runs inline in the request; if the KB ever grows, move to this command + a "reindex queued" toast (the documented seam).

### 4.7 File-tasks (this section's deliverables)

| Path | Responsibility | Key functions | Port from |
|---|---|---|---|
| `kb/semantic.py` | Voice retrieval engine | `rank_faq`, `reindex`, `_corpus_vectors`, `_cos`, `_keyword_fallback`, `enabled` | `swedish-bot/kb/semantic.py` (whole file; `_corpus_vectors`~L52, `rank_guides`~L103→`rank_faq`, `_cos`~L44) |
| `kb/management/commands/reindex_kb.py` ★ | CLI reindex (+ optional mirror) | `handle(--mirror)` | swedish-bot `kb/management/commands/*` pattern |

**Acceptance (G-6):** §9 AC-B (rank correctness, content-hash invalidation, keyword fallback, reindex count).

---

## 5. The `faq_lookup` tool read path (`voice/tools/faq.py`)

`faq_lookup` is the **canonical** KB read (path 2, ADR-012 — edits are instant). The Vapi Query Tool over mirrored Files (§6) is the fast fallback (path 1). The tool is registered by P0 (`from . import faq`) and lives behind the one webhook (P0 §4.3).

**Signature + behavior:**
```python
@register("faq_lookup")
def faq_lookup(args: dict, ctx: dict) -> dict:
    query = (args.get("query") or "").strip()
    store = args.get("store") or ctx.get("store") or settings.HHT_DEFAULT_STORE   # "yakima" default (O-4)
    hits = semantic.rank_faq(query, store=store, top_k=3)
    if not hits:
        return {"answer": None, "grounded": False,
                "fallback": "Let me get a team member who can help with that.",
                "store": store}
    top, _score = hits[0]
    answer = _compose(top, hits)          # KB row text ONLY — never a composed figure (Numbers-Guard)
    return {"answer": answer, "grounded": True,
            "sources": [{"kind": _kind(r), "id": r.pk, "title": _title(r)} for r, _ in hits],
            "store": store}
```

**Binding rules:**
- **`_compose` returns KB text verbatim-ish** — it picks the top chunk's `answer`/`value`/`body` and (for multi-row answers like "what are the specials") concatenates the matching rows' values. It **never** synthesizes a number not present in a row. A query whose answer would require an invented figure → the top chunk's text is spoken as-is, or `grounded:False` if nothing matches.
- **`grounded:False` ⇒ offer a human** — the assistant prompt (§8.8) instructs: when `grounded` is false, say "let me get a team member" and never guess.
- **Store-scoped** — store-specific facts (hours/address/phone) are filtered to `store` in `rank_faq`; a global fact (payment/pickup/limits/taxonomy) returns regardless.
- **Leak-Guard** — `voice/guardrails.assert_no_leak(result)` runs before return (P0 §4.3). The KB has no cost/margin, but the wrap is mandatory (defensive, ADR-008).
- **Multi-store disambiguation** — if `query` mentions a store name ("in Pullman"), `ctx`/`store` is overridden to that store before `rank_faq` (a cheap substring check on the canonical store slugs in `voice/routing.py` if present, else inline). Never guess the store silently for a store-specific fact — if `store` is unknown and the fact is store-specific, the answer asks which store.

**Result shape** (the `result` object inside the P0 §4.3 tool-result envelope — frozen here):
```json
{ "answer": "Our Yakima store is open until 11 PM tonight.",
  "grounded": true,
  "sources": [{ "kind": "store_fact", "id": 12, "title": "Yakima hours" }],
  "store": "yakima" }
```
`kind ∈ {faq, policy, store_fact, education, blog, taxonomy}`. The full webhook envelope wrapping this is P0 §4.3.

---

## 6. The Vapi Files mirror (`kb/vapi_files.py`)

The low-latency grounded **fallback** (path 1, ADR-012): a snapshot of the curated KB pushed to Vapi Files + a Vapi **Query Tool** (Gemini retrieval) attached to the `faq` assistant. Canonical truth stays in `kb/`; this is re-pushed on every reindex.

**`mirror_all() -> dict`:**
1. Render the KB into **≤300KB markdown files** (one per source group), each a flat dump of the active rows' `chunk_text()`:
   - `faq.md` — all `FAQEntry` (global + per-store, sectioned by store).
   - `return-policy.md` — the `return_policy` `PolicyDocument` (WAC 314-55-079).
   - `store-facts.md` — all `StoreFact` (per-store sections; `confirmed=False` rendered as "call to confirm").
   - `wa-law.md` — the `limit`-axis taxonomy + `StoreFact kind=limit` + the age rule.
   - `weights-types.md` — the full `WeightTypeTaxonomy` table (every axis).
   - `education.md` — `EducationDoc` + `BlogDoc` (the longer-form content; chunked under the 300KB cap; split into `education-2.md` if it overflows).
2. Upload each via `core/services/vapi.py` Files CRUD (idempotent: **find-by-name then replace** — delete the prior file of that name, upload the new — so a re-mirror never duplicates).
3. `ensure_query_tool()` — find/create a Vapi **Query Tool** referencing the uploaded file ids, attach its `toolId` to the `faq`/`entry_faq` assistant's `toolIds` (alongside `faq_lookup`).
4. Return `{"files": [{name, id}…], "tool_id": "…"}` — or, with `VAPI_PRIVATE_KEY` unset, `{"skipped": "not configured"}`.

**`_render_file(kind) -> str`** builds each markdown body (≤300KB; assert the cap, split if exceeded). **`ensure_query_tool()`** is GET-then-PATCH (idempotent). The exact Files-API endpoint shapes are in `20-SPEC-vapi-deploy.md`; this doc states only what `vapi_files.py` calls.

**Boundary:** the mirror is **never** the source of truth and is **never** read by `faq_lookup` (that reads `kb/` live). The mirror exists only so the Vapi-side Query Tool can answer with sub-100ms retrieval when the assistant prefers it; an edit not yet mirrored is still served correctly by the canonical `faq_lookup`. The dashboard reindex button (P4) re-mirrors.

---

## 7. G-7 RESOLUTION — the section → model → rows mapping table

**This table is the resolution of gap G-7:** every research/brief section maps to the model + the concrete rows it seeds. `kb/seed.py` implements exactly this; §8 writes the row content out.

| # | Source section (file → §) | Model | Rows seeded (count) | Seed fn |
|---|---|---|---|---|
| 1 | Synthesis brief §2 FAQ + `_research-education-blogs.md` §10/§11 | `FAQEntry` | 8 Q&As: age-21, payment, delivery/pickup, ready-time, limits, returns, specials, ID-required (§8.1) | `seed_faq()` |
| 2 | Synthesis brief §2 Return-policy + `_research-education-blogs.md` §10 (WAC 314-55-079) | `PolicyDocument` | 1 row `kind=return_policy`, `citation="WAC 314-55-079"` (§8.2) | `seed_return_policy()` |
| 3 | Synthesis brief §2 Store-facts + roadmap §10 (3 stores) | `StoreFact` | Yakima (address/phone/hours/email), Mt Vernon (address/phone + **hours `confirmed=False`**), Pullman (address/phone/hours); global payment/pickup/email/age = **~14 rows** (§8.3) | `seed_store_facts()` |
| 4 | Synthesis brief §2 weekly specials | `StoreFact kind=special` | 5 specials (Flower Mon / Cyber Tue / Wax Wed / Self-Care Thu / Happy Fri) (§8.3) | `seed_store_facts()` |
| 5 | `_research-education-blogs.md` §10 WA limits | `StoreFact kind=limit` + `WeightTypeTaxonomy axis=limit` | 4 limit rows (flower 1oz / concentrate 7g / solid-edible 16oz / liquid-edible 72oz) + the age/ID rule (§8.4) | `seed_wa_limits()` |
| 6 | `_research-education-blogs.md` §9 (weights ladder) | `WeightTypeTaxonomy axis=weight` | 10 rows (0.5g…28g, eighth/quarter/half/oz named) (§8.5.A) | `seed_weights_types()` |
| 7 | `_research-education-blogs.md` §9 (cart sizes) | `axis=cart_size` | 2 rows (0.5g, 1g) (§8.5.B) | `seed_weights_types()` |
| 8 | `_research-education-blogs.md` §9 (pre-roll) | `axis=preroll` | 3 rows (single, 5-pack, 10-pack) (§8.5.C) | `seed_weights_types()` |
| 9 | `_research-education-blogs.md` §2/§3/§9 (edible dosing) | `axis=edible_dose` | 6 rows (microdose, beginner, standard, WA-max-pack, onset, peak/wait) (§8.5.D) | `seed_weights_types()` |
| 10 | `_research-education-blogs.md` §6 (concentrate subtypes) | `axis=concentrate_subtype` | ~13 rows (rosin/live-resin/RSO/distillate/diamonds/sauce/badder/shatter/crumble/sugar/wax/bubble-hash/kief) (§8.5.E) | `seed_weights_types()` |
| 11 | `_research-education-blogs.md` §6 (flower forms) | `axis=flower_form` | ~6 rows (whole-bud/smalls/shake/pre-roll/infused-pre-roll/blunt) (§8.5.F) | `seed_weights_types()` |
| 12 | `_research-education-blogs.md` §5 (strain types) | `axis=strain_type` | 3 rows (indica/sativa/hybrid + the "label is general" house note) (§8.5.G) | `seed_weights_types()` |
| 13 | `_research-education-blogs.md` §4 (THC:CBD ratios) | `axis=ratio` | 5 rows (1:1, 2:1, 5:1, 20:1, CBN) (§8.5.H) | `seed_weights_types()` |
| 14 | `_research-education-blogs.md` §2–§7 (education pages) | `EducationDoc` | 5 rows (edibles, microdosing, strain-types, storage, thc-cbd) — `provisional=True` (§8.6) | `seed_education()` |
| 15 | `_research-education-blogs.md` Provenance table (blogs) | `BlogDoc` | 3 rows (disposable-vape how-to, best-dispensary-yakima, rec-marijuana-yakima) — `provisional=True` (§8.7) | `seed_blogs()` |
| 16 | `_research-education-blogs.md` §8 house style + ADR-018 + roadmap persona | `AgentPrompt` | 1 row `role="faq"` (entry_faq) — Koptza persona + Numbers-Guard FAQ prompt (§8.8) | `seed_agent_prompts()` |

`seed_all()` runs blocks 1–16 in order; `python manage.py seed_kb` calls it; `--reindex` also runs `semantic.reindex()` + `vapi_files.mirror_all()`. Every block is `get_or_create` by the model's natural key (idempotent — P0 acceptance D1).

---

## 8. THE SEED CONTENT (written out concretely)

> This is the literal content `kb/seed.py` carries. Numbers-Guard: every figure below becomes a KB row so the agent quotes it, never invents it. Facts labeled `[CONFIRMED]` are confirmed store facts; `[WA-LAW]` are statutory; `[SITE]`/`[GENERAL]` carry the provenance from `_research-education-blogs.md`.

### 8.1 FAQ (`FAQEntry`) — the literal Q&As

| key | question | answer | store | topic |
|---|---|---|---|---|
| `age-21` | "Do I need to be 21?" / "What's the age?" | "Yes — you must be 21 or older with a valid government-issued photo ID for recreational purchase." | "" | age |
| `payment` | "Do you take cards? How do I pay?" | "We take cash and debit only, and there's an on-site ATM if you need it." | "" | payment |
| `delivery` | "Do you deliver?" | "No delivery — it's pickup only, which is Washington state law. You can order online and pick up in store." | "" | pickup |
| `ready-time` | "How long until my order is ready?" | "Online orders are usually ready for pickup in about 15 minutes." | "" | pickup |
| `limits` | "What are the purchase limits?" / "How much can I buy?" | "Per visit you can buy up to 1 ounce of useable flower, 7 grams of concentrate, 16 ounces of solid edibles, or 72 ounces of liquid edibles." | "" | limits |
| `returns` | "Can I return a product?" / "What's your return policy?" | "All sales are final, but under Washington law (WAC 314-55-079) a defective product — like a vape cart that won't fire — can be exchanged with no time limit. Bring the original packaging with a legible lot ID and your receipt, and a team member will take care of it." | "" | returns |
| `specials` | "What are this week's specials?" / "Any deals?" | "We run a daily deal: Flower Monday 30% off, Cyber Tuesday 30% off online, Wax Wednesday 25% off, Self-Care Thursday 25% off, and Happy Friday 30% off online." | "" | specials |
| `id-required` | "Do I need to bring ID?" | "Yes — bring a valid government-issued photo ID; you'll need it at pickup, and you must be 21 or older." | "" | age |

`paraphrases` seeded per row to widen recall — e.g. `returns`: `["can I return a vape","my cart is broken","refund","exchange a defective product","dead cartridge"]`; `payment`: `["credit card","do you take debit","ATM","cash only"]`; `limits`: `["how much flower can I buy","ounce limit","edible limit"]`.

### 8.2 Return policy (`PolicyDocument`) — the literal body

One row, `kind="return_policy"`, `title="Return policy"`, `citation="WAC 314-55-079"`, `source_url="https://happytimeweed.com/dispensary-faqs/"`, `weight=120`:

> **Body:** "All sales are final. The one exception, allowed under Washington Administrative Code **WAC 314-55-079**, is a **defective product** — for example a vape cartridge that won't fire or a malfunctioning device. A defective product may be exchanged **with no time limit**, provided the customer brings the **original packaging with a legible lot identification number** and the **purchase receipt**. Defective-return disputes, refunds, or any case that isn't a clear straightforward defective exchange are handed to a team member (escalation) — the agent never promises a refund or adjudicates a dispute itself. Cash-back refunds are not given; the remedy is an exchange for an equivalent product."

`[WA-LAW]` — the WAC cite is verbatim. This row is the grounding for the `faq` member's return answers AND the `escalation` member's defective-product path (P2 reads the same row).

### 8.3 Store facts (`StoreFact`) — all 3 stores + global + specials

**Per-store** (`[CONFIRMED]` from roadmap §10 / synthesis brief §2; Mt Vernon hours `[O-8 unconfirmed]`):

| store | kind | label | value | confirmed |
|---|---|---|---|---|
| yakima | address | Yakima address | "1315 N 1st St, Yakima, WA 98901" | True |
| yakima | phone | Yakima phone | "(509) 571-1106" | True |
| yakima | hours | Yakima hours | "9 AM–11 PM daily" | True |
| yakima | email | Yakima email | "happytimeyak509@gmail.com" | True |
| mount-vernon | address | Mt Vernon address | "200 Suzanne Ln, Mount Vernon, WA" | True |
| mount-vernon | phone | Mt Vernon phone | "(360) 488-2923" | True |
| mount-vernon | hours | Mt Vernon hours | "" (placeholder) | **False** → "call to confirm" (O-8) |
| pullman | address | Pullman address | "5602 WA-270, Pullman, WA" | True |
| pullman | phone | Pullman phone | "(509) 334-2788" | True |
| pullman | hours | Pullman hours | "9 AM–11 PM daily" *(seed if owner confirms; else `confirmed=False`)* | True/conditional |

> **O-8 (binding):** Mt Vernon's two site pages conflict (`/mount-vernon` 9a–10p vs `/contact` 9a–11p). **Do NOT seed a guessed Mt Vernon close time.** Seed `confirmed=False` with `value=""`; the `chunk_text()` emits "Mt Vernon hours: not confirmed — ask the caller to call the store to confirm." The agent says exactly that, never a guessed hour. Owner confirms → flip `confirmed=True` + set the real value, re-run `seed_kb`. Pullman hours: seed the real value if owner-confirmed; otherwise the same `confirmed=False` treatment.

**Global** (`store=""`, applies to all):

| kind | label | value |
|---|---|---|
| payment | Payment | "Cash and debit only; on-site ATM available." |
| pickup | Pickup | "Pickup only (no delivery, WA law); online orders ready in ~15 minutes." |
| email | Shared email | "happytimeyak509@gmail.com" |
| age | Age requirement | "21+ with a valid government-issued photo ID." |

**Weekly specials** (`store=""`, `kind=special`, one row each — so "what's the Wednesday deal" retrieves just that row):

| label | value |
|---|---|
| Flower Monday | "Flower Monday — 30% off flower." |
| Cyber Tuesday | "Cyber Tuesday — 30% off online orders." |
| Wax Wednesday | "Wax Wednesday — 25% off concentrates/wax." |
| Self-Care Thursday | "Self-Care Thursday — 25% off (self-care / wellness)." |
| Happy Friday | "Happy Friday — 30% off online orders." |

### 8.4 WA purchase limits (`StoreFact kind=limit` + `WeightTypeTaxonomy axis=limit`)

`[WA-LAW]` per WAC 314-55-095 / WSLCB (`_research-education-blogs.md` §10). Seeded **twice** — as `StoreFact kind=limit` (so a "limits" FAQ query hits them) AND as `WeightTypeTaxonomy axis=limit` (so a "flower limit" weights query hits them); both reference the same numbers (one source of truth in `seed.py`, two row kinds):

| term | value | notes |
|---|---|---|
| useable flower | 1 ounce (28 g) | "The WA per-visit flower cap." |
| concentrate | 7 grams | "Per visit." |
| solid edibles | 16 ounces | "Per visit (solid cannabis-infused edibles)." |
| liquid edibles | 72 ounces | "Per visit (liquid cannabis-infused edibles)." |

Plus the age/ID rule as the `age` `StoreFact` (§8.3) — "21+, valid government photo ID; purchases are tracked so limits can't be exceeded in a transaction." `[GENERAL]` DOH-Approved products map to budtender's `doh_only` filter — a `notes` line records this so the agent can say "we can filter to DOH-Compliant products if you'd like."

### 8.5 The FULL weights/types taxonomy (`WeightTypeTaxonomy`) — every row

`[GENERAL]` aligned with budtender `ranking.py` (`_GRAM_HINTS`/`_SUBTYPE_KEYWORDS`/`CATEGORY_BY_SLOTKEY`) — taxonomy parity (binding).

**A. `axis=weight` (flower/concentrate ladder):**

| term | value | synonyms | notes |
|---|---|---|---|
| half-gram | 0.5 g | ["0.5g","half g"] | "Common cart or single pre-roll size." |
| gram | 1 g | ["1g","a gram"] | "Standard cart size; a gram of flower." |
| two grams | 2 g | ["2g"] | |
| eighth | 3.5 g | ["1/8 oz","eighth oz","an eighth","eight-ball"] | "The default flower unit customers shop by." |
| four grams | 4 g | ["4g"] | "Occasional 4g eighth-plus deals." |
| quarter | 7 g | ["1/4 oz","quarter oz","a quarter"] | |
| eight grams | 8 g | ["8g"] | |
| ten grams | 10 g | ["10g"] | |
| half-ounce | 14 g | ["1/2 oz","half oz","half ounce"] | |
| ounce | 28 g | ["1 oz","an ounce","oz"] | "The WA flower purchase cap (1 oz = 28 g)." |

Quick-math note seeded as a `notes` line on `ounce`: "1 oz = 28 g · ½ oz = 14 g · ¼ oz = 7 g · ⅛ oz = 3.5 g."

**B. `axis=cart_size`:** `0.5 g` (["half gram cart"]), `1 g` (["full gram cart"]) — notes: "Most common cartridge sizes; disposables are all-in-one (battery + oil)."

**C. `axis=preroll`:** `single`, `5-pack` (["5pk","five pack"]), `10-pack` (["10pk","ten pack"]) — notes: "Sold by pack count; per-joint weight commonly 0.5 g or 1 g."

**D. `axis=edible_dose`:** `[SITE]`+`[GENERAL]`

| term | value | notes |
|---|---|---|
| microdose | 1–2.5 mg THC | "Functional, sub-intoxicating dose." |
| beginner start | 2.5 mg THC | "First-timer dose — a quarter of a 10 mg gummy." |
| standard piece | 5 mg or 10 mg THC | "Typical gummy strength." |
| WA max edible package | 10 × 10 mg = 100 mg THC | "Standard WA solid-edible pack." |
| onset | 30–90 min (beverages 15–30 min) | "Edibles onset slower than inhaled; beverages are the fastest edible." |
| peak / re-dose | peak ≈ 3 h | "Wait 2 hours before re-dosing — a hard rule, even if you don't feel it yet." |

**E. `axis=concentrate_subtype`** (parity with budtender `_SUBTYPE_KEYWORDS["concentrates"]`): `rosin`/`live rosin` ("solventless, premium"), `live resin`, `cured resin`, `RSO`/`FECO` ("full-extract, oral"), `distillate` ("high-THC, flavorless"), `diamonds`, `sauce`, `badder`/`budder`, `shatter`, `crumble`, `sugar`, `wax`, `bubble hash`/`temple ball`, `kief`. ~13 rows; `notes` carries the one-line description.

**F. `axis=flower_form`:** `whole-bud`, `smalls`/`popcorn` ("smaller buds, cheaper"), `shake` ("loose, cheapest"), `pre-roll` ("single or multi-pack"), `infused pre-roll` ("diamond / hash-hole / moon-rock"), `blunt`.

**G. `axis=strain_type`** (the house-rule rows — `[SITE]`): `indica`, `sativa`, `hybrid`, each `value=""` with `notes` carrying the house position: "Indica/sativa/hybrid is a general industry label — the terpene profile and your own physiology shape the experience more than the label. Never over-promise (e.g. 'indica = couch-lock'); ask about the desired effect and steer by terpene + reported effects." A 4th row `term="terpenes"` carries the cheat-sheet (myrcene/linalool → relaxed; limonene/pinene → uplifted; caryophyllene → calming; terpinolene → bright).

**H. `axis=ratio`** (`[SITE]`+`[GENERAL]`): `1:1` ("balanced CBD:THC — often feels less intoxicating; CBD softens THC"), `2:1`, `5:1`, `20:1` ("CBD-leaning, progressively less head-high"), `CBN` ("the 'sleepy' minor cannabinoid; pairs with THC for sleep").

### 8.6 Education (`EducationDoc`) — distilled rows (`provisional=True`)

One row per confirmed `happytimeweed.com/education/*` URL (`_research-education-blogs.md` Provenance table). Body = the distilled `[SITE]` content; `provisional=True` until the Vercel wall lifts for verbatim copy (re-run `seed_kb` then). `source_url` set.

| slug | title | topic | body (distilled from `_research-education-blogs.md`) |
|---|---|---|---|
| `edibles` | "Edibles guide" | edibles | §2: start at 2.5 mg (¼ of a 10 mg gummy); wait 2 h before re-dosing; onset 30–90 min, lasts 4–8 h, peak ≈3 h; empty stomach = faster/less predictable, with food = slower/gradual; if you took too much — stay calm, hydrate, rest, it passes, CBD can blunt it; formats: gummies (5/10 mg), chocolates, baked goods, mints (fast sublingual), beverages (15–30 min, fastest). |
| `microdosing` | "Microdosing guide" | microdosing | §3: sub-intoxicating 1–2.5 mg THC; start 2.5 mg, wait 2 h, peak ≈3 h; use cases — tolerance management, stepping down from heavy use, anxiety modulation (low THC+CBD), sleep (2–5 mg THC + low CBN); benefits compound over 2–4 weeks; don't re-dose early / stack with alcohol / start with high-THC. |
| `cannabis-strain-types` | "Strain types" | strains | §5: indica/sativa/hybrid is a general label; terpene profile + physiology matter more; ask desired effect, steer by terpene + reported effects; terpene→effect cheat-sheet (myrcene/linalool relaxed, limonene/pinene uplifted, caryophyllene calming, terpinolene bright). |
| `cannabis-storage-guide` | "Storage guide" | storage | §7: UV degrades THC + terpenes → store opaque/dark; cool, dark, airtight; flower ~59–63% RH; concentrates cold; carts upright; child-resistant packaging, locked from kids/pets. |
| `thc-cbd` | "THC vs CBD" | thc-cbd | §4: CBD is non-intoxicating, calming/anti-anxiety; 1:1 (5 mg CBD + 5 mg THC) often feels less intoxicating; common WA ratios 1:1/2:1/5:1/20:1; CBN = the sleepy minor cannabinoid. |

### 8.7 Blogs (`BlogDoc`) — distilled rows (`provisional=True`)

| slug | title | body (distilled) |
|---|---|---|
| `how-to-use-disposable-vape` | "How to use a disposable vape" | §6: live-resin carts/disposables track the strain's terpene profile; fast onset like flower, no smoke, discreet; all-in-one (battery + oil); beginner how-to (draw-activated, no buttons, store upright). |
| `best-dispensary-yakima-wa` | "Best dispensary in Yakima" | Brand/community post — family-owned, three WA stores, pickup via Dutchie; use for "are you local / what makes you different" questions. |
| `recreational-marijuana-yakima-wa` | "Recreational marijuana in Yakima" | Rec-cannabis-in-Yakima overview — 21+, pickup-only, WA limits; community/SEO framing. |

### 8.8 The persona `AgentPrompt` (`role="faq"` / entry_faq)

One row, `role="faq"`, `vapi_model="gpt-4.1-mini"`, `voice_id="a3520a8f-226a-428d-9fcd-b0a4711a6829"`, `tool_names=["faq_lookup"]`. Body (P5 finalizes the Koptza tone — `15` §3.1):

> "You are **Koptza**, the warm, friendly voice of **Happy Time Weed**, a family-owned Washington cannabis shop. Tone: welcoming, community-minded, no-pressure, conservative on dosing. Greet callers and confirm they are 21 or older with a spoken question — **never** say 'let me peek at your ID' (you're on the phone, you can't see it). **Answer ONLY from the `faq_lookup` tool** — every fact (hours, payment, pickup, returns, purchase limits, weights, doses, ratios, specials) comes from the knowledge base, not your own memory. If `faq_lookup` returns `grounded:false`, say you'll get a team member — **never invent** a number, hour, price, or dose. When you mention a price, it's **out-the-door** (what the customer pays). On dosing, stay conservative: start low, wait 2 hours, don't over-promise strain-type effects, and point to our education guides. Localize hours/address/phone to the caller's store (Yakima, Mt Vernon, or Pullman); if a store-specific fact isn't confirmed, say so and suggest they call the store."

`[SITE]` house behaviors (§8 of `_research-education-blogs.md`) — lead with the effect/occasion question, cite the source for health claims, respect WA limits + 21+, pickup-only per store — are embedded in the body. Numbers-Guard is explicit ("answer ONLY from `faq_lookup`").

---

## 9. Acceptance criteria (testable, concrete — lettered)

**A. Models + migration**
- A1. `kb/models.py` defines exactly the six voice KB models (`FAQEntry`, `PolicyDocument`, `StoreFact`, `EducationDoc`, `BlogDoc`, `WeightTypeTaxonomy`); a test asserts **exactly one** taxonomy model exists (no `WeightsTypesTaxonomy` duplicate — drift guard).
- A2. `makemigrations --check` exits 0 (the KB migration is committed); each model has its `unique_together`/`unique` natural key (idempotent-seed precondition).
- A3. Every text model exposes `chunk_text()` returning a non-empty string for a seeded row.

**B. Embeddings retrieval (gap G-6)**
- B1. `rank_faq("what time do you close","yakima")` returns the **Yakima-hours `StoreFact`** as the top row; `rank_faq("can I return a dead vape")` returns the **WAC-314-55-079 `PolicyDocument`**; `rank_faq("how many grams in an eighth")` returns the **`eighth` `WeightTypeTaxonomy`** row; `rank_faq("what's a microdose")` returns the **`microdose` `edible_dose`** row.
- B2. Store scoping: `rank_faq("hours","pullman")` does **not** return a Yakima or Mt Vernon hours row; a global row (payment/limits/taxonomy) returns for any store.
- B3. Content-hash invalidation: editing a KB row's text changes the `_corpus_vectors` cache key → the next `rank_faq` reflects the edit with **no redeploy** (assert the key differs + the returned text changes). (P0 acceptance C2.)
- B4. Degrade-safe: with `gemini.embed` mocked to raise, `rank_faq` falls back to keyword match and **still** returns the correct row for B1's queries.
- B5. `reindex()` returns the chunk count == the number of active KB rows; `python manage.py reindex_kb` prints it; `--mirror` also calls `vapi_files.mirror_all()`.

**C. `faq_lookup` read path**
- C1. `faq_lookup({"query":"do you take cards"}, ctx)` returns `{answer: <payment value>, grounded:true, sources:[…], store:"yakima"}` — the answer is the KB row's text (Numbers-Guard).
- C2. A query with no KB match → `{answer:null, grounded:false, fallback:"…team member…"}` — **no invented figure** (assert no number absent from every KB row appears in the answer).
- C3. The Mt Vernon hours query (unconfirmed, O-8) → the answer is the "call to confirm" text, **never a guessed close time**.
- C4. Leak-Guard: every `faq_lookup` response passes `assert_no_leak` (no `"cost"`/`"margin"` substring) — **non-negotiable gate** (ADR-008).

**D. Seed content (gap G-7)**
- D1. `manage.py seed_kb` is **idempotent** (run twice → no duplicate rows; `get_or_create` natural keys).
- D2. After seeding, **every §7-mapped row exists** (a parametrized existence test): the 8 FAQ Q&As, the WAC-314-55-079 `PolicyDocument`, the ~14 store-facts (Yakima/Mt-Vernon-stub/Pullman/global), the 5 specials, the 4 WA-limit rows, the FULL `WeightTypeTaxonomy` (every axis: weight 10 / cart_size 2 / preroll 3 / edible_dose 6 / concentrate_subtype ~13 / flower_form ~6 / strain_type 4 / ratio 5 / limit 4), the 5 education rows, the 3 blog rows, the `entry_faq` `AgentPrompt`.
- D3. **O-8 honored:** the Mt Vernon hours `StoreFact` has `confirmed=False` and `value==""`; its `chunk_text()` emits "call to confirm." Yakima hours seed real.
- D4. **WA-law accuracy:** the return-policy body contains the literal string `"WAC 314-55-079"`; the limit rows are exactly 1 oz / 7 g / 16 oz / 72 oz.
- D5. **Taxonomy parity:** every `axis=concentrate_subtype` `term` is present in budtender `ranking._SUBTYPE_KEYWORDS["concentrates"]` (a parity test reads the budtender constant); the `weight`-axis terms match `_GRAM_HINTS`.

**E. Vapi Files mirror**
- E1. `vapi_files.mirror_all()` (mocked Files API) renders ≤300KB files (assert each `_render_file(kind)` ≤ 300·1024 bytes; split if exceeded), uploads them find-by-name-then-replace (a re-mirror sends **zero** duplicate-create calls), attaches/updates a Query Tool, returns `{files, tool_id}`.
- E2. With `VAPI_PRIVATE_KEY` unset, `mirror_all()` returns `{skipped:"not configured"}` and **`faq_lookup` still answers** from `kb/` (the mirror is a fallback, not the source of truth).

**F. Grounded round-trip (the deliverable)**
- F1. A real inbound call asking **hours / payment / a weight / the flower limit / returns** → each answered from the seeded KB content (no hallucinated facts); a **sampled FAQ question, a sampled weight question ("how many grams in a quarter?"), and a sampled limit question ("how much flower can I buy?")** are all answered grounded — the answer text matches a seeded row. (Roadmap §5 P0 + this spec's acceptance.)
- F2. Embeddings retrieval returns the right chunk for each sampled query (B1) — proven on the live call (the spoken answer == the top `rank_faq` chunk).
- F3. Editing one FAQ answer + reindex → the next call speaks the new answer (no redeploy).

**G. Hygiene**
- G1. `ruff check` + `ruff format --check` clean; `python manage.py check` clean; `makemigrations --check` exit 0; targeted `pytest` green. **Paste all four outputs** (`03-CONVENTIONS.md` §1.3).

---

## 10. Test plan

Mirrors `03-CONVENTIONS.md` §5 (Unit · Contract · Provisioning · Manual). The **Leak-Guard** and the **grounded-no-invented-figure** tests are mandatory gates (this doc touches a tool path + serialized output).

### 10.1 Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network)
- `tests/test_kb_models.py` — A1/A2/A3: the six models + the single-taxonomy drift guard; `chunk_text()` non-empty per seeded row.
- `tests/test_semantic_faq.py` — B1/B2/B3/B4: `rank_faq` top-chunk correctness across all six models; store scoping; content-hash cache invalidation on edit; Gemini-down keyword fallback (mock `gemini.embed` to raise). **Test-data discipline:** expected top rows hand-authored, not generated by the code under test.
- `tests/test_faq_tool.py` — C1/C2/C3: grounded answer == KB row text; no-match → `grounded:false` + human-offer + no invented figure; Mt Vernon "call to confirm".
- `tests/test_seed.py` — D1/D2/D3/D4: idempotent seed; every §7 row exists (parametrized); Mt Vernon `confirmed=False`; the `"WAC 314-55-079"` literal + the exact limits present.
- `tests/test_taxonomy_parity.py` — D5: the concentrate-subtype + weight terms match the budtender `ranking.py` constants (imports the budtender constant or a recorded copy).
- `tests/test_reindex.py` — B5: `reindex()` chunk count; the command prints it.

### 10.2 Contract (`pytest -m integration`, Vapi Files API mocked, Gemini stubbed/recorded)
- `tests/test_leak_guard_kb.py` (**mandatory**) — no `"cost"`/`"margin"` substring in any `faq_lookup` response **or** in any rendered `vapi_files` markdown file (ADR-008 / C4).
- `tests/test_vapi_files_mirror.py` — E1/E2: `mirror_all` renders ≤300KB files, find-by-name-then-replace (zero duplicate creates on re-mirror), returns `{files,tool_id}`; degrades to `{skipped}` with no key; `faq_lookup` still answers from `kb/`.
- `tests/test_grounded_no_invent.py` (**mandatory**) — a fabricated-fact query ("how much is a gram of X-brand") → `grounded:false`, the answer contains **no number** not present in a KB row (Numbers-Guard).

### 10.3 Provisioning (with `seed_kb` + the live mirror)
- `python manage.py seed_kb --reindex` against a sandbox Vapi key → seeds the rows, rebuilds the cosine cache, mirrors the files, attaches the Query Tool; a re-run is drift-free (no duplicate rows, no duplicate files). Paste the row counts + the mirror result.

### 10.4 Manual call script (the definition of done — paste evidence)
Dial the provisioned number (O-4 placeholder), run, paste transcript + the `rank_faq` top-chunk for each:
1. **Sampled FAQ:** "do you take cards?" → cash+debit+ATM (the `payment` row).
2. **Sampled weights:** "how many grams in a quarter?" → 7 g (the `quarter` taxonomy row); "what's a microdose?" → 1–2.5 mg (the `microdose` row).
3. **Sampled limit:** "how much flower can I buy in one visit?" → 1 ounce (the `useable flower` limit row).
4. **Returns/WAC:** "can I return a dead vape cart?" → the WAC-314-55-079 defective exception (original packaging + legible lot ID + receipt; a human handles disputes).
5. **Store localization:** "what time do you close in Yakima?" → 9 PM/11 PM seeded value; "…in Mt Vernon?" → "call to confirm" (O-8), never a guessed time.
6. **Live edit:** change the `payment` answer in admin → re-call → the new answer is spoken (no redeploy).

**Coverage:** ~90% diff coverage on `kb/semantic.py` (`rank_faq`/`reindex`/`_keyword_fallback`), `kb/seed.py`, `kb/vapi_files.py`, `voice/tools/faq.py`. Never lower a ratchet.

---

## 11. Cross-check vs P0 (no-contradiction proof)

| Item | P0 (`10-…`) | This spec (`22-…`) | Consistent? |
|---|---|---|---|
| Model set | §3.4: `FAQEntry`/`PolicyDocument`/`StoreFact`/`EducationDoc`/`BlogDoc`/`WeightTypeTaxonomy` | §3 same six | ✅ |
| Taxonomy name | §3.4/§4.7 `WeightTypeTaxonomy` | §3.6 `WeightTypeTaxonomy` (synonym `WeightsTypesTaxonomy` prose-only) | ✅ (drift guard A1) |
| Embeddings | §5: Gemini 768-dim Matryoshka + cached cosine + keyword fallback + pgvector seam | §4 same, deeper | ✅ |
| `rank_faq`/`reindex` | §3.4 names them | §4.3 specifies signatures | ✅ |
| `faq_lookup` result | §4.3 envelope `{answer,grounded,sources,store}` | §5 the `result` inner object (same keys) | ✅ |
| Seed map | §4.7 table-summary, defers full content | §7 + §8 full content | ✅ (this is the deferred detail) |
| Mt Vernon O-8 | §4.7 `confirmed=False` stub | §8.3 same | ✅ |
| WAC cite | §4.7 `PolicyDocument` WAC 314-55-079 | §8.2 literal body | ✅ |
| Vapi Files | §3.4 `vapi_files.mirror_all` ≤300KB + Query Tool | §6 same | ✅ |
| Persona | §4.7 `entry_faq` `role="faq"` | §8.8 same row | ✅ |
| Leak/Numbers-Guard | §1.3 / E1/E2 | §1.3 / §9 C4 + grounded-no-invent | ✅ |

**Where this doc is authoritative:** KB content (§8), the seed row map (§7), the embeddings pipeline detail (§4), the Files-mirror render (§6). **Where P0 is authoritative:** the webhook envelope, provisioning, the migration commit. No field shape, name, or number conflicts.

---

## 12. Risks / open questions

| Risk / open item | Impact | Mitigation / disposition |
|---|---|---|
| **Verbatim house education copy blocked by the Vercel wall** (`_research-education-blogs.md` Provenance) | Education rows are `[SITE]`-distilled, not verbatim. | Seed `EducationDoc`/`BlogDoc` `provisional=True`; `seed.py` re-runnable so a later verbatim paste (browser/computer-use MCP) updates the rows via `get_or_create` + an `update` on the body. Does NOT block the FAQ deliverable — hours/payment/returns/limits/weights are confirmed facts/WA-law. |
| **O-8 Mt Vernon hours conflict** | Wrong hours spoken. | `confirmed=False` "call to confirm" (§8.3); never seed a guessed close time. Owner confirms → flip + re-seed. A test gates `confirmed=False` (D3). |
| **Taxonomy drift from budtender** (`15` §3.2 parity) | The agent speaks a size/subtype the recommender can't fulfill. | The parity test (D5) reads budtender's `ranking.py` constants; the taxonomy `term`s stay identical. If budtender adds a subtype, re-seed. |
| **Gemini/Vertex auth absent in the build env** (the marketing_dashboard had a 403'd Generative Language API) | Cosine retrieval disabled. | `rank_faq` keyword fallback (§4.4) still returns the right row (B4); seeding needs no Gemini at all (rows are plain text). `healthz` reports `gemini: not ready`. |
| **300KB Vapi Files cap** | A large education dump could overflow one file. | `_render_file` asserts ≤300KB and splits (`education-2.md`); the cap is tested (E1). |
| **Numbers-Guard relies on the persona prompt + the KB** | A model could still try to paraphrase a number. | Two layers: the KB holds every figure (so the model has a real row to read) AND the prompt forbids inventing; the grounded-no-invent test (10.2) gates the surface. A `grounded:false` answer always offers a human. |
| **Single-number multi-store routing** (O-4) | The agent may not know the caller's store for a store-specific fact. | `HHT_DEFAULT_STORE=yakima` default; a store name in the query overrides; if a store-specific fact is asked with no store known, the answer asks which store (never guesses). Global facts (payment/limits/taxonomy) answer regardless. |
| **`provisional` education content quality** | A distilled row could read thin. | `provisional=True` flags it for the verbatim pass; the dashboard KB-source manager (P4) surfaces provisional rows for the owner to upgrade. |

---

## 13. Definition of done (this spec's implementation, inside P0)

- The six KB models, `kb/semantic.py` (`rank_faq`/`reindex`/keyword fallback/pgvector-seam docstring), `kb/vapi_files.py`, the `reindex_kb` command, `kb/seed.py` (every §8 row), and `voice/tools/faq.py` are built (by P0) and green.
- **Every §9 acceptance criterion passes with pasted output** (`ruff check`, `ruff format --check`, `pytest`, `manage.py check`, `makemigrations --check`).
- A real inbound call answers a **sampled FAQ, a sampled weights question, and a sampled WA-limit question** grounded in the seeded KB; embeddings retrieval returns the right chunk for each (F1/F2).
- A KB edit is reflected on the next call with no redeploy (F3); the Vapi mirror is a re-pushable fallback (E1/E2).
- Docs updated in the SAME change (`03-CONVENTIONS.md` §6): this spec referenced from `10-P0-CHASSIS-FAQ.md` §3.4/§4.7/§11 (the KB content + embeddings detail it defers here); the `2X-SPEC` index in `00-MASTER-ROADMAP.md` updated to list `22-SPEC-kb-seed.md`; gap **G-7 marked resolved** in `99-PLAN-REVIEW.md` (the section→model→rows map is §7).
