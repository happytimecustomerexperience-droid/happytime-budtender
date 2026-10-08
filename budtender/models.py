"""
Data model for the self-contained budtender service.

Sensitive columns (cost, margin) live ONLY on Product and are never exposed by
any serializer — see serializers.py. Customer profiles are keyed by phone.
"""
from django.db import models

STORES = (("yakima", "Yakima"), ("mount-vernon", "Mount Vernon"), ("pullman", "Pullman"))


def _phone_cart_token() -> str:
    import secrets

    return "pc-" + secrets.token_urlsafe(18)


class Product(models.Model):
    """One in-stock SKU at one store, synced from Dutchie."""

    sku = models.CharField(max_length=64, db_index=True)
    product_id = models.CharField(max_length=64, blank=True, db_index=True)  # Dutchie productId — join key for transactions
    location_slug = models.CharField(max_length=32, choices=STORES, db_index=True)
    slug = models.SlugField(max_length=200, blank=True)  # for /catalog/product/<slug> + dtche[product]
    name = models.CharField(max_length=255)
    brand = models.CharField(max_length=128, blank=True)
    category = models.CharField(max_length=64, blank=True)  # catalog slug: flower, edibles, ...
    strain = models.CharField(max_length=128, blank=True)
    strain_type = models.CharField(max_length=16, blank=True)  # indica|sativa|hybrid|cbd
    thc_percent = models.FloatField(null=True, blank=True)
    dominant_terpene = models.CharField(max_length=64, blank=True)
    effects = models.JSONField(default=list, blank=True)
    flavors = models.JSONField(default=list, blank=True)

    price = models.DecimalField(max_digits=8, decimal_places=2, default=0)  # sell price
    price_was = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    # SERVER-ONLY — never serialized to the client.
    cost = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    margin = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    quantity_on_hand = models.IntegerField(default=0)
    availability = models.BooleanField(default=True)
    image_url = models.URLField(blank=True)
    # Size matching: real weight in grams (flower/concentrate/cart) and dose in
    # mg (edibles/tinctures), pulled from Dutchie's unitWeight / effectivePotencyMg.
    unit_weight = models.FloatField(null=True, blank=True)
    potency_mg = models.FloatField(null=True, blank=True)
    # Lab: the batch on the sales floor (most floor stock) and its COA when the POS
    # carries one. Without one, the backoffice lab result cached by new_drops
    # (keyed on batch_id) supplies it — see serializers.public_product.
    batch_id = models.CharField(max_length=32, blank=True)
    coa_url = models.URLField(max_length=500, blank=True)

    # ── Merchandising classification (server-only; see subsystem-1 spec) ──
    # `margin` above is the gross profit $ (price − cost). These add the
    # margin %, sales velocity, peer-relative z-scores and the strategy bucket.
    BUCKETS = (("core", "Core"), ("traffic", "Traffic driver"), ("profit", "Profit driver"))
    subcategory = models.CharField(max_length=16, blank=True, db_index=True)  # 28g, 1g, 10mg…
    margin_pct = models.FloatField(default=0)            # gross_profit / price
    velocity = models.FloatField(default=0)              # units sold per day (trailing)
    margin_z = models.FloatField(default=0)              # z within (category×subcategory)
    price_z = models.FloatField(default=0)
    bucket = models.CharField(max_length=8, choices=BUCKETS, default="core", db_index=True)
    bucket_source = models.CharField(max_length=8, default="auto")  # auto | manual
    classified_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("location_slug", "sku")
        indexes = [models.Index(fields=["location_slug", "category", "availability"])]

    def __str__(self) -> str:
        return f"{self.name} @ {self.location_slug}"


