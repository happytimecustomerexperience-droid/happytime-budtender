"""
Pairing/upsell selection: ONE complementary, in-stock, high-margin item.
Prefers items the customer bought before-but-not-recently or bought 2+ times.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.core.cache import cache

from .models import CustomerProfile, ManualPairing, Product
from .ranking import _affinity_score, _quality_fit

# The add-on is a LIGHTER, cheaper category than the anchor — a grab-and-go
# impulse buy, not a second main purchase. Each anchor maps to lighter categories
# ONLY, ordered by how natural the attachment is (pre-rolls lead — highest
# attachment rate in cannabis retail). So a 3.5g flower → pre-roll, a cart →
# edible. Heavier/equal categories are intentionally absent.
LADDER_COMPLEMENTS = {
    "flower":           ["pre-rolls", "edibles", "beverages"],
    "concentrates":     ["pre-rolls", "edibles", "beverages"],
    "vape-cartridges":  ["pre-rolls", "edibles", "beverages"],
    "disposable-vapes": ["pre-rolls", "edibles", "beverages"],
    "pre-rolls":        ["edibles", "beverages"],
    "edibles":          ["beverages", "tinctures"],
    "beverages":        ["edibles", "tinctures"],
    "tinctures":        ["edibles", "beverages"],
    "topicals":         ["edibles", "tinctures"],
}
DEFAULT_COMPLEMENT = ["pre-rolls", "edibles", "beverages"]

# A pair must be SIGNIFICANTLY cheaper than the anchor. Research: cross-sell
# converts best around ~25% of the main item's price (acceptable-additional-spend
# + contrast effect); we hard-gate at <=50% and reward the ~25% sweet spot.
MAX_PAIR_PRICE_RATIO = 0.50   # hard gate: pair.price <= 50% of anchor.price
IDEAL_PAIR_PRICE_RATIO = 0.25 # sweet spot the price_fit term peaks at

# Pairing score weights (relative; need not sum to 1). The pair is chosen on
# ATTRIBUTES — category/subcategory(size)/brand/price/profit-bucket/affinity and
# real co-purchase — NEVER the product name, so it stays right as stock rotates.
W_BASKET = 0.40      # "people/you bought these together" (sku-co, attr-co, repeat)
W_CUSTOMER = 0.25    # feels like theirs (brand/size/tier/strain/terpene affinity)
W_LADDER = 0.15      # earlier in the lighter ladder = more natural add-on
W_MARGIN = 0.15      # business profit
W_PRICEFIT = 0.25    # near the ~25% impulse-price sweet spot
RECENT_DAYS = 30
MIN_STOCK = 5        # owner policy: never pair anything with fewer than 5 on the sales floor


def pair_attr_key(category: str | None, subcategory: str | None) -> str:
    """Durable attribute bucket for the co-purchase matrix: 'flower|3.5g'. Keying
    on attributes (not SKU) means a sold-out item still informs the pairing."""
    return f"{(category or '').strip().lower()}|{(subcategory or '').strip().lower()}"


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
            if dt.tzinfo is None:          # purchase_history stores naive UTC stamps
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS):
                return 0.7, "bought_before_not_recent"
        except (ValueError, TypeError):
            pass
        # bought very recently → don't re-suggest
        return 0.0, ""
    return 0.4, "bought_before_not_recent"


def _global_popularity(location: str, anchor_sku: str, cand_sku: str) -> float:
    """Nightly co-purchase matrix in Redis: pair:{location}:{sku} -> {sku: weight}."""
    data = cache.get(f"pair:{location}:{anchor_sku}") or {}
    return float(data.get(cand_sku, 0)) if isinstance(data, dict) else 0.0


def _attr_popularity(location: str, anchor: Product, cand: Product) -> float:
    """Attribute-level 'bought together' weight from the DURABLE matrix
    (pairattr:{loc}:{category|size}) — survives SKU rotation, so 'people who buy
    3.5g flower add a 1g live-resin cart' keeps working as inventory changes."""
    data = cache.get(f"pairattr:{location}:{pair_attr_key(anchor.category, anchor.subcategory)}") or {}
    if not isinstance(data, dict):
        return 0.0
    return float(data.get(pair_attr_key(cand.category, cand.subcategory), 0.0))


def _reason_text(reason_code: str, anchor: Product | None, pair: Product | None,
                 profile: CustomerProfile | None) -> str:
    """A compelling, human sentence for the upsell modal/PairingCard — built only
    from real signals and ATTRIBUTES (never the product-name string)."""
    acat = (anchor.category or "this").replace("-", " ").rstrip("s") if anchor else "this"
    pcat = (pair.category or "add-on").replace("-", " ").rstrip("s") if pair else "add-on"
    if reason_code == "staff_pick":
        return f"Our budtenders hand-pick this {pcat} to go with your {acat}."
    if reason_code == "bought_2plus_times":
        return f"You grab this one a lot — perfect to restock alongside your {acat}."
    if reason_code == "bought_before_not_recent":
        return f"You've loved this before — it's been a minute, and it pairs great with your {acat}."
    if reason_code == "popular_pair":
        return f"Folks who grab a {acat} almost always toss in a {pcat} like this — an easy add-on."
    if reason_code == "your_brand" and pair and pair.brand:
        return f"It's {pair.brand} — right in your wheelhouse — and a different way to enjoy the night."
    if reason_code == "your_lane":
        return f"Matches your taste and complements the {acat} you just picked."
    return f"Round out your {acat} with this {pcat} — a quick, cheap add-on."


