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
