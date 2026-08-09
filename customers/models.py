"""Local cache of scanned/looked-up guests + an immutable Dutchie WRITE audit,
plus per-visit activity tracking (ShopVisit / ShopEvent)."""

from django.conf import settings
from django.db import models
from django.utils import timezone


def _dur_display(seconds: int) -> str:
    """Short m/s duration string, e.g. '3m 41s' or '12s'."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


class Customer(models.Model):
    """A local record of a scanned or looked-up guest.

    The dashboard `_log` tables are the source of truth for purchase history;
    this table just caches what the POS flow scanned/resolved so the operator
    doesn't re-scan, and links to the Dutchie account when known.
    """

    dutchie_acct_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    dutchie_code = models.CharField(max_length=120, blank=True)
    customer_type_id = models.IntegerField(null=True, blank=True)
    first_name = models.CharField(max_length=120, blank=True)
    middle_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True, db_index=True)
    birth_date = models.DateField(null=True, blank=True)
    mjstateidno = models.CharField(max_length=120, blank=True)
    id_number = models.CharField(max_length=120, blank=True)
    id_expiration = models.DateField(null=True, blank=True)
    id_type = models.CharField(max_length=40, blank=True)
    gender = models.CharField(max_length=40, blank=True)
    over_21 = models.BooleanField(null=True)
    address = models.CharField(max_length=255, blank=True)
    address2 = models.CharField(max_length=255, blank=True)
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


class StaffSession(models.Model):
    """One staff shift — sign-in to sign-out. Every ShopVisit + Dutchie write made during
    the shift links back here, so 'who worked when, on which register, and what they sold'
    is one query. Times render Pacific (project TIME_ZONE)."""

    ROLES = [("budtender", "budtender"), ("door", "door"), ("admin", "admin")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shifts")
    username = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=12, choices=ROLES, default="budtender")
    store = models.CharField(max_length=120, blank=True)
    register_id = models.CharField(max_length=40, blank=True)
    register_name = models.CharField(max_length=120, blank=True)
    login_at = models.DateTimeField(auto_now_add=True, db_index=True)
    logout_at = models.DateTimeField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    # What Dutchie said this employee may do, read once at sign-in on their own
    # session. Recorded rather than acted on beyond the LogintoPOS gate: with only
    # an Administrator credential to look at we cannot yet tell which permission
    # separates a door person from a budtender, and guessing would either lock out
    # budtenders or admit door staff. A few real shifts turn that into data.
    # Empty list + dutchie_permissions_known False means Dutchie gave no answer.
    dutchie_permissions = models.JSONField(default=list, blank=True)
    dutchie_permissions_known = models.BooleanField(default=False)

    class Meta:
        ordering = ["-login_at"]
        indexes = [models.Index(fields=["username", "login_at"]), models.Index(fields=["store", "login_at"])]

    @property
    def is_open(self):
        return self.logout_at is None

    @property
    def duration_seconds(self):
        return int(((self.logout_at or timezone.now()) - self.login_at).total_seconds())

    @property
    def duration_display(self):
        return _dur_display(self.duration_seconds)

    @property
    def visit_count(self):
        return self.visits.count()

    @property
    def checkout_count(self):
        return self.visits.filter(outcome="checked_out").count()

    @property
    def revenue(self):
        from django.db.models import Sum
        return self.visits.filter(outcome="checked_out").aggregate(s=Sum("cart_total"))["s"] or 0

    def __str__(self):
        return f"{self.username} {self.role} @ {self.store} ({self.login_at:%Y-%m-%d %H:%M})"


class DutchieWriteAudit(models.Model):
    """Immutable log of every Dutchie WRITE (the cart flow moves real inventory)."""

    store = models.CharField(max_length=120)
    action = models.CharField(max_length=40)  # checkin/select/add/status/submit
    acct_id = models.BigIntegerField(null=True, blank=True)
    shipment_id = models.BigIntegerField(null=True, blank=True)
    summary = models.CharField(max_length=500)  # NO PII, no raw creds
    ok = models.BooleanField()
    username = models.CharField(max_length=150, blank=True)
    staff_session = models.ForeignKey("StaffSession", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="writes")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.store} ok={self.ok} @ {self.created_at:%Y-%m-%d %H:%M}"


class ShopVisit(models.Model):
    """One customer's session with one budtender at one store: created when a customer is
    identified, ended at checkout (checked_out) or restart/new-customer (abandoned)."""

    OUTCOMES = [("open", "open"), ("checked_out", "checked out"), ("abandoned", "abandoned")]
    STATUSES = [("queued", "queued"), ("claimed", "claimed")]  # queue lifecycle (orthogonal to outcome)

    store = models.CharField(max_length=120)
    budtender = models.CharField(max_length=150, blank=True)
    claimed_by = models.CharField(max_length=150, blank=True)
    acct_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    acct_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    how_started = models.CharField(max_length=20, blank=True)  # scan/lookup/guest/phone/name/created/door
    # Queue lifecycle: door-scanned customers start "queued" (no budtender); a budtender
    # "claims" them. Existing direct-shop visits default "claimed" so nothing regresses.
    status = models.CharField(max_length=10, choices=STATUSES, default="claimed", db_index=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)   # queue/scan time
    claimed_at = models.DateTimeField(null=True, blank=True)              # budtender pickup time
    ended_at = models.DateTimeField(null=True, blank=True)                # checkout/abandon time
    outcome = models.CharField(max_length=16, choices=OUTCOMES, default="open", db_index=True)
    event_count = models.PositiveIntegerField(default=0)
    items_viewed = models.PositiveIntegerField(default=0)
    items_added = models.PositiveIntegerField(default=0)
    cart_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    order_shipment_id = models.BigIntegerField(null=True, blank=True)
    staff_session = models.ForeignKey("StaffSession", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="visits")

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["store", "started_at"]),
            models.Index(fields=["budtender"]),
            models.Index(fields=["outcome"]),
            models.Index(fields=["store", "status", "ended_at"]),  # live queue query
        ]

    @property
    def is_open(self):
        return self.ended_at is None

    @property
    def duration_seconds(self):
        return int(((self.ended_at or timezone.now()) - self.started_at).total_seconds())

    @property
    def duration_display(self):
        return _dur_display(self.duration_seconds)

    @property
    def wait_seconds(self):
        """Scan → budtender-claim. Live (now − started) while still queued; final once claimed."""
        end = self.claimed_at or (timezone.now() if self.status == "queued" and self.ended_at is None else None)
        return int((end - self.started_at).total_seconds()) if end else None

    @property
    def wait_display(self):
        w = self.wait_seconds
        return _dur_display(w) if w is not None else "—"

    @property
    def service_seconds(self):
        """Claim → checkout (or now if still shopping)."""
        base = self.claimed_at or self.started_at
        return int(((self.ended_at or timezone.now()) - base).total_seconds())

    @property
    def service_display(self):
        return _dur_display(self.service_seconds)

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