class BatchLab(models.Model):
    """One batch's lab report, kept durably — a batch's lab result never changes.

    Written ONLY by budtender.lab_enrich (a paced job, never a request). `data` is the Contract-A
    `lab` dict without its `profile` (that is derived from the numbers at read time). Status:
      ok   — Dutchie answered with a lab report
      none — Dutchie answered and there is no lab data; asked again after 7 days
    Unreachable / rate-limited / errored writes NO row: empty is not the same as unknown.
    """
    batch_id = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=8, choices=(("ok", "ok"), ("none", "none")))
    data = models.JSONField(default=dict, blank=True)
    checked_at = models.DateTimeField(db_index=True)

    def __str__(self) -> str:
        return f"BatchLab({self.batch_id} {self.status})"


class ProductDetail(models.Model):
    """The allowlisted `info` for one product (Dutchie's product-master record, boiled down by
    budtender.product_detail). Unlike a batch lab this is MUTABLE (tags, ingredients, category),
    so it is refreshed after 7 days. Written only by budtender.lab_enrich, never by a request.
      ok   — Dutchie answered and the record has something to show (`data` is the info)
      none — Dutchie answered and nothing customer-facing is filled in (`data` is {})
    Unreachable / empty / unstructured answers write NO row.
    """
    product_id = models.CharField(max_length=64, unique=True)  # Dutchie productId == Product.product_id
    status = models.CharField(max_length=8, choices=(("ok", "ok"), ("none", "none")))
    data = models.JSONField(default=dict, blank=True)
    checked_at = models.DateTimeField(db_index=True)

    def __str__(self) -> str:
        return f"ProductDetail({self.product_id} {self.status})"


class SyncState(models.Model):
    """When each store's inventory was last successfully refreshed from Dutchie.
    Suggestions are only ever served against in-stock products; this record lets a
    staleness guard force a fresh pull if the inventory is older than 24h, so we
    never recommend something that has since sold out."""
    location_slug = models.CharField(max_length=32, unique=True, db_index=True, choices=STORES)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    item_count = models.IntegerField(default=0)  # in-stock SKUs in the last pull
    # Transaction-ingest watermark: the max transactionDate folded into customer history so far.
    # The recurring sync folds transactions newer than this, PLUS any at exactly this timestamp whose
    # id isn't in last_tx_ids — so same-second sales at the boundary are neither dropped nor
    # double-counted (lossless + exactly-once). null = no history yet → next sync backfills.
    last_tx_at = models.DateTimeField(null=True, blank=True)
    last_tx_ids = models.JSONField(default=list, blank=True)  # tx ids folded AT exactly last_tx_at
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"SyncState({self.location_slug} @ {self.last_synced_at})"


class CustomerProfile(models.Model):
    phone = models.CharField(max_length=20, unique=True, db_index=True)  # E.164
    name = models.CharField(max_length=120, blank=True, db_index=True)  # from Dutchie (staff browse)
    total_orders = models.IntegerField(default=0)
    last_purchase_at = models.DateTimeField(null=True, blank=True)

    brand_affinity = models.JSONField(default=dict, blank=True)
    category_affinity = models.JSONField(default=dict, blank=True)
    strain_type_affinity = models.JSONField(default=dict, blank=True)
    flavor_affinity = models.JSONField(default=dict, blank=True)
    terpene_affinity = models.JSONField(default=dict, blank=True)
    subcategory_affinity = models.JSONField(default=dict, blank=True)
    thc_min = models.FloatField(null=True, blank=True)
    thc_max = models.FloatField(null=True, blank=True)
    price_tier = models.CharField(max_length=8, blank=True)  # value|mid|top (quality tier)
    # 0 = creature-of-habit (buys the same things), 1 = explorer (branches out).
    novelty_score = models.FloatField(default=0)
    # Share of their buys that are core/traffic/profit, e.g. {"core":0.5,"profit":0.4,"traffic":0.1}
    bucket_mix = models.JSONField(default=dict, blank=True)

    # Compact history: [{sku, brand, category, strain_type, qty, last_bought_at, times_bought}]
    purchase_history = models.JSONField(default=list, blank=True)
    computed_at = models.DateTimeField(null=True, blank=True)

    # Who created the row. Dutchie sync only reaches phones Dutchie knows; a caller or visitor we
    # have never sold to is created by us ("voice" | "web") and is NEVER written to Dutchie.
    source = models.CharField(max_length=16, default="dutchie")
    # Dutchie customerIds that fold into this phone (set by sync_transactions): the merge bridge.
    dutchie_ids = models.JSONField(default=list, blank=True)
    # Set when the weekly merge folded this row into another (the phone stays, as a pointer).
    merged_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="merged_from")
    # Customer memory v1 (docs/contracts/customer-memory-v1.md, budtender/memory.py): style, stated
    # likes/dislikes/context, short notes, and `derived` (from purchases). Written ONLY through
    # memory.py (allowlist + 4 KB cap) and only from a TRUSTED session (carrier caller-ID).
    memory = models.JSONField(default=dict, blank=True)
    memory_updated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"CustomerProfile({self.phone})"


