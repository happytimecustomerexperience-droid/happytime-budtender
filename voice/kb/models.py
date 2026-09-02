"""Knowledge-base models — the voice KB plane (22-SPEC-kb-seed.md §3).

Six voice KB models the FAQ agent quotes from (Numbers-Guard: every figure lives in a
row, never in the LLM): FAQEntry, PolicyDocument, StoreFact, EducationDoc, BlogDoc,
WeightTypeTaxonomy. Plus the editable persona/flow rows owned conceptually by P0
(AgentPrompt ~swedish-bot L226, FlowConfig ~L276), forked + slimmed to the voice domain.

Ports from swedish-bot/kb/models.py: FAQEntry(+FAQEntryText, collapsed to a flat English
model), PolicyDocument, SiteFAQ (the StoreFact shape inspiration), GenericGuide (the
EducationDoc/BlogDoc shape), AgentPrompt, FlowConfig.

Embedding seam (ADR-013 / 22-SPEC §4.5): every text model exposes ``chunk_text()`` — the
single (id, text) the embedder/keyword-matcher sees — and carries a nullable ``embedding``
JSON column. Today retrieval is the cached in-memory cosine in ``kb/semantic.py`` (the corpus
is dozens of rows; the column stays null). The column IS the pgvector swap-seam: past a few
thousand rows, swap it for an ``embedding vector(768)`` column + an HNSW index +
``ORDER BY embedding <=> q LIMIT k`` — keeping the same ``rank_faq(query, store, top_k)``
signature so no caller changes.
"""

from __future__ import annotations

import datetime

from django.db import models


class SourceSyncMixin(models.Model):
    """Metadata for KB rows imported from happytimeweed.com."""

    source_hash = models.CharField(max_length=64, blank=True)
    last_scraped_at = models.DateTimeField(null=True, blank=True)
    scrape_status = models.CharField(max_length=16, blank=True)
    scrape_error = models.CharField(max_length=300, blank=True)

    class Meta:
        abstract = True

# Agent roles match Vapi member roles (03-CONVENTIONS.md §1.4). entry_faq's row is
# role="faq" so the later faq split is a rename, not a new row (10-P0 §6.4).
AGENT_ROLE_CHOICES = [
    ("entry_router", "Entry router"),
    ("budtender", "Budtender"),
    ("faq", "FAQ"),
    ("vendor", "Vendor"),
    ("escalation", "Escalation"),
]


# ── The six voice KB text models ──────────────────────────────────────────────


class FAQEntry(SourceSyncMixin, models.Model):
    """One row per spoken FAQ (hours/payment/pickup/returns/limits/specials/general/age).
    The single most-hit KB model. Forked + flattened from swedish-bot FAQEntry+FAQEntryText
    (HVAC-category-scoped + i18n-split) into a flat single-row English model."""

    key = models.SlugField(max_length=64, unique=True)  # natural key for idempotent seed
    question = models.TextField()  # canonical spoken question
    answer = models.TextField()  # grounded answer — Numbers-Guard: every figure is here
    paraphrases = models.JSONField(default=list, blank=True)  # alt phrasings widen embedding recall
    store = models.CharField(
        max_length=32, blank=True
    )  # "" = global; else yakima|mount-vernon|pullman
    topic = models.CharField(
        max_length=32, blank=True
    )  # hours|payment|pickup|returns|limits|specials|general|age
    source_url = models.URLField(blank=True)
    weight = models.IntegerField(default=100)  # retrieval priority tiebreak (higher first)
    embedding = models.JSONField(null=True, blank=True)  # pgvector swap-seam (null today)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-weight", "key"]

    def chunk_text(self) -> str:  # the (id, text) the embedder/keyword matcher sees
        extra = (" / ".join(self.paraphrases)) if self.paraphrases else ""
        return f"Q: {self.question}\n{extra}\nA: {self.answer}".strip()

    def __str__(self):
        return f"{self.key} ({self.topic})"


