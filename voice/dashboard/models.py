"""Dashboard-local models (14-P4 §3.7/§4.2).

``RankingWeights`` is a singleton (pk forced to 1) holding the owner's ranking levers — the
``W_ANON``/``W_KNOWN`` weight dicts + the margin-emphasis knob. Defaults are byte-identical to
budtender's ``W_ANON``/``W_KNOWN`` (01-ARCHITECTURE.md §3) so a fresh install reproduces
budtender's current behavior exactly. The tuner persists here ALWAYS and pushes to budtender's
admin surface when reachable (``dashboard/weights.py``); budtender owns the ranking — this row is
the editable source the push syncs.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

# budtender's anonymous (margin-first) + known (taste-first) defaults — the fresh-install baseline.
DEFAULT_W_ANON = {
    "margin": 0.55,
    "affinity": 0.0,
    "effect": 0.18,
    "category": 0.05,
    "bucket": 0.12,
    "quality": 0.0,
    "budget": 0.10,
}
DEFAULT_W_KNOWN = {
    "margin": 0.22,
    "affinity": 0.34,
    "effect": 0.10,
    "category": 0.04,
    "bucket": 0.12,
    "quality": 0.14,
    "budget": 0.04,
}


class Credential(models.Model):
    """A dashboard-editable secret/config value (P6 "configure everything, incl. credentials").

    Stored in the DB and applied to BOTH ``os.environ[name]`` and ``settings.<name>`` on save +
    on app startup (``DashboardConfig.ready``), so a change is live for os.environ readers (the
    Vapi client) AND Django-settings readers (transfer numbers, SMTP, budtender token) without an
    env-file edit or redeploy. ``name`` is the canonical ENV/settings var name. The value is NEVER
    rendered in cleartext (the dashboard masks it). Env/.env remains the bootstrap default; a
    Credential row is the override layer on top."""

    name = models.CharField(max_length=64, unique=True)  # ENV var name e.g. VAPI_PRIVATE_KEY
    value = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"Credential<{self.name}>"


class BotCapability(models.Model):
    """The owner's on/off state for one declared capability (``voice/voice/capabilities.py``).

    No row = the capability's declared default. Only declared keys are ever written
    (``capabilities.set_enabled`` refuses anything else)."""

    key = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField()
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return f"BotCapability<{self.key}={'on' if self.enabled else 'off'}>"


class VendorAllowlistEntry(models.Model):
    """One vendor whose calls skip the AI and ring the owner's phone (``voice/vendor_allowlist.py``,
    the /dashboard/vendor-allowlist/ page).

    ``phone`` is the vendor's business number as the owner typed it, normalised to US E.164
    (``+1XXXXXXXXXX``) and matched EXACTLY against the inbound caller-ID. It is a contact the owner
    entered, not a caller's number captured from a call: calls themselves are still logged by the
    peppered hash only (``VoiceCall.caller_phone_hash``), and this table lives here rather than in
    ``crm`` so that app keeps its no-raw-number rule. ``store`` is a label (which store the vendor
    serves); it does not limit which line they can call."""

    STORE_CHOICES = [
        ("", "Any store"),
        ("yakima", "Yakima"),
        ("mount-vernon", "Mount Vernon"),
        ("pullman", "Pullman"),
    ]

    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=16, unique=True)  # +1XXXXXXXXXX, exact-match key
    note = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    store = models.CharField(max_length=32, blank=True, choices=STORE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    last_matched_at = models.DateTimeField(null=True, blank=True)
    match_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return f"VendorAllowlistEntry<{self.name} {'on' if self.active else 'off'}>"

    @property
    def phone_display(self) -> str:
        """``(509) 555-1212`` for a normalised number; the stored value otherwise."""
        d = self.phone[2:] if self.phone.startswith("+1") and len(self.phone) == 12 else ""
        return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d else self.phone


class RankingWeights(models.Model):
    """Singleton (pk=1) — the owner's ranking-weight levers, pushed to budtender (§4.6)."""

    w_anon = models.JSONField(default=dict)  # anonymous caller → margin-first
    w_known = models.JSONField(default=dict)  # known caller → taste-first
    margin_emphasis = models.FloatField(default=1.0)  # multiplier on the anon margin term
    updated_at = models.DateTimeField(auto_now=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "Ranking weights"

    def save(self, *args, **kwargs):
        self.pk = 1  # force the singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> RankingWeights:
        """Get-or-create the singleton, seeded with budtender's defaults on first load."""
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={"w_anon": dict(DEFAULT_W_ANON), "w_known": dict(DEFAULT_W_KNOWN)},
        )
        return obj

    def as_request_config(self) -> dict:
        """The compact ``ranking_weights`` config the voice repo forwards to budtender on every
        suggestion request (the owner's "high margin first" lever reaches the ranker per call).

        budtender owns the re-ranking; this only TELLS it which weights to apply. Shape:
        ``{"w_anon": {...}, "w_known": {...}, "margin_emphasis": <float>}`` — budtender selects
        ``w_anon`` (margin-first) when no ``phone`` is sent, ``w_known`` (taste-first) when one is."""
        return {
            "w_anon": dict(self.w_anon or DEFAULT_W_ANON),
            "w_known": dict(self.w_known or DEFAULT_W_KNOWN),
            "margin_emphasis": float(self.margin_emphasis),
        }

    def is_default(self) -> bool:
        """True when the owner has not changed anything off the byte-identical budtender baseline —
        lets the client OMIT the ``ranking_weights`` param so budtender uses its own defaults
        (zero behavior change until the owner actually tunes a lever)."""
        return (
            self.w_anon == DEFAULT_W_ANON
            and self.w_known == DEFAULT_W_KNOWN
            and self.margin_emphasis == 1.0
        )

    def __str__(self) -> str:
        return f"RankingWeights(margin_emphasis={self.margin_emphasis})"


