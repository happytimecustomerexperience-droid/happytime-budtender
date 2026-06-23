"""Out-the-door (OTD, tax-included) price helpers for spoken picks (ADR-009; 21-SPEC §5.3).

budtender returns a PRE-TAX ``price`` today (``serializers.public_product`` has no ``price_otd``
field — TODO-B1). The agent MUST speak the out-the-door price (what the customer pays), never the
pre-tax net (ADR-009). Until budtender ships a native ``price_otd``, the voice repo uplifts the
pre-tax ``price`` here — a tiny, deterministic, single-source-of-truth module with the per-store WA
rates as named constants (21-SPEC §5.3 + risk "OTD tax rates duplicated").

WA tax model (mirrors the marketing_dashboard tax-inclusive customer-facing convention): a 37%
excise on the base, then per-store local sales tax on the ``base + excise`` total. The multiplier is
``1 + excise + local·(1 + excise)``. The rates are per-store (the website/dashboard convention):
Yakima 8.4% / Mt Vernon 8.8% / Pullman 8.9% local; an unknown/combined store uses no local (excise
only), matching budtender's engine-default no-local behavior.

**Leak-safe:** the OTD uplift adds NO cost/margin — it derives from the allowlisted ``price`` only.
"""

from __future__ import annotations

# WA cannabis excise (pass-through; applied first, on the pre-tax base).
WA_EXCISE = 0.37

# Per-store local sales tax (applied on base + excise). A store not in the map → no local
# (excise only) — the engine-default "combined" behavior (21-SPEC §5.3).
LOCAL_SALES_TAX = {
    "yakima": 0.084,
    "mount-vernon": 0.088,
    "pullman": 0.089,
}


def otd_multiplier(store: str | None) -> float:
    """The OTD multiplier for a store: ``1 + excise + local·(1 + excise)``.

    Yakima → 1 + 0.37 + 0.084·1.37 = 1.48508. An unknown/combined store → excise only (1.37)."""
    local = LOCAL_SALES_TAX.get((store or "").strip().lower(), 0.0)
    return round((1.0 + WA_EXCISE) * (1.0 + local), 5)


def otd(price: float | int | None, store: str | None) -> float:
    """Uplift a pre-tax ``price`` to the out-the-door figure for ``store`` (ADR-009).

    Deterministic + monotonic: ``otd(p) >= p`` for any positive ``p``. A None/non-positive price
    returns ``0.0`` (no fabricated number — Numbers-Guard). Rounded to cents (what's spoken)."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0:
        return 0.0
    return round(p * otd_multiplier(store), 2)
