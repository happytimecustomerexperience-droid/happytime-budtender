"""
Data model for the self-contained budtender service.

Sensitive columns (cost, margin) live ONLY on Product and are never exposed by
any serializer — see serializers.py. Customer profiles are keyed by phone.
"""
from django.db import models

STORES = (("yakima", "Yakima"), ("mount-vernon", "Mount Vernon"), ("pullman", "Pullman"))


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


class SyncState(models.Model):
    """When each store's inventory was last successfully refreshed from Dutchie.
    Suggestions are only ever served against in-stock products; this record lets a
    staleness guard force a fresh pull if the inventory is older than 24h, so we
    never recommend something that has since sold out."""
    location_slug = models.CharField(max_length=32, unique=True, db_index=True, choices=STORES)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    item_count = models.IntegerField(default=0)  # in-stock SKUs in the last pull
    # Transaction-ingest watermark: the max transactionDate folded into customer history so far.
    # The recurring sync folds ONLY transactions strictly newer than this (exactly-once, no
    # over-count); null = no history ingested yet → next sync backfills the full lookback window.
    last_tx_at = models.DateTimeField(null=True, blank=True)
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

    def __str__(self) -> str:
        return f"CustomerProfile({self.phone})"


class ChatSession(models.Model):
    session_token = models.CharField(max_length=64, unique=True, db_index=True)
    location_slug = models.CharField(max_length=32, blank=True)
    phone = models.CharField(max_length=20, blank=True, db_index=True)
    customer = models.ForeignKey(CustomerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions")
    slots = models.JSONField(default=dict, blank=True)
    stage = models.CharField(max_length=24, default="WELCOME")
    channel = models.CharField(max_length=16, default="chat")  # chat|questionnaire|voice
    is_active = models.BooleanField(default=True)
    started_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["phone", "-last_active_at"])]


class ChatMessage(models.Model):
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=12)  # user|assistant|system
    content = models.TextField(blank=True)
    chips = models.JSONField(default=list, blank=True)
    result_skus = models.JSONField(default=list, blank=True)  # audit only — never prices
    ts = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ts"]


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
    phone_hash = models.CharField(max_length=64, blank=True, db_index=True)
    location_slug = models.CharField(max_length=32, blank=True, db_index=True)
    channel = models.CharField(max_length=16, default="chat")  # chat|menu|questionnaire
    event_type = models.CharField(max_length=32, db_index=True)
    props = models.JSONField(default=dict, blank=True)
    ts = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["event_type", "-ts"]), models.Index(fields=["location_slug", "-ts"])]


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

    class Meta:
        indexes = [
            models.Index(fields=["customer", "-shown_at"]),
            models.Index(fields=["session", "-shown_at"]),
        ]
