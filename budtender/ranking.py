"""
Margin-first product ranking with customer-affinity, effect, category and
budget terms. Margin is normalized across the candidate set so it leads without
overriding a clearly on-profile pick.
"""
from __future__ import annotations

import re

from .models import CustomerProfile, Product

# Anonymous: margin-first. Logged-in: taste leads, margin still matters. The two
# weight sets are blended by whether we have a customer profile.
W_ANON = {"margin": 0.55, "affinity": 0.0, "effect": 0.18, "category": 0.05, "bucket": 0.12, "quality": 0.0, "budget": 0.10}
W_KNOWN = {"margin": 0.22, "affinity": 0.34, "effect": 0.10, "category": 0.04, "bucket": 0.12, "quality": 0.14, "budget": 0.04}

# Profit nudge by bucket (balanced — never overrides taste/budget gates).
BUCKET_NUDGE = {"profit": 1.0, "core": 0.4, "traffic": 0.0}
_TIER_CENTER = {"value": -0.6, "mid": 0.0, "top": 0.6}

# Never suggest near-out-of-stock items (avoids "only 1 left" dead ends).
MIN_STOCK = 3

EFFECT_HINTS = {
    "relaxed": {"indica", "myrcene", "linalool", "kush"},
    "uplifted": {"sativa", "limonene", "pinene", "haze"},
    "middle": {"hybrid"},
}

CATEGORY_BY_SLOTKEY = {
    "flower": "flower",
    "concentrate": "concentrates",
    "cartridge": "vape-cartridges",
    "edible": "edibles",
    "tincture": "tinctures",
}


def price_tier_bounds(tier: str | None) -> tuple[float, float]:
    if tier == "value":
        return 0, 20
    if tier == "mid":
        return 20, 40
    if tier == "top":
        return 40, 1e9
    return 0, 1e9


# Size tokens → substrings that may appear in a Dutchie product name. Used as a
# SOFT filter: we narrow to matching sizes, but if that leaves too few picks we
# fall back to ignoring size so the customer always gets options.
_SIZE_SYNONYMS = {
    "1g": ("1g", "1 g", "1gram", "1 gram", "(1g)"),
    "2g": ("2g", "2 g", "2gram", "2 gram"),
    "3.5g": ("3.5g", "3.5 g", "eighth", "1/8"),
    "7g": ("7g", "7 g", "quarter", "1/4"),
    "14g": ("14g", "14 g", "half oz", "half ounce", "1/2 oz"),
    "28g": ("28g", "28 g", "ounce", "1 oz", "1oz", "oz"),
    "0.5g": ("0.5g", ".5g", "half gram", "half-gram", "0.5 g"),
    "5mg": ("5mg", "5 mg"),
    "10mg": ("10mg", "10 mg"),
    "20mg+": ("20mg", "25mg", "50mg", "100mg"),
}