class PolicyCategory(models.Model):
    """An owner-defined policy category (return policy, delivery, ID rules, product use,
    anything) — replaces the old hardcoded ``PolicyDocument.POLICY_KINDS`` enum so the owner
    can add a new category from the dashboard with no code change. ``slug`` is the stable
    retrieval key (never rename in place — it is what ``PolicyDocument.category`` and any
    external reference key off). ``topic`` is optional: when it matches one of chat.py's
    ``_faq_topic`` values ("return_policy"/"specials"/"hours_location"), rows under this
    category opt into topic-scoped retrieval (kb/semantic.py); "" (the default) means the
    category is unconstrained and only reachable by unconstrained retrieval, exactly like
    every other KB row type."""

    slug = models.SlugField(max_length=64, unique=True)
    label = models.CharField(max_length=120)
    description = models.TextField(blank=True)  # what this category is for (owner-facing)
    topic = models.CharField(max_length=32, blank=True)  # "" | return_policy | specials | hours_location
    weight = models.IntegerField(default=120)  # default weight for documents seeded under it
    is_active = models.BooleanField(default=True)
    order = models.IntegerField(default=0)  # dashboard display order

    class Meta:
        ordering = ["order", "label"]
        verbose_name_plural = "Policy categories"

    def __str__(self):
        return self.label


class PolicyDocument(SourceSyncMixin, models.Model):
    """Company-level policy bodies the FAQ cites — return policy, product use, delivery, ID
    rules, or any other category the owner adds via ``PolicyCategory``. Forked from
    swedish-bot PolicyDocument (PDF-backed Swedish terms) into a body-text model with an
    optional PDF attachment (ingestible via kb/ingest.py). MANY documents can live under one
    category (e.g. several delivery-policy pages) — the old one-per-``kind`` unique
    constraint is gone; ``category`` is a plain FK."""

    category = models.ForeignKey(
        PolicyCategory, on_delete=models.PROTECT, related_name="documents"
    )
    title = models.CharField(max_length=200)
    body = models.TextField()  # the spoken-grounding body (WAC cite lives here)
    citation = models.CharField(
        max_length=64, blank=True
    )  # "WAC 314-55-079" — surfaced as a source
    source_url = models.URLField(blank=True)
    pdf = models.FileField(
        upload_to="policy/", blank=True, null=True
    )  # optional; ingested via kb/ingest
    sha256 = models.CharField(max_length=64, blank=True)
    weight = models.IntegerField(default=120)  # policy outranks generic FAQ on a returns query
    embedding = models.JSONField(null=True, blank=True)  # pgvector swap-seam
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def chunk_text(self) -> str:
        cite = f" ({self.citation})" if self.citation else ""
        return f"{self.title}{cite}: {self.body}"

    def __str__(self):
        return f"{self.title} [{self.category.slug}]"


class StoreFactQuerySet(models.QuerySet):
    """``current()`` is the one place the validity window is expressed. Every path that RETRIEVES
    or SPEAKS a store fact goes through it (``kb.semantic._build_corpus``, ``kb.vapi_files``, the
    ``faq_lookup`` specials answer, the webhook's hours lookup); the dashboard editor deliberately
    does NOT, so the owner can still see and edit a deal whose window has closed."""

    def current(self, today=None):
        today = today or datetime.date.today()
        return self.filter(
            models.Q(valid_from__isnull=True) | models.Q(valid_from__lte=today),
            models.Q(valid_to__isnull=True) | models.Q(valid_to__gte=today),
        )


class StoreFact(SourceSyncMixin, models.Model):
    """Per-store + global operational facts the agent localizes — address, phone, hours,
    email, payment, pickup, specials, limits, age. The ``confirmed`` flag carries O-8
    (Mt Vernon hours stay unspoken until confirmed). Shaped from swedish-bot SiteFAQ."""

    KINDS = [
        ("address", "Address"),
        ("phone", "Phone"),
        ("hours", "Hours"),
        ("email", "Email"),
        ("payment", "Payment"),
        ("pickup", "Pickup"),
        ("special", "Weekly special"),
        ("limit", "WA purchase limit"),
        ("age", "Age requirement"),
    ]
    store = models.CharField(max_length=32, blank=True)  # "" = applies to all stores
    kind = models.CharField(max_length=16, choices=KINDS)
    label = models.CharField(max_length=120)  # human label ("Yakima hours")
    value = models.TextField(blank=True)  # the spoken value ("9 AM–11 PM daily")
    source_url = models.URLField(blank=True)
    confirmed = models.BooleanField(default=True)  # O-8: False => "call to confirm", never a fact
    # Validity window (2026-09-01). A monthly deal used to be a plain row with its dates baked
    # into the prose ("30% off all flower — July 1–31"), so it kept being read out to callers in
    # September: the KB had no way to know a row had stopped being true. Both ends are nullable
    # and both default to open, so every row that is not a dated promotion behaves exactly as
    # before — an address does not expire.
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    weight = models.IntegerField(default=110)
    embedding = models.JSONField(null=True, blank=True)  # pgvector swap-seam
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = StoreFactQuerySet.as_manager()

    class Meta:
        unique_together = [("store", "kind", "label")]  # natural key for idempotent seed
        ordering = ["store", "kind", "label"]

    def is_current(self, today=None) -> bool:
        """Whether this row is inside its validity window today. An absent bound is open."""
        today = today or datetime.date.today()
        if self.valid_from and today < self.valid_from:
            return False
        return not (self.valid_to and today > self.valid_to)

    def chunk_text(self) -> str:
        # Fail closed: an out-of-window row contributes NO text, so any retrieval path that
        # forgets to filter (``StoreFactQuerySet.current``) still cannot speak a stale deal.
        # ``kb.semantic._build_corpus`` drops empty chunks.
        if not self.is_current():
            return ""
        scope = f"{self.store} " if self.store else ""
        if not self.confirmed:
            return (
                f"{scope}{self.label}: not confirmed — ask the caller to call the store to confirm."
            )
        return f"{scope}{self.label}: {self.value}"

    def __str__(self):
        return f"{self.store or 'global'}/{self.kind}/{self.label}"


