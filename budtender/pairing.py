"""
Pairing/upsell selection: ONE complementary, in-stock, high-margin item.
Prefers items the customer bought before-but-not-recently or bought 2+ times.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.core.cache import cache

from .models import CustomerProfile, ManualPairing, Product
from .ranking import _affinity_score, _quality_fit

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

# Pairing score weights (relative; need not sum to 1). The pair is chosen on
# ATTRIBUTES — category/subcategory(size)/brand/price/profit-bucket/affinity and
# real co-purchase — NEVER the product name, so it stays right as stock rotates.
W_BASKET = 0.45      # "people/you bought these together" (sku-co, attr-co, repeat)
W_CUSTOMER = 0.30    # feels like theirs (brand/size/tier/strain/terpene affinity)
W_COMPLEMENT = 0.20  # a sensible DIFFERENT format than the anchor
W_MARGIN = 0.20      # business profit
W_PRICE = 0.10       # sane add-on price band
RECENT_DAYS = 30
MIN_STOCK = 3        # owner policy: never pair anything with fewer than 3 on hand


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
        return f"Folks who grab a {acat} almost always add a {pcat} like this — it rounds out the sesh."
    if reason_code == "your_brand" and pair and pair.brand:
        return f"It's {pair.brand} — right in your wheelhouse — and a different way to enjoy the night."
    if reason_code == "your_lane":
        return f"Matches your taste and complements the {acat} you just picked."
    return f"A {pcat} is the natural sidekick to your {acat} — they go great together."


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

    complements = COMPLEMENT.get(anchor.category, DEFAULT_COMPLEMENT)
    qs = Product.objects.filter(
        location_slug=location, availability=True, quantity_on_hand__gte=MIN_STOCK, category__in=complements
    ).exclude(sku=anchor.sku)
    candidates = list(qs)
    if not candidates:
        return None, "none", "", 0.0

    # Keep the add-on sensible relative to the anchor (no $2000 pairing for a
    # $40 flower). Scoring still applies *within* this ceiling.
    ceiling = max(float(anchor.price) * 1.6, 25.0)
    capped = [p for p in candidates if float(p.price) <= ceiling]
    if capped:
        candidates = capped

    hist = _history_index(profile)
    margins = [float(p.margin) for p in candidates]
    m_lo, m_hi = min(margins), max(margins)
    span = (m_hi - m_lo) or 1.0
    anchor_price = float(anchor.price) or 1.0

    best, best_score = None, -1.0
    best_reason, best_text, best_strength = "pairs_well", "", 0.0
    for p in candidates:
        margin_norm = (float(p.margin) - m_lo) / span
        complement_rank = 1 - (complements.index(p.category) / max(len(complements), 1))
        # Prefer a DIFFERENT size/format than the anchor (variety, not a near-dupe).
        if p.subcategory and anchor.subcategory and p.subcategory == anchor.subcategory:
            complement_rank *= 0.5
        # basket = strongest "bought together" signal: the customer's own repeat,
        # the exact-SKU co-purchase, or the durable attribute-bucket co-purchase.
        co_score, co_reason = _copurchase_signal(p.sku, hist)
        sku_pop = _global_popularity(location, anchor.sku, p.sku)
        attr_pop = _attr_popularity(location, anchor, p)
        basket = max(co_score, sku_pop, attr_pop)
        # customer fit = does it feel like THEIRS (brand/size/tier/strain/terpene)?
        cust = (0.6 * _affinity_score(p, profile) + 0.4 * _quality_fit(p, profile)) if profile else 0.0
        price_fit = 1 - min(abs(float(p.price) - anchor_price) / anchor_price, 1.0)
        score = (W_BASKET * basket + W_CUSTOMER * cust + W_COMPLEMENT * complement_rank
                 + W_MARGIN * margin_norm + W_PRICE * price_fit)
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
            best_strength = round(min(1.0, 0.55 * basket + 0.45 * cust + (0.2 if co_reason else 0.0)), 3)
    best_text = _reason_text(best_reason, anchor, best, profile) if best else ""
    return best, best_reason, best_text, best_strength