def _parse_size_target(size: str) -> tuple[str, float] | None:
    s = size.lower().strip()
    m = re.search(r"([\d.]+)", s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if "mg" in s:
        return ("mg", val)
    if "g" in s:
        return ("g", val)
    return None


_GRAM_BUCKETS = (0.5, 1.0, 2.0, 3.5, 7.0, 14.0, 28.0)


def size_label(unit_weight: float | None, potency_mg: float | None, category: str | None) -> str:
    """Normalized subcategory label for peer grouping + size filtering.
    Grams for flower/concentrate/cart; mg dose for edibles/tinctures."""
    cat = (category or "").lower()
    if cat in ("edibles", "tinctures") and potency_mg:
        mg = float(potency_mg)
        if mg <= 7:
            return "5mg"
        if mg <= 14:
            return "10mg"
        return "20mg+"
    if unit_weight:
        w = float(unit_weight)
        best = min(_GRAM_BUCKETS, key=lambda g: abs(g - w))
        if abs(best - w) <= 0.3:
            return f"{best:g}g"
    return ""


def _size_match(p: Product, size: str | None) -> bool:
    """Match a product to the requested size. Uses Dutchie's real unitWeight
    (grams) / effectivePotencyMg (dose) when present, falling back to a
    name-substring match. Returns True (no opinion) when size is open-ended."""
    if not size or size in ("any", "stock-up", "disposable"):
        return True
    tgt = _parse_size_target(size)
    if tgt:
        unit, val = tgt
        if unit == "g" and p.unit_weight:
            return abs(float(p.unit_weight) - val) <= 0.3
        if unit == "mg" and p.potency_mg:
            return float(p.potency_mg) >= 20 if val >= 20 else abs(float(p.potency_mg) - val) <= 2.5
    # No structured field → fall back to matching size tokens in the name.
    toks = _SIZE_SYNONYMS.get(size)
    if not toks:
        return True
    hay = (p.name or "").lower()
    return any(t in hay for t in toks)


def _effect_score(p: Product, desired: str | None) -> float:
    if not desired:
        return 0.0
    hints = EFFECT_HINTS.get(desired, set())
    hay = f"{p.strain} {p.strain_type} {p.dominant_terpene} {p.name}".lower()
    return 1.0 if any(h in hay for h in hints) else 0.0


def _affinity_score(p: Product, profile: CustomerProfile | None) -> float:
    if not profile:
        return 0.0
    s = 0.0
    # Brand + strain_type are the strongest "feels like mine" signals.
    s += 1.6 * float(profile.brand_affinity.get(p.brand, 0)) if p.brand else 0
    s += 1.0 * float(profile.strain_type_affinity.get(p.strain_type, 0)) if p.strain_type else 0
    s += 0.6 * float(profile.category_affinity.get(p.category, 0)) if p.category else 0
    s += 0.6 * float((profile.subcategory_affinity or {}).get(p.subcategory, 0)) if p.subcategory else 0
    if p.dominant_terpene:
        s += 0.4 * float(profile.terpene_affinity.get(p.dominant_terpene, 0))
    return min(s, 1.0)


def _quality_fit(p: Product, profile: CustomerProfile | None) -> float:
    """1.0 when the product sits at the customer's usual quality tier, fading
    with distance (uses the peer-relative price_z from classification)."""
    if not profile or not profile.price_tier:
        return 0.0
    center = _TIER_CENTER.get(profile.price_tier, 0.0)
    return 1.0 - min(abs(float(p.price_z or 0) - center) / 2.0, 1.0)


def _novelty_bias(p: Product, profile: CustomerProfile | None) -> float:
    """Habitual buyers (low novelty) get a boost for brands they already buy;
    explorers (high novelty) get a boost for brands they have NOT bought (same
    taste envelope, something new). Returns roughly -0.3..+0.3."""
    if not profile or not p.brand:
        return 0.0
    known = float(profile.brand_affinity.get(p.brand, 0)) > 0
    nov = float(profile.novelty_score or 0)
    if known:
        return 0.3 * (1.0 - nov)      # reward familiar for habitual
    return 0.3 * nov                  # reward new brands for explorers


def rank_products(location: str, slots: dict, profile: CustomerProfile | None,
                  limit: int = 5, exclude_skus: set[str] | None = None) -> list[tuple[Product, str]]:
    exclude_skus = exclude_skus or set()
    cat_slot = slots.get("category")
    category = CATEGORY_BY_SLOTKEY.get(cat_slot, cat_slot) if cat_slot else None
    # Prefer an explicit dollar range; fall back to the tier bounds (chat route).
    if slots.get("price_min") is not None or slots.get("price_max") is not None:
        lo = float(slots.get("price_min") or 0)
        hi = float(slots.get("price_max") or 1e9)
    else:
        lo, hi = price_tier_bounds(slots.get("price_tier"))

    qs = Product.objects.filter(location_slug=location, availability=True, quantity_on_hand__gte=MIN_STOCK)
    if category:
        qs = qs.filter(category=category)
    candidates = [p for p in qs if lo <= float(p.price) <= hi and p.sku not in exclude_skus]
    if not candidates:
        return []

    # Soft size filter: narrow to the requested size if that still leaves a
    # healthy set; otherwise keep all (better to show options than nothing).
    size = slots.get("size")
    if size:
        sized = [p for p in candidates if _size_match(p, size)]
        if len(sized) >= min(limit, 3):
            candidates = sized

    margins = [float(p.margin) for p in candidates]
    m_lo, m_hi = min(margins), max(margins)
    span = (m_hi - m_lo) or 1.0
    desired = slots.get("effect_desired")
    mid = (lo + min(hi, 200)) / 2

    # Taste leads when we know the customer; margin-first when anonymous. Price-
    # sensitive customers (value tier) make traffic-drivers acceptable.
    W = W_KNOWN if profile else W_ANON
    price_sensitive = bool(profile and profile.price_tier == "value")

    scored = []
    for p in candidates:
        margin_norm = (float(p.margin) - m_lo) / span
        budget_fit = 1 - min(abs(float(p.price) - mid) / (mid or 1), 1)
        nudge = BUCKET_NUDGE.get(p.bucket, 0.4)
        if p.bucket == "traffic" and price_sensitive:
            nudge = 0.6  # cheap loss-leaders are fine for bargain hunters
        score = (
            W["margin"] * margin_norm
            + W["affinity"] * _affinity_score(p, profile)
            + W["effect"] * _effect_score(p, desired)
            + W["category"] * (1.0 if category and p.category == category else 0.0)
            + W["bucket"] * nudge
            + W["quality"] * _quality_fit(p, profile)
            + W["budget"] * budget_fit
            + _novelty_bias(p, profile)
        )
        why = _why(p, desired, profile)
        scored.append((score, p, why))

    scored.sort(key=lambda t: t[0], reverse=True)

    picks: list[tuple] = []
    used_skus: set[str] = set()

    # For a known customer, RESERVE up to 2 slots for brands they actually buy
    # (when in stock) so the set visibly feels like theirs — the rest fills by
    # price spread. This is the "familiar + adjacent-new + profit" educated mix.
    if profile and profile.brand_affinity:
        familiar = [(s, p, w) for s, p, w in scored
                    if p.brand and float(profile.brand_affinity.get(p.brand, 0)) > 0]
        seen_brands: set[str] = set()
        for s, p, w in familiar:
            if len(picks) >= 2:
                break
            if p.brand in seen_brands:   # one product per familiar brand, for variety
                continue
            seen_brands.add(p.brand)
            picks.append((s, p, w))
            used_skus.add(p.sku)

    # Spread the remaining slots across DISTINCT prices that exist in the range
    # (incl. odd ones like $65/$75/$85), highest-score item at each price point.
    remaining = [(s, p, w) for s, p, w in scored if p.sku not in used_skus]
    best_at_price: dict[float, tuple] = {}
    for s, p, w in remaining:  # scored desc → first seen at a price = best score
        pr = round(float(p.price), 2)
        if pr not in best_at_price:
            best_at_price[pr] = (s, p, w)
    prices_sorted = sorted(best_at_price)
    need = max(limit - len(picks), 0)
    if prices_sorted and need:
        if len(prices_sorted) <= need:
            spread = [best_at_price[pr] for pr in prices_sorted]
        else:
            n = len(prices_sorted)
            idxs = sorted({int(i * (n - 1) / (need - 1)) for i in range(need)}) if need > 1 else [0]
            j = 0
            while len(idxs) < need and j < n:
                if j not in idxs:
                    idxs.append(j)
                j += 1
            idxs = sorted(set(idxs))[:need]
            spread = [best_at_price[prices_sorted[i]] for i in idxs]
        for s, p, w in spread:
            if p.sku not in used_skus:
                picks.append((s, p, w))
                used_skus.add(p.sku)

    # Backfill if still short (sparse catalog / few distinct prices).
    for s, p, w in scored:
        if len(picks) >= limit:
            break
        if p.sku not in used_skus:
            picks.append((s, p, w))
            used_skus.add(p.sku)

    picks.sort(key=lambda t: t[0], reverse=True)
    return [(p, why) for _, p, why in picks[:limit]]


def _why(p: Product, desired: str | None, profile: CustomerProfile | None) -> str:
    """Natural, taste-aware reason — never a receipt (no dates/exact past SKUs)."""
    bits = []
    if profile and p.brand:
        ba = float(profile.brand_affinity.get(p.brand, 0))
        sta = float(profile.strain_type_affinity.get(p.strain_type, 0)) if p.strain_type else 0
        if ba >= 0.3:
            bits.append(f"{p.brand} is a brand you reach for")
        elif sta >= 0.4 and p.strain_type:
            bits.append(f"a fresh {p.strain_type.lower()} pick in your lane")
        elif profile.price_tier and _quality_fit(p, profile) >= 0.7:
            bits.append("right at your usual quality level")
    if desired and _effect_score(p, desired):
        bits.append(f"matches your {desired} vibe")
    if p.strain and len(bits) < 2:
        bits.append(p.strain)
    return " · ".join(bits[:2]) if bits else ""