class EducationDoc(SourceSyncMixin, models.Model):
    """One row per happytimeweed.com/education/* page (edibles, microdosing, strain types,
    storage, THC/CBD) — the longer-form teaching content. An education analogue of
    swedish-bot GenericGuide. provisional=True until verbatim house copy lands (Vercel wall)."""

    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=200)
    topic = models.CharField(
        max_length=48
    )  # edibles|microdosing|strains|storage|thc-cbd|concentrates
    body = models.TextField()  # distilled [SITE] content (conservative dosing framing)
    source_url = models.URLField(blank=True)
    provisional = models.BooleanField(default=True)  # True until verbatim house copy lands
    weight = models.IntegerField(default=80)  # education ranks below operational FAQ
    embedding = models.JSONField(null=True, blank=True)  # pgvector swap-seam
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["topic", "slug"]

    def chunk_text(self) -> str:
        return f"[{self.topic}] {self.title}: {self.body}"

    def __str__(self):
        return f"{self.slug} [{self.topic}]"


class BlogDoc(SourceSyncMixin, models.Model):
    """One row per happytimeweed.com/blog/* post (disposable-vape how-to, Yakima dispensary
    SEO posts) — lighter than education. Same shape as EducationDoc minus topic."""

    slug = models.SlugField(max_length=160, unique=True)
    title = models.CharField(max_length=200)
    body = models.TextField()  # distilled post content
    source_url = models.URLField(blank=True)
    provisional = models.BooleanField(default=True)
    weight = models.IntegerField(default=60)  # blog ranks lowest (least authoritative for facts)
    embedding = models.JSONField(null=True, blank=True)  # pgvector swap-seam
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def chunk_text(self) -> str:
        return f"[blog] {self.title}: {self.body}"

    def __str__(self):
        return self.slug


class WeightTypeTaxonomy(models.Model):
    """The canonical, structured weights + types + dose + ratio table — the single source
    the agent quotes for "how many grams in an eighth", "what's a microdose", cart sizes,
    purchase limits, "what's solventless". One row per (axis, term).

    Taxonomy parity (binding, 22-SPEC §3.6 / 15 §3.2): the axis+term vocabulary stays
    identical to budtender ranking.py's CATEGORY_BY_SLOTKEY / _SUBTYPE_KEYWORDS / _GRAM_HINTS.

    Canonical class name is WeightTypeTaxonomy (P0). "WeightsTypesTaxonomy" is a prose
    synonym only — do NOT create two models (a unit test asserts exactly one exists)."""

    AXES = [
        ("weight", "Flower/concentrate weight"),
        ("cart_size", "Cartridge size"),
        ("preroll", "Pre-roll format"),
        ("edible_dose", "Edible dosing"),
        ("concentrate_subtype", "Concentrate subtype"),
        ("flower_form", "Flower form"),
        ("strain_type", "Strain type"),
        ("ratio", "THC:CBD ratio"),
        ("limit", "WA purchase limit"),
    ]
    axis = models.CharField(max_length=32, choices=AXES)
    term = models.CharField(max_length=64)  # "eighth" / "microdose" / "live resin"
    value = models.CharField(
        max_length=120, blank=True
    )  # "3.5 g" / "1–2.5 mg THC" / "" descriptive
    notes = models.TextField(blank=True)  # "the default flower unit customers shop by"
    synonyms = models.JSONField(default=list, blank=True)  # ["1/8 oz","eight-ball"] widen recall
    weight = models.IntegerField(default=90)
    embedding = models.JSONField(null=True, blank=True)  # pgvector swap-seam
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("axis", "term")]  # natural key for idempotent seed
        ordering = ["axis", "id"]
        verbose_name_plural = "Weight/type taxonomy"

    def chunk_text(self) -> str:
        syn = (" (also: " + ", ".join(self.synonyms) + ")") if self.synonyms else ""
        val = f" = {self.value}" if self.value else ""
        return f"[{self.axis}] {self.term}{val}{syn}. {self.notes}".strip()

    def __str__(self):
        return f"{self.axis}/{self.term}"