class JobRun(models.Model):
    """One run of a background job — a Celery beat task, a host cron script, or a manual run —
    reported to the dashboard Health page (``dashboard/health.py``). ``ok`` is null while running.

    Written by the Celery signal handlers in ``core/celery.py`` (source ``beat``) and by
    ``manage.py record_job_run`` (source ``cron`` / ``manual``). The newest ``KEEP`` rows per
    ``name`` are kept; older ones are pruned on write."""

    KEEP = 50
    SOURCES = [("beat", "beat"), ("cron", "cron"), ("manual", "manual")]

    name = models.CharField(max_length=64, db_index=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(null=True, blank=True)
    summary = models.CharField(max_length=500, blank=True)
    source = models.CharField(max_length=8, choices=SOURCES)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        state = "running" if self.ok is None else ("ok" if self.ok else "FAILED")
        return f"JobRun<{self.name} {state}>"

    @classmethod
    def begin(cls, name: str, source: str) -> JobRun:
        """Open a run (``ok`` null = running); close it with ``finish``."""
        row = cls.objects.create(name=name, source=source)
        cls._prune(name)
        return row

    def finish(self, ok: bool, summary: str = "") -> None:
        self.ok, self.summary, self.finished_at = ok, (summary or "")[:500], timezone.now()
        self.save(update_fields=["ok", "summary", "finished_at"])

    @classmethod
    def record(cls, name: str, *, ok: bool, summary: str = "", source: str = "manual") -> JobRun:
        """A run that is already over (a host script reporting once, at its end)."""
        now = timezone.now()
        row = cls.objects.create(
            name=name, source=source, ok=ok, summary=(summary or "")[:500],
            started_at=now, finished_at=now,
        )
        cls._prune(name)
        return row

    @classmethod
    def _prune(cls, name: str) -> None:
        keep = list(
            cls.objects.filter(name=name).order_by("-started_at", "-id").values_list("pk", flat=True)[: cls.KEEP]
        )
        cls.objects.filter(name=name).exclude(pk__in=keep).delete()