def pair_for(location: str, anchor: Product | None, profile: CustomerProfile | None):
    """Choose ONE complementary add-on by ATTRIBUTES + real co-purchase, tuned to
    the customer. Returns (pair|None, reason_code, reason_text, strength) where
    strength ∈ [0,1] gates the upsell modal (only surface a genuinely strong pair)."""
    if anchor is None:
        return None, "none", "", 0.0

    # Admin-defined manual pairing wins (when the override product is in stock).
    mp = ManualPairing.objects.filter(location_slug=location, anchor_sku=anchor.sku, active=True).first()
    if mp:
        forced = Product.objects.filter(
            location_slug=location, sku=mp.pair_sku, availability=True, quantity_on_hand__gte=MIN_STOCK
        ).first()
        if forced:
            return forced, "staff_pick", _reason_text("staff_pick", anchor, forced, profile), 1.0

    complements = LADDER_COMPLEMENTS.get(anchor.category, DEFAULT_COMPLEMENT)
    qs = Product.objects.filter(
        location_slug=location, availability=True, quantity_on_hand__gte=MIN_STOCK, category__in=complements
    ).exclude(sku=anchor.sku)
    candidates = list(qs)
    if not candidates:
        return None, "none", "", 0.0

    # SIGNIFICANTLY cheaper than the anchor — a true impulse add-on. Hard gate at
    # <=50% of the anchor price. If nothing lighter-and-cheaper is in stock, show
    # NO pair (an honest miss beats a bad upsell).
    anchor_price = float(anchor.price) or 1.0
    candidates = [p for p in candidates if float(p.price) <= MAX_PAIR_PRICE_RATIO * anchor_price]
    if not candidates:
        return None, "none", "", 0.0

    hist = _history_index(profile)
    margins = [float(p.margin) for p in candidates]
    m_lo, m_hi = min(margins), max(margins)
    span = (m_hi - m_lo) or 1.0

    best, best_score = None, -1.0
    best_reason, best_text, best_strength = "pairs_well", "", 0.0
    for p in candidates:
        margin_norm = (float(p.margin) - m_lo) / span
        # Earlier in the lighter ladder = the more natural add-on (pre-roll first).
        ladder_rank = 1 - (complements.index(p.category) / max(len(complements), 1)) if p.category in complements else 0.0
        # Prefer a DIFFERENT size/format than the anchor (variety, not a near-dupe).
        if p.subcategory and anchor.subcategory and p.subcategory == anchor.subcategory:
            ladder_rank *= 0.5
        # Impulse-price sweet spot: peaks at ~25% of the anchor, 0 by 50%.
        price_fit = max(0.0, 1 - abs(float(p.price) / anchor_price - IDEAL_PAIR_PRICE_RATIO) / IDEAL_PAIR_PRICE_RATIO)
        # basket = strongest "bought together" signal: the customer's own repeat,
        # the exact-SKU co-purchase, or the durable attribute-bucket co-purchase.
        co_score, co_reason = _copurchase_signal(p.sku, hist)
        sku_pop = _global_popularity(location, anchor.sku, p.sku)
        attr_pop = _attr_popularity(location, anchor, p)
        basket = max(co_score, sku_pop, attr_pop)
        # customer fit = does it feel like THEIRS (brand/size/tier/strain/terpene)?
        cust = (0.6 * _affinity_score(p, profile) + 0.4 * _quality_fit(p, profile)) if profile else 0.0
        score = (W_BASKET * basket + W_CUSTOMER * cust + W_LADDER * ladder_rank
                 + W_MARGIN * margin_norm + W_PRICEFIT * price_fit)
        # Reason priority: personal history > broad co-purchase > brand > lane > generic.
        if co_reason:
            reason = co_reason
        elif sku_pop > 0 or attr_pop > 0:
            reason = "popular_pair"
        elif profile and p.brand and float(profile.brand_affinity.get(p.brand, 0)) >= 0.25:
            reason = "your_brand"
        elif profile and _affinity_score(p, profile) >= 0.3:
            reason = "your_lane"
        else:
            reason = "pairs_well"
        if score > best_score:
            best, best_score, best_reason = p, score, reason
            # Confidence for gating the upsell modal: real bought-together signal +
            # customer fit dominate, with a nudge for a perfectly-priced impulse add-on.
            best_strength = round(min(1.0, 0.45 * basket + 0.35 * cust + 0.20 * price_fit
                                      + (0.15 if co_reason else 0.0)), 3)
    best_text = _reason_text(best_reason, anchor, best, profile) if best else ""
    return best, best_reason, best_text, best_strength
