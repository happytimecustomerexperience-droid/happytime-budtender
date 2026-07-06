"""Local cache of scanned/looked-up guests + an immutable Dutchie WRITE audit,
plus per-visit activity tracking (ShopVisit / ShopEvent)."""

from django.db import models
from django.utils import timezone


class Customer(models.Model):
    """A local record of a scanned or looked-up guest.

    The dashboard `_log` tables are the source of truth for purchase history;
    this table just caches what the POS flow scanned/resolved so the operator
    doesn't re-scan, and links to the Dutchie account when known.
    """

    dutchie_acct_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True, db_index=True)
    birth_date = models.DateField(null=True, blank=True)
    mjstateidno = models.CharField(max_length=120, blank=True)
    over_21 = models.BooleanField(null=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=40, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    email = models.CharField(max_length=255, blank=True)
    raw_scan = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["phone"]),
            models.Index(fields=["dutchie_acct_id"]),
        ]

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip() or "(no name)"
        return f"{name} ({self.dutchie_acct_id})"


class DutchieWriteAudit(models.Model):
    """Immutable log of every Dutchie WRITE (the cart flow moves real inventory)."""

    store = models.CharField(max_length=120)
    action = models.CharField(max_length=40)  # checkin/select/add/status/submit
    acct_id = models.BigIntegerField(null=True, blank=True)
    shipment_id = models.BigIntegerField(null=True, blank=True)
    summary = models.CharField(max_length=500)  # NO PII, no raw creds
    ok = models.BooleanField()
    username = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.store} ok={self.ok} @ {self.created_at:%Y-%m-%d %H:%M}"


class ShopVisit(models.Model):
    """One customer's session with one budtender at one store: created when a customer is
    identified, ended at checkout (checked_out) or restart/new-customer (abandoned)."""

    OUTCOMES = [("open", "open"), ("checked_out", "checked out"), ("abandoned", "abandoned")]

    store = models.CharField(max_length=120)
    budtender = models.CharField(max_length=150, blank=True)
    acct_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    acct_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    how_started = models.CharField(max_length=20, blank=True)  # scan/lookup/guest/phone/name/created
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default="open", db_index=True)
    event_count = models.PositiveIntegerField(default=0)
    items_viewed = models.PositiveIntegerField(default=0)
    items_added = models.PositiveIntegerField(default=0)
    cart_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    order_shipment_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["store", "started_at"]),
            models.Index(fields=["budtender"]),
            models.Index(fields=["outcome"]),
        ]

    @property
    def is_open(self):
        return self.ended_at is None

    @property
    def duration_seconds(self):
        return int(((self.ended_at or timezone.now()) - self.started_at).total_seconds())

    @property
    def duration_display(self):
        s = self.duration_seconds
        return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

    def __str__(self):
        return f"{self.acct_name or 'Guest'} @ {self.store} ({self.outcome})"


class ShopEvent(models.Model):
    """One tracked action inside a visit (or a standalone budtender `login`). No new PII:
    only acct_id/name (already on Customer) + behavior — never DOB/ID#/address."""

    visit = models.ForeignKey(ShopVisit, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="events")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    kind = models.CharField(max_length=32, db_index=True)
    budtender = models.CharField(max_length=150, blank=True)
    acct_id = models.BigIntegerField(null=True, blank=True)
    product_id = models.CharField(max_length=64, blank=True)
    product_name = models.CharField(max_length=255, blank=True)
    brand = models.CharField(max_length=120, blank=True, db_index=True)
    category = models.CharField(max_length=120, blank=True, db_index=True)
    detail = models.CharField(max_length=200, blank=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at"]
        indexes = [
            models.Index(fields=["kind", "at"]), models.Index(fields=["at"]),
            models.Index(fields=["kind", "category"]), models.Index(fields=["kind", "brand"]),
        ]

    def __str__(self):
        return f"{self.kind} {self.product_name or self.detail}".strip()