class ChatSession(models.Model):
    session_token = models.CharField(max_length=64, unique=True, db_index=True)
    location_slug = models.CharField(max_length=32, blank=True)
    phone = models.CharField(max_length=20, blank=True, db_index=True)
    customer = models.ForeignKey(CustomerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions")
    slots = models.JSONField(default=dict, blank=True)
    stage = models.CharField(max_length=24, default="WELCOME")
    # Sticky conversation-level intent: escalation dominates, else the first real
    # (non-greeting) turn intent. Per-turn intent lives in AnalyticsEvent.props.
    primary_intent = models.CharField(max_length=24, blank=True, db_index=True)
    channel = models.CharField(max_length=16, default="chat")  # chat|questionnaire|voice
    # How `customer` was established: "caller_id" (carrier, voice) | "web_phone" (typed by the
    # visitor; owner-approved identity, HHT_WEB_PHONE_IDENTITY). A website request personalises
    # from `customer` only when this is set.
    identity_via = models.CharField(max_length=16, blank=True)
    # What this conversation taught us while its identity is NOT trusted (typed website phone,
    # anonymous): same schema as CustomerProfile.memory minus `derived`. Never merged into a profile;
    # cleared by identity.unlink_session / forget and when the session changes hands.
    learned = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    started_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Conversations are kept forever (see docs/data-retention in budtender/CLAUDE.md): these
        # indexes keep the owner dashboard and the per-store/day rollups fast as history grows.
        indexes = [
            models.Index(fields=["phone", "-last_active_at"]),
            models.Index(fields=["-last_active_at"], name="chatsession_active_idx"),
            models.Index(fields=["location_slug", "-started_at"], name="chatsession_store_idx"),
        ]


class ChatMessage(models.Model):
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=12)  # user|assistant|system
    content = models.TextField(blank=True)
    chips = models.JSONField(default=list, blank=True)
    result_skus = models.JSONField(default=list, blank=True)  # audit only — never prices
    ts = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ts", "id"]  # id breaks ties: a persisted snapshot inserts many rows in one instant
        indexes = [models.Index(fields=["session", "ts"], name="chatmessage_session_ts_idx")]


class Feedback(models.Model):
    """Customer feedback from the chatbot / feedback page. Phone hashed; raw
    contact email kept only if the customer opts in to a reply."""
    RATINGS = [(i, str(i)) for i in range(1, 6)]
    rating = models.IntegerField(choices=RATINGS, null=True, blank=True)  # 1–5
    category = models.CharField(max_length=32, blank=True)  # suggestions|speed|ux|product|other
    message = models.TextField(blank=True)
    session_token = models.CharField(max_length=64, blank=True, db_index=True)
    phone_hash = models.CharField(max_length=64, blank=True, db_index=True)
    location_slug = models.CharField(max_length=32, blank=True)
    channel = models.CharField(max_length=16, default="chat")
    contact_email = models.EmailField(blank=True)  # only if they want a reply
    resolved = models.BooleanField(default=False)
    ts = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-ts"]


