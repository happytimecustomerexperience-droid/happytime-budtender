"""
Precomputed questionnaire FACETS — subtypes / sizes / price-bands.

These are deterministic from inventory and only change when inventory changes, so
we compute them at SYNC time and serve them from the cache. The questionnaire's
subtype/size/price steps then load INSTANTLY and ONE container serves many
concurrent users without re-running the per-request product scans.

A per-store version stamp (bumped on every inventory sync) invalidates the whole
snapshot atomically. The common facets are eagerly warmed right after a sync;
anything not warmed is lazily computed once on first request and cached under the
same version, so it's instant for everyone after that.
"""
from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone

from .models import Product
from .ranking import (CATEGORY_BY_SLOTKEY, MIN_STOCK, available_sizes,
                      price_bands, product_subtype, subtype_label, _size_match)

FACET_TTL = 36 * 3600  # entries self-expire; a version bump supersedes them sooner


def _version(location: str) -> str:
    v = cache.get(f"facetver:{location}")
    if v is None:
        v = "init"
        cache.set(f"facetver:{location}", v, timeout=None)
    return v


def bump_version(location: str) -> None:
    """Invalidate the whole facet snapshot for a store (call on inventory sync)."""
    cache.set(f"facetver:{location}", timezone.now().strftime("%Y%m%d%H%M%S%f"), timeout=None)


def _cached(location: str, key: str, fn):
    ck = f"facet:{location}:{_version(location)}:{key}"
    val = cache.get(ck)
    if val is None:
        val = fn()
        cache.set(ck, val, timeout=FACET_TTL)
    return val


def resolve_category(cat_slot):
    return CATEGORY_BY_SLOTKEY.get(cat_slot, cat_slot) if cat_slot else None


def _instock(location: str, category):
    qs = Product.objects.filter(location_slug=location, availability=True, quantity_on_hand__gte=MIN_STOCK)
    return qs.filter(category=category) if category else qs


# ── pure compute (no cache) ──────────────────────────────────────────────────
def compute_subtypes(location, category) -> list[dict]:
    counts: dict[str, int] = {}
    for name in _instock(location, category).values_list("name", flat=True):
        st = product_subtype(name, category)
        if st:
            counts[st] = counts.get(st, 0) + 1
    return [{"value": v, "label": subtype_label(v), "count": c}
            for v, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True) if c >= 2]


def compute_sizes(location, category, sub) -> list[dict]:
    rows = [(n, uw) for n, uw in _instock(location, category).values_list("name", "unit_weight")
            if not sub or product_subtype(n, category) == sub]
    return available_sizes(rows, category)


def compute_bands(location, category, size, sub) -> dict:
    prices = [float(p.price) for p in _instock(location, category)
              if _size_match(p, size) and (not sub or product_subtype(p.name, p.category) == sub)]
    return {"bands": price_bands(prices), "count": len(prices)}


def compute_doh(location, category, size, sub, price_min, price_max) -> dict:
    """Is 'DOH-certified only?' a REAL choice for the current filters? Only when the
    matching in-stock set has BOTH DOH and non-DOH products. all-DOH → the filter is
    redundant; none-DOH → it's a dead end (e.g. a 5g live-resin cart that isn't DOH)."""
    lo = float(price_min) if price_min not in (None, "") else 0.0
    hi = float(price_max) if price_max not in (None, "") else 1e9
    doh = non = 0
    for p in _instock(location, category):
        if not _size_match(p, size):
            continue
        if sub and product_subtype(p.name, p.category) != sub:
            continue
        if not (lo <= float(p.price) <= hi):
            continue
        if "doh" in (p.name or "").lower():
            doh += 1
        else:
            non += 1
    return {"doh": doh, "non_doh": non, "total": doh + non, "meaningful": doh > 0 and non > 0}


# ── cached public API (used by the views) ────────────────────────────────────
def subtypes(location, category) -> list[dict]:
    return _cached(location, f"sub:{category}", lambda: compute_subtypes(location, category))


def sizes(location, category, sub) -> list[dict]:
    return _cached(location, f"size:{category}:{sub or ''}", lambda: compute_sizes(location, category, sub))


def bands(location, category, size, sub) -> dict:
    return _cached(location, f"band:{category}:{size or ''}:{sub or ''}",
                   lambda: compute_bands(location, category, size, sub))


def doh(location, category, size, sub, price_min, price_max) -> dict:
    key = f"doh:{category}:{size or ''}:{sub or ''}:{price_min or ''}:{price_max or ''}"
    return _cached(location, key, lambda: compute_doh(location, category, size, sub, price_min, price_max))


def warm(location: str) -> int:
    """Eager-precompute the COMMON facets after an inventory sync so the first user
    gets instant steps: every category's subtypes, its sizes (with and within each
    subtype), and the price-bands for the broad + per-size queries. Subtype+size
    band combos stay lazy (cached on first hit) to keep the warm pass bounded."""
    n = 0
    cats = sorted(c for c in set(_instock(location, None).values_list("category", flat=True)) if c)
    for cat in cats:
        subs = subtypes(location, cat); n += 1
        szs = sizes(location, cat, ""); n += 1
        bands(location, cat, "", ""); n += 1
        for sz in szs:
            bands(location, cat, sz["value"], ""); n += 1   # "Any type" + a size
        for st in subs:
            sizes(location, cat, st["value"]); n += 1        # sizes within a subtype
    return n