# ── Persona / flow rows (forked from swedish-bot; seeded here, owned by P0) ────


class AgentPrompt(models.Model):
    """The editable system-prompt + Vapi config per agent role. One source of truth for the
    persona body, the model id, the voice, and the bound tools (read fresh → a save is live,
    no redeploy). Forked from swedish-bot AgentPrompt (~L226) + the P4 voice fields
    (14-P4 §4.1) added now so no later migration churn."""

    role = models.CharField(max_length=32, choices=AGENT_ROLE_CHOICES, unique=True)
    body = models.TextField()
    # Vapi surface (the P4 voice fields, added up front).
    # P6: model + voice are now per-row + provider-aware (the dashboard edit reaches Vapi). The
    # provider strings are Vapi's: model_provider ∈ {google, openai, anthropic, …}; voice_provider
    # ∈ {cartesia, 11labs, …}. provision.build_assistant_payload reads these (constant fallback).
    model_provider = models.CharField(max_length=32, blank=True)  # "google" → Gemini (ADR-024)
    vapi_model = models.CharField(max_length=64, blank=True)  # "gemini-2.5-flash" (ADR-024)
    voice_provider = models.CharField(max_length=32, blank=True)  # "cartesia" | "11labs"
    voice_id = models.CharField(max_length=64, blank=True)  # provider voice id
    # ElevenLabs (and any future provider) per-voice knobs: model, stability, similarityBoost,
    # style, useSpeakerBoost, optimizeStreamingLatency. One JSON field over N columns (ponytail).
    voice_settings = models.JSONField(default=dict, blank=True)
    tool_names = models.JSONField(default=list, blank=True)  # ["faq_lookup"] — bound custom tools
    vapi_assistant_id = models.CharField(max_length=64, blank=True)  # written back by provisioner
    transfer_number_key = models.CharField(max_length=32, blank=True)  # YAKIMA|MTVERNON|PULLMAN
    temperature = models.FloatField(null=True, blank=True)
    max_output_tokens = models.IntegerField(null=True, blank=True)
    prompt_version = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
    # Publish-to-Vapi bookkeeping (14-P4 §4.1) — the zero-drift oracle for re-publish.
    last_published_at = models.DateTimeField(null=True, blank=True)
    last_publish_hash = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.role} (v{self.prompt_version}, {self.vapi_model})"

    def voice_settings_json(self) -> str:
        """Pretty JSON of the voice knobs for the dashboard textarea (empty when unset). A method so
        every card render path (list / inline-save / full editor) shows it without threading ctx."""
        import json

        return json.dumps(self.voice_settings, indent=2) if self.voice_settings else ""


class FlowConfig(models.Model):
    """The editable Squad flow (the canvas), stored as one JSON graph (singleton). One row,
    one JSON field — no per-node/edge tables. ``agent`` steps reference an AgentPrompt by
    role, so the prompt/model stays the single source of truth. The hardcoded safety
    guardrails live in code (voice/guardrails.py) and are NOT configurable here.
    Forked from swedish-bot FlowConfig (~L276)."""

    graph = models.JSONField(default=dict, help_text="{nodes:[...], edges:[...]}")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        n = len((self.graph or {}).get("nodes", []))
        return f"FlowConfig(#{self.pk}, {n} steps)"


class SiteScrapeRun(models.Model):
    """Audit record for one scrape/apply/publish attempt."""

    STATUS_CHOICES = [
        ("running", "Running"),
        ("applied", "Applied"),
        ("blocked", "Blocked"),
        ("failed", "Failed"),
    ]
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="running")
    pages = models.JSONField(default=list, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)
    publish_results = models.JSONField(default=list, blank=True)
    summary = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"site scrape {self.status} at {self.started_at:%Y-%m-%d %H:%M}"