class AnalyticsEvent(models.Model):
    """Every chat/menu interaction. Phone is stored HASHED (never raw) so the
    analytics tables hold no PII. Visible only behind the Cloudflare-Access admin."""
    session_token = models.CharField(max_length=64, db_index=True, blank=True)
    # The browser's anonymous visitor id (hht-visitor-id), lifted out of props so "distinct
    # shoppers" is an indexed SQL count, not a scan of JSON. Empty for rows written before it existed.
    visitor_id = models.CharField(max_length=64, blank=True, db_index=True)
    phone_hash = models.CharField(max_length=64, blank=True, db_index=True)
    location_slug = models.CharField(max_length=32, blank=True, db_index=True)
    channel = models.CharField(max_length=16, default="chat")  # chat|menu|questionnaire
    event_type = models.CharField(max_length=32, db_index=True)
    props = models.JSONField(default=dict, blank=True)
    ts = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "-ts"]),
            models.Index(fields=["location_slug", "-ts"]),
            models.Index(fields=["session_token", "ts"], name="analyticsevent_session_ts_idx"),
        ]


class PhoneCartDraft(models.Model):
    """A staged cart handoff for POS staff — from a phone call or an online order.

    Voice and the public /custom-order page may create/update/release these rows,
    but neither ever writes a Dutchie order. Staff must claim the draft into the
    normal POS session cart before checkout.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RELEASED = "released", "Released"
        CLAIMED = "claimed", "Claimed"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        VOICE = "voice", "Phone call"
        ONLINE = "online", "Online order"

    draft_token = models.CharField(max_length=64, unique=True, db_index=True, default=_phone_cart_token)
    call_id = models.CharField(max_length=80, blank=True, db_index=True)
    session_token = models.CharField(max_length=80, blank=True, db_index=True)
    location_slug = models.CharField(max_length=32, choices=STORES, db_index=True)
    phone_hash = models.CharField(max_length=64, blank=True, db_index=True)
    phone_last4 = models.CharField(max_length=4, blank=True)
    pickup_name = models.CharField(max_length=120, blank=True)
    # Online orders need real contact details — staff has to match the customer to a
    # Dutchie guest and call them if something sold out between order and pickup.
    # The voice path deliberately stores only hash+last4 and leaves these blank.
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.VOICE, db_index=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    contact_email = models.EmailField(blank=True)
    bundle_slug = models.CharField(max_length=32, blank=True)

    class Customer(models.TextChoices):
        MATCHED = "matched", "Matched existing account"
        NEW = "new", "No account — create at claim"
        UNRESOLVED = "unresolved", "Lookup unavailable"

    # An order with no customer is a dead end: cart_submit requires an AcctId. We
    # resolve by phone when the order is placed (read-only) and stamp the result;
    # the POS claim then auto-selects the match, or creates the guest and selects
    # it. `unresolved` means the lookup itself failed — staff must search by hand.
    dutchie_acct_id = models.CharField(max_length=32, blank=True, db_index=True)
    customer_status = models.CharField(max_length=16, choices=Customer.choices, blank=True)
    customer_name = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    lines = models.JSONField(default=list, blank=True)
    quote = models.JSONField(default=dict, blank=True)
    audit = models.JSONField(default=list, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["location_slug", "status", "-updated_at"]),
            models.Index(fields=["phone_hash", "status", "-updated_at"]),
        ]
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"PhoneCartDraft<{self.draft_token} {self.status}>"


class AdminAudit(models.Model):
    """Append-only record of every admin write (bucket override, pairing edit,
    threshold change) — who, what, before/after."""
    actor = models.CharField(max_length=128, blank=True)
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=128, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    ts = models.DateTimeField(auto_now_add=True, db_index=True)


class Setting(models.Model):
    """Admin-tunable knobs (classification thresholds, ranking weights) the jobs
    read at runtime. One row per key."""
    key = models.CharField(max_length=64, unique=True)
    value = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.key


class ManualPairing(models.Model):
    """Admin-defined pairing override: when `anchor_sku` is opened, prefer
    suggesting `pair_sku`. Takes precedence over the computed pairing."""
    location_slug = models.CharField(max_length=32, db_index=True)
    anchor_sku = models.CharField(max_length=64, db_index=True)
    pair_sku = models.CharField(max_length=64)
    note = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("location_slug", "anchor_sku", "pair_sku")


class SuggestedProduct(models.Model):
    KIND = (("primary", "primary"), ("pairing", "pairing"))
    SOURCE = (("chat", "chat"), ("questionnaire", "questionnaire"), ("catalog", "catalog"), ("menu", "menu"))

    session = models.ForeignKey(ChatSession, null=True, blank=True, on_delete=models.SET_NULL, related_name="suggestions")
    customer = models.ForeignKey(CustomerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="suggestions")
    location_slug = models.CharField(max_length=32)
    sku = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=12, choices=KIND, default="primary")
    source = models.CharField(max_length=16, choices=SOURCE, default="chat")
    paired_with_sku = models.CharField(max_length=64, blank=True)
    reason_code = models.CharField(max_length=32, blank=True)
    shown_at = models.DateTimeField(auto_now_add=True)
    accepted = models.BooleanField(null=True, blank=True)
    # docs/contracts/suggestion-analytics-v1.md — what the customer was shown, frozen at suggestion
    # time (budtender.suggestions.snapshot: customer-facing fields only, never cost/margin), so the row
    # still means something once the SKU leaves the menu.
    snapshot = models.JSONField(default=dict, blank=True)
    # brand | category-family | product-line (budtender.suggestions.sibling_key); "" = never a sibling.
    sibling_key = models.CharField(max_length=255, blank=True, db_index=True)
    # phone|chat|questionnaire|similar|pairing|menu|unknown (budtender.suggestions.CHANNELS)
    channel = models.CharField(max_length=16, default="unknown", db_index=True)
    # The session's identity_via at suggestion time; updated when the session later links.
    identity_via = models.CharField(max_length=16, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["customer", "-shown_at"]),
            models.Index(fields=["session", "-shown_at"]),
            models.Index(fields=["kind", "-shown_at"], name="suggested_kind_shown_idx"),
            models.Index(fields=["-shown_at"], name="suggested_shown_idx"),
            models.Index(fields=["channel", "-shown_at"], name="suggested_channel_shown_idx"),
            models.Index(fields=["location_slug", "-shown_at"], name="suggested_store_shown_idx"),
        ]


class SuggestionOutcome(models.Model):
    """Did the customer buy what we suggested (or a sibling) within the window? One per
    SuggestedProduct, created with it (budtender.suggestions). Attribution is event-driven from the
    transaction ingest; the hourly close job decides the rest. Customer-facing amounts only."""

    STATUS = (("pending", "pending"), ("bought_exact", "bought_exact"), ("bought_sibling", "bought_sibling"),
              ("not_bought", "not_bought"), ("unattributable", "unattributable"))
    MATCH = (("exact", "exact"), ("sibling_size", "sibling_size"), ("sibling_strain", "sibling_strain"),
             ("sibling_both", "sibling_both"), ("", ""))

    suggestion = models.OneToOneField(SuggestedProduct, on_delete=models.CASCADE, related_name="outcome")
    status = models.CharField(max_length=16, choices=STATUS, default="pending", db_index=True)
    match_kind = models.CharField(max_length=16, choices=MATCH, blank=True)
    matched_sku = models.CharField(max_length=64, blank=True)
    matched_product_id = models.CharField(max_length=64, blank=True)
    matched_name = models.CharField(max_length=255, blank=True)
    matched_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)  # line total
    matched_at = models.DateTimeField(null=True, blank=True)
    # The transaction line that decided it ("<tx id>:<product id>:<line #>", or "history:<key>" when it
    # came from purchase_history): re-ingesting that line is a no-op.
    matched_line = models.CharField(max_length=160, blank=True)
    window_ends_at = models.DateTimeField(db_index=True)
    evaluated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "window_ends_at"], name="outcome_status_window_idx")]

    def __str__(self) -> str:
        return f"SuggestionOutcome({self.suggestion_id} {self.status})"
