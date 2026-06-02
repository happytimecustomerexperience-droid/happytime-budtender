"""
Pairing/upsell selection: ONE complementary, in-stock, high-margin item.
Prefers items the customer bought before-but-not-recently or bought 2+ times.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.core.cache import cache

from .models import CustomerProfile, ManualPairing, Product

# Pair should be a DIFFERENT consumption method than the anchor.
COMPLEMENT = {
    "flower": ["vape-cartridges", "edibles", "pre-rolls"],
    "pre-rolls": ["flower", "edibles"],
    "concentrates": ["vape-cartridges", "flower"],
    "vape-cartridges": ["flower", "edibles"],
    "disposable-vapes": ["flower", "edibles"],
    "edibles": ["tinctures", "flower"],
    "beverages": ["edibles", "flower"],
    "tinctures": ["edibles", "flower"],
    "topicals": ["edibles", "tinctures"],
}
DEFAULT_COMPLEMENT = ["edibles", "flower", "vape-cartridges"]

W_COMPLEMENT = 0.4
W_MARGIN = 0.35
W_COPURCHASE = 0.5  # can dominate — a known repeat item is a strong signal
RECENT_DAYS = 30
MIN_STOCK = 3       # don't pair near-out-of-stock items


def _history_index(profile: CustomerProfile | None) -> dict:
    if not profile:
        return {}
    return {h.get("sku"): h for h in (profile.purchase_history or []) if h.get("sku")}


def _copurchase_signal(sku: str, hist: dict) -> tuple[float, str]:
    """(score, reason_code) from the customer's own purchase history."""
    h = hist.get(sku)
    if not h:
        return 0.0, ""
    times = int(h.get("times_bought", 0) or 0)
    if times >= 2:
        return 1.0, "bought_2plus_times"
    last = h.get("last_bought_at")
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if dt < datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS):
                return 0.7, "bought_before_not_recent"
        except ValueError:
            pass
        # bought very recently → don't re-suggest
        return 0.0, ""
    return 0.4, "bought_before_not_recent"


def _global_popularity(location: str, anchor_sku: str, cand_sku: str) -> float:
    """Nightly co-purchase matrix in Redis: pair:{location}:{sku} -> {sku: weight}."""
    data = cache.get(f"pair:{location}:{anchor_sku}") or {}
    return float(data.get(cand_sku, 0)) if isinstance(data, dict) else 0.0


def pair_for(location: str, anchor: Product | None, profile: CustomerProfile | None) -> tuple[Product | None, str]:
    if anchor is None:
        return None, "none"

    # Admin-defined manual pairing wins (when the override product is in stock).
    mp = ManualPairing.objects.filter(location_slug=location, anchor_sku=anchor.sku, active=True).first()
    if mp:
        forced = Product.objects.filter(
            location_slug=location, sku=mp.pair_sku, availability=True, quantity_on_hand__gte=MIN_STOCK
        ).first()
        if forced:
            return forced, "staff_pick"

    complements = COMPLEMENT.get(anchor.category, DEFAULT_COMPLEMENT)
    qs = Product.objects.filter(
        location_slug=location, availability=True, quantity_on_hand__gte=MIN_STOCK, category__in=complements
    ).exclude(sku=anchor.sku)
    candidates = list(qs)
    if not candidates:
        return None, "none"

    # Keep the add-on sensible relative to the anchor (no $2000 pairing for a
    # $40 flower). Margin-first still applies *within* this ceiling.
    ceiling = max(float(anchor.price) * 1.6, 25.0)
    capped = [p for p in candidates if float(p.price) <= ceiling]
    if capped:
        candidates = capped

    hist = _history_index(profile)
    margins = [float(p.margin) for p in candidates]
    m_lo, m_hi = min(margins), max(margins)
    span = (m_hi - m_lo) or 1.0

    best, best_score, best_reason = None, -1.0, "pairs_well"
    for p in candidates:
        margin_norm = (float(p.margin) - m_lo) / span
        complement_rank = 1 - (complements.index(p.category) / max(len(complements), 1))
        co_score, co_reason = _copurchase_signal(p.sku, hist)
        pop = _global_popularity(location, anchor.sku, p.sku)
        score = W_COMPLEMENT * complement_rank + W_MARGIN * margin_norm + W_COPURCHASE * max(co_score, pop)
        reason = co_reason or ("popular_pair" if pop > 0 else "pairs_well")
        if score > best_score:
            best, best_score, best_reason = p, score, reason
    return best, best_reason
