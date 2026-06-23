"""CRM models for the voice repo — the returning-caller key + minimal shells.

``phone_hash`` is ported VERBATIM from swedish-bot/crm/models.py (L17-29): a peppered
SHA-256 of the normalized number. The hash is the only returning-caller key, so a DB
leak never exposes a reversible phone index; the pepper is in env and MUST differ from
SECRET_KEY (prod-fail-closed — config/settings.py, 23-SPEC §3.3).

PII discipline (23-SPEC §3.5, binding): the voice repo persists ONLY the hash — NO model
here declares a raw ``phone``/``phone_number`` column (stricter than swedish-bot's Customer,
which keeps one for callbacks). A raw number reaches budtender's resume-by-phone transiently
in-request (P1) and is never stored here. ``Caller``/``CallSession`` are minimal shells now;
P1 wires returning-caller personalization, P3 adds ``VendorCallback``.
"""

from __future__ import annotations

from django.db import models


def phone_hash(phone: str) -> str:
    """Peppered SHA-256 of the normalized phone (ported verbatim, swedish-bot L17-29).
    The hash is the lookup key for returning callers, so a DB leak doesn't expose a
    reversible phone index. Pepper is in env, distinct from SECRET_KEY (prod-fail-closed)."""
    import hashlib

    from django.conf import settings

    norm = "".join(c for c in (phone or "") if c.isdigit() or c == "+")
    if not norm:
        return ""
    pepper = getattr(settings, "PHONE_HASH_PEPPER", "")
    return hashlib.sha256((pepper + norm).encode()).hexdigest()


class Caller(models.Model):
    """A returning caller, keyed ONLY by the peppered phone-hash (no raw number — PII
    discipline, 23-SPEC §3.5). The shell exists now; P1 wires it to budtender's profile."""

    phone_hash = models.CharField(max_length=64, unique=True, db_index=True)
    note = models.CharField(max_length=255, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Caller<{self.phone_hash[:12]}…>"


class CallSession(models.Model):
    """A lightweight per-call shell linking a returning Caller to a Vapi call_id. Kept
    minimal in P0; P1/P2 enrich. Stores no raw phone (the FK Caller already holds only
    the hash)."""

    caller = models.ForeignKey(
        Caller, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions"
    )
    call_id = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"CallSession<{self.call_id}>"


class VendorCallbackStatus(models.TextChoices):
    OPEN = "open", "Open"
    CONTACTED = "contacted", "Contacted"
    CLOSED = "closed", "Closed"


class VendorCallback(models.Model):
    """The durable B2B-callback record (P3, ADR-015; 13-P3 §4.3).

    Written by ``voice/tools/vendor.py::notify_vendor_callback`` on the no-answer return-to-AI leg
    of a vendor call: the warm transfer to the store human didn't connect, so the AI captured what
    the vendor wanted, this row is logged, staff are alerted immediately, and the caller is told a
    callback window. Idempotent on ``vapi_call_id`` (== ``VoiceCall.call_id``) — a Vapi tool-call
    re-delivery never double-creates / double-alerts. P4's dashboard queue lists/filters by
    ``status``/``store``/``created_at`` and marks contacted/closed.

    PII discipline (23-SPEC §3.5 / ADR-006): only the peppered ``caller_phone_hash`` is stored —
    NO raw number column. ``caller_name`` is the spoken name/company (free text), never a number.
    Leak-safe: no product/cost/margin field exists on the model. Mirrors swedish-bot's
    ``ServiceRequest`` durable-record + status-lifecycle idioms."""

    vapi_call_id = models.CharField(max_length=64, unique=True, db_index=True)  # idempotency key
    voice_call = models.ForeignKey(
        "voice.VoiceCall",
        related_name="vendor_callbacks",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    store = models.CharField(max_length=32)  # yakima|mount-vernon|pullman
    reason = models.CharField(max_length=32, blank=True)  # delivery|wholesale_order|manifest|…
    summary = models.TextField(blank=True)  # the caller's stated "why" (server-folded)
    caller_name = models.CharField(max_length=128, blank=True)  # spoken name/company; NO number
    # Peppered SHA-256; the raw number is NEVER stored (PII discipline, ADR-006/019).
    caller_phone_hash = models.CharField(max_length=64, blank=True, db_index=True)
    callback_window = models.CharField(max_length=64, blank=True)  # window stated to the caller
    status = models.CharField(
        max_length=16,
        choices=VendorCallbackStatus.choices,
        default=VendorCallbackStatus.OPEN,
        db_index=True,
    )
    alerted = models.BooleanField(default=False)  # staff alert fired
    contacted_at = models.DateTimeField(null=True, blank=True)  # set by P4 mark_contacted()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"VendorCallback<{self.store}/{self.reason or '—'} {self.status}>"

    def mark_contacted(self) -> None:
        """P4 queue action — the store called the vendor back."""
        from django.utils import timezone

        self.status = VendorCallbackStatus.CONTACTED
        self.contacted_at = timezone.now()
        self.save(update_fields=["status", "contacted_at", "updated_at"])

    def mark_closed(self) -> None:
        """P4 queue action — the callback is resolved."""
        self.status = VendorCallbackStatus.CLOSED
        self.save(update_fields=["status", "updated_at"])


class AlertDelivery(models.Model):
    """The per-(voice_call, sink) idempotency ledger for staff alerts (12-P2 §4.2).

    Ported from swedish-bot/crm/models.py ``LeadDelivery``: one row per ``(voice_call, sink)`` so a
    re-delivered end-of-call-report (Vapi retries) never re-sends an email/Slack. ``crm.sinks.
    dispatch`` ``get_or_create``s a row, short-circuits when ``status=="success"``, and records the
    per-sink outcome — the durable ``VoiceCall`` row is already safe, this just makes the ALERT
    exactly-once."""

    voice_call = models.ForeignKey(
        "voice.VoiceCall", related_name="alert_deliveries", on_delete=models.CASCADE
    )
    sink = models.CharField(max_length=24)  # db | email | slack
    status = models.CharField(max_length=16, default="pending")  # pending|success|failed|skipped
    attempts = models.IntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("voice_call", "sink")]  # the idempotency guarantee
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"AlertDelivery<{self.voice_call_id}/{self.sink}={self.status}>"
