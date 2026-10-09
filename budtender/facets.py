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

import hashlib
import json

from django.core.cache import cache
from django.utils import timezone

from . import lab_enrich, live_stock, product_attrs, ranking
from .models import Product
from .ranking import (CATEGORY_BY_SLOTKEY, MIN_STOCK, available_sizes, eligible, kinds_of, master_of,
                      price_bands, product_subtype, size_dimension, size_value_label, subtype_label,
                      _live_price, _size_for, _size_match)

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


# ══ Search v2: viability-aware options (docs/contracts/search-v2.md) ═════════
# Every option below is counted on ranking.eligible — THE candidate set the search draws from — under
# ALL the slots chosen so far, so an option exists only if it leads to >= 1 in-stock product, and the
# count is what the search will find. Rules for a facet's own slot:
#   * step / partition facets (subtypes, sizes, price bands, DOH, thc, pack) and list facets (terpenes,
#     tags, infusion) IGNORE their own slot, so the step shows its alternatives (list slots append, ANY);
#   * refinement facets (extraction, cannabinoids, lab, solventless) HONOUR it and only offer options
#     that narrow further (count < total).
# `skip` is true when the step cannot narrow usefully (partition: < 2 options; label: no option that is
# both non-empty and narrower than the whole set). Results are cached briefly (V2_TTL) per slots.
V2_TTL = 60

FACET_NAMES = ("thc", "terpenes", "cannabinoids", "extraction", "infusion", "tags", "lab", "pack", "solventless")
FACET_LABELS = {
    "thc": "THC strength", "terpenes": "Terpenes (aroma)", "cannabinoids": "Minor cannabinoids",
    "extraction": "Extraction / hash type", "infusion": "Infused with", "tags": "Tags",
    "lab": "Lab data", "pack": "Pack size", "solventless": "Solventless (trichome / ice-water)",
}
# the questions "Specify more" reveals, per category family (order = display order)
SPECIFY_MORE = {
    "flower": ("terpenes", "thc", "cannabinoids", "lab", "tags"),
    "preroll": ("infusion", "terpenes", "thc", "lab", "tags"),
    "infused": ("infusion", "solventless", "terpenes", "thc", "tags"),
    "concentrate": ("extraction", "solventless", "terpenes", "thc", "cannabinoids", "lab", "tags"),
    "vape": ("extraction", "solventless", "terpenes", "thc", "tags"),
    "edible": ("infusion", "solventless", "tags"),
    "other": ("tags",),
    "any": ("terpenes", "thc", "solventless", "tags"),
}
_PARTITION = frozenset({"thc", "pack"})
# THC bands: (value, label, thc_min, thc_max). Inclusive bounds that never overlap, so a band's count is
# exactly what `thc_min`/`thc_max` will find.
_THC_LOW = (("u20", "Under 20%", 0, 19.99), ("20-25", "20–25%", 20, 24.99), ("25-30", "25–30%", 25, 29.99),
            ("30+", "30%+", 30, 100))
_THC_HIGH = (("u70", "Under 70%", 0, 69.99), ("70-80", "70–80%", 70, 79.99), ("80-90", "80–90%", 80, 89.99),
             ("90+", "90%+", 90, 100))
_THC_ANY = (("u20", "Under 20%", 0, 19.99), ("20-30", "20–30%", 20, 29.99), ("30-60", "30–60%", 30, 59.99),
            ("60-80", "60–80%", 60, 79.99), ("80+", "80%+", 80, 100))
_CANNABINOID_OPTIONS = (("cbd", "CBD", "cbd_min", 1.0), ("cbg", "CBG", "cbg_min", 0.5),
                        ("cbn", "CBN", "cbn_min", 0.5), ("cbc", "CBC", "cbc_min", 0.5),
                        ("thcv", "THCV", "thcv_min", 0.3))
_TERPENE_TOTAL_OPTIONS = (1.0, 2.0, 3.0)
_FLOWERISH = frozenset({"flower", "pre-rolls", "blunt", "infused-blunt"})
_EXTRACTS = frozenset({"concentrates", "vape-cartridges"})


class _Ctx:
    """One request's shared reads: the live stock map and the lab/detail memos (one bulk read each)."""

    def __init__(self, location: str):
        self.location = location
        self.live = live_stock.stock_map(location)
        self.labs = lab_enrich.Memo()
        self.details = lab_enrich.Memo()

    def pool(self, slots, ignore=()):
        return eligible(self.location, slots, labs=self.labs, details=self.details, live=self.live,
                        ignore=frozenset(ignore), exact_size=True)

    def info_for(self, products):
        lab_enrich.details_for([p.product_id for p in products], memo=self.details)
        return lambda p: self.details.get(p.product_id) or None

    def lab_for(self, products):
        lab_enrich.labs_for([p.batch_id for p in products], memo=self.labs)
        return lambda p: self.labs.get(p.batch_id) or None


def _clean_slots(slots) -> dict:
    return slots if isinstance(slots, dict) else {}


# The raw slots `ranking.eligible` reads besides the ones `ranking.parse_filters` validates. Together they
# are everything a facet result depends on (test_security_backend pins this against ranking's source).
CACHE_KEY_RAW_SLOTS = ("category", "subcategory", "category_blocklist", "price_min", "price_max", "price_tier",
                       "sort_by", "doh_only", "size", "pack")


def cache_key_slots(slots: dict) -> dict:
    """What the 60s facet cache is keyed on: the parsed v2 filters plus the raw slots eligible() reads,
    never the request's other keys. Junk keys or padding past a field's cap cannot mint a new entry
    (each would be a full recompute and one more Redis key)."""
    raw = {k: slots[k] for k in CACHE_KEY_RAW_SLOTS if slots.get(k) is not None}   # None == absent there
    return {"f": ranking.parse_filters(slots), "raw": raw}


def _cached_v2(location: str, kind: str, slots: dict, fn):
    try:
        blob = json.dumps(cache_key_slots(slots), sort_keys=True, default=str)
    except (TypeError, ValueError):
        return fn()
    ck = f"facet2:{location}:{_version(location)}:{kind}:{hashlib.sha1(blob.encode()).hexdigest()}"
    try:
        val = cache.get(ck)
    except Exception:  # noqa: BLE001 - a cache outage must not take a step down
        val = None
    if val is None:
        val = fn()
        try:
            cache.set(ck, val, V2_TTL)
        except Exception:  # noqa: BLE001
            pass
    return val


def _label_skip(options, total) -> bool:
    return not any(0 < o["count"] < total for o in options)


def _ranked(counts: dict, label, slot_of, top: int | None = None) -> list[dict]:
    items = sorted(((v, c) for v, c in counts.items() if c >= 1), key=lambda kv: (-kv[1], kv[0]))
    return [{"value": v, "label": label(v), "count": c, "slots": slot_of(v)} for v, c in items[:top]]


# ── the four existing steps, now viability-aware ─────────────────────────────
def subtype_options(location: str, slots: dict) -> dict:
    slots = _clean_slots(slots)

    def run():
        ctx = _Ctx(location)
        pool = ctx.pool(slots, ignore={"subcategory"})
        info = ctx.info_for(pool)
        counts: dict[str, int] = {}
        for p in pool:
            for k in kinds_of(p, info(p)):
                counts[k] = counts.get(k, 0) + 1
        opts = _ranked(counts, ranking.subtype_label, lambda v: {"subcategory": v})
        return {"subtypes": opts, "options": opts, "skip": _label_skip(opts, len(pool)), "total": len(pool)}
    return _cached_v2(location, "subtypes", slots, run)


def size_options(location: str, slots: dict) -> dict:
    slots = _clean_slots(slots)

    def run():
        cats, _ = ranking.resolve_category(slots.get("category"))
        if not cats or not size_dimension(cats[0]):
            return {"sizes": [], "options": [], "skip": True, "total": 0}
        ctx = _Ctx(location)
        pool = ctx.pool(slots, ignore={"size"})
        opts = ranking.available_sizes([(p.name, p.unit_weight) for p in pool], cats[0])
        for o in opts:
            o["slots"] = {"size": o["value"]}
        return {"sizes": opts, "options": opts, "skip": len(opts) < 2, "total": len(pool)}
    return _cached_v2(location, "sizes", slots, run)


def band_options(location: str, slots: dict) -> dict:
    slots = _clean_slots(slots)

    def run():
        ctx = _Ctx(location)
        pool = ctx.pool(slots, ignore={"price"})
        prices = [_live_price(ctx.live, p) for p in pool]
        out = []
        for b in ranking.price_bands(prices):
            if "min" in b:
                b = {**b, "count": sum(1 for x in prices if b["min"] <= x <= b["max"]),
                     "slots": {"price_min": b["min"], "price_max": b["max"]}}
            else:
                b = {**b, "count": len(prices), "slots": {"price_min": None, "price_max": None}}
            if b["count"] >= 1:
                out.append(b)
        real = [b for b in out if "min" in b]
        return {"bands": out, "count": len(prices), "options": out, "skip": len(real) < 2}
    return _cached_v2(location, "bands", slots, run)


def doh_options(location: str, slots: dict) -> dict:
    slots = _clean_slots(slots)

    def run():
        ctx = _Ctx(location)
        pool = ctx.pool(slots, ignore={"doh"})
        doh = sum(1 for p in pool if "doh" in (p.name or "").lower())
        non = len(pool) - doh
        meaningful = doh > 0 and non > 0
        opts = ([{"value": "doh", "label": "DOH-certified only", "count": doh, "slots": {"doh_only": True}},
                 {"value": "any", "label": "Any", "count": len(pool), "slots": {"doh_only": False}}]
                if meaningful else [])
        return {"doh": doh, "non_doh": non, "total": len(pool), "meaningful": meaningful,
                "options": opts, "skip": not meaningful}
    return _cached_v2(location, "doh", slots, run)


# ── categories (Dutchie master categories) ───────────────────────────────────
def category_options(location: str, slots: dict | None = None) -> dict:
    """In-stock count per master category, in the fixed master order (then Tincture / Capsule while the
    sync still yields them). Honours the store-wide slots (q, thc, terpenes, doh, ...) but not the
    category-specific steps (subcategory, size, price), which belong to a category already chosen."""
    slots = _clean_slots(slots or {})

    def run():
        ctx = _Ctx(location)
        pool = ctx.pool(slots, ignore={"category", "subcategory", "size", "price"})
        info = ctx.info_for(pool)
        counts: dict[str, int] = {}
        unlisted = 0
        for p in pool:
            m = master_of(p, info(p))
            if m:
                counts[m] = counts.get(m, 0) + 1
            else:
                unlisted += 1
        out = []
        for m in product_attrs.MASTER_ORDER + product_attrs.EXTRA_MASTERS:
            if counts.get(m, 0) >= 1:
                value, label, cats = product_attrs.MASTERS[m]
                out.append({"master": m, "value": value, "label": label, "count": counts[m],
                            "catalog_slugs": list(cats)})
        return {"categories": out, "options": out, "skip": len(out) < 2, "unlisted": unlisted}
    return _cached_v2(location, "categories", slots, run)


# ── POST /products/facets ────────────────────────────────────────────────────
def _thc_bands(slots):
    cats, _ = ranking.resolve_category(slots.get("category"))
    if not cats:
        return _THC_ANY
    c = set(cats)
    if c <= _EXTRACTS:
        return _THC_HIGH
    if c <= _FLOWERISH:
        return _THC_LOW
    return ()   # edibles / topicals / tinctures: a THC % is not how they are shopped


def _facet(ctx: _Ctx, slots: dict, name: str) -> dict:
    if name == "thc":
        bands = _thc_bands(slots)
        pool = ctx.pool(slots, ignore={"thc"}) if bands else []
        lab = ctx.lab_for(pool)
        thcs = [t for p in pool if isinstance(t := lab_enrich.effective_thc(p.thc_percent, lab(p)), (int, float))]
        opts = []
        for value, label, lo, hi in bands:
            n = sum(1 for t in thcs if lo <= float(t) <= hi)
            if n:
                opts.append({"value": value, "label": f"{label} THC", "count": n, "min": lo, "max": hi,
                             "slots": {"thc_min": lo, "thc_max": hi}})
        return {"options": opts, "skip": len(opts) < 2, "total": len(pool)}

    if name == "terpenes":
        pool = ctx.pool(slots, ignore={"terpenes"})
        lab = ctx.lab_for(pool)
        counts: dict[str, int] = {}
        for p in pool:
            for t in product_attrs.lab_terpenes(lab(p)):
                counts[t] = counts.get(t, 0) + 1
        opts = _ranked(counts, product_attrs.terpene_label, lambda v: {"terpenes": [v]}, top=8)
        for o in opts:
            notes = product_attrs._terpenes.NOTES.get(o["value"], {}).get("notes") or []
            if notes:
                o["hint"] = ", ".join(notes)
        return {"options": opts, "skip": _label_skip(opts, len(pool)), "total": len(pool)}

    if name == "cannabinoids":
        pool = ctx.pool(slots)
        lab = ctx.lab_for(pool)
        opts = []
        for value, label, slot, thr in _CANNABINOID_OPTIONS:
            n = sum(1 for p in pool if product_attrs.cannabinoid_pct(lab(p), value) >= thr)
            if 0 < n < len(pool):
                opts.append({"value": value, "label": f"{label} {thr:g}%+", "count": n, "min": thr,
                             "slots": {slot: thr}})
        return {"options": opts, "skip": not opts, "total": len(pool)}

    if name == "extraction":
        pool = [p for p in ctx.pool(slots) if p.category in product_attrs.EXTRACTION_CATEGORIES]
        info = ctx.info_for(pool)
        counts = {}
        for p in pool:
            for k in kinds_of(p, info(p)):
                counts[k] = counts.get(k, 0) + 1
        counts = {k: c for k, c in counts.items() if c < len(pool)}   # a refinement must narrow
        opts = _ranked(counts, ranking.subtype_label, lambda v: {"subcategory": v})
        return {"options": opts, "skip": not opts, "total": len(pool)}

    if name == "infusion":
        pool = ctx.pool(slots, ignore={"infusion"})
        info = ctx.info_for(pool)
        counts = {}
        for p in pool:
            for k in product_attrs.infusion_kinds(p, info(p)):
                counts[k] = counts.get(k, 0) + 1
        opts = _ranked(counts, lambda v: product_attrs.INFUSION_LABELS.get(v, v), lambda v: {"infusion": v})
        return {"options": opts, "skip": _label_skip(opts, len(pool)), "total": len(pool)}

    if name == "tags":
        pool = ctx.pool(slots, ignore={"tags"})
        info = ctx.info_for(pool)
        counts, labels = {}, {}
        for p in pool:
            seen = set()
            for t in product_attrs.info_tags(info(p)):
                k = product_attrs.norm_text(t)[:40]
                if not k or k in seen or product_attrs.is_size_tag(k):
                    continue
                seen.add(k)
                labels.setdefault(k, t.strip()[:40])
                counts[k] = counts.get(k, 0) + 1
        opts = _ranked(counts, lambda v: labels[v], lambda v: {"tags": [v]}, top=12)
        return {"options": opts, "skip": _label_skip(opts, len(pool)), "total": len(pool)}

    if name == "lab":
        pool = ctx.pool(slots)
        lab = ctx.lab_for(pool)
        opts = []
        n = sum(1 for p in pool if product_attrs.has_lab(lab(p)))
        if 0 < n < len(pool):
            opts.append({"value": "lab_tested", "label": "Lab results on file", "count": n,
                         "slots": {"lab_tested": True}})
        for thr in _TERPENE_TOTAL_OPTIONS:
            n = sum(1 for p in pool if product_attrs.total_terpenes(lab(p)) >= thr)
            if 0 < n < len(pool):
                opts.append({"value": f"terpenes_{thr:g}", "label": f"{thr:g}%+ total terpenes", "count": n,
                             "min": thr, "slots": {"terpene_total_min": thr}})
        return {"options": opts, "skip": not opts, "total": len(pool)}

    if name == "pack":
        cats, _ = ranking.resolve_category(slots.get("category"))
        if not cats or size_dimension(cats[0]) != "pack":
            return {"options": [], "skip": True, "total": 0}
        pool = ctx.pool(slots, ignore={"size"})
        counts = {}
        for p in pool:
            v = _size_for(p.name, p.unit_weight, p.category)
            if v:
                counts[v] = counts.get(v, 0) + 1
        opts = [{"value": v, "label": size_value_label(v)[0], "hint": size_value_label(v)[1], "count": c,
                 "slots": {"size": v}}
                for v, c in sorted(counts.items(), key=lambda kv: (kv[0] != "single", float(kv[0][:-2] or 0)
                                                                   if kv[0] != "single" else 0))]
        return {"options": opts, "skip": len(opts) < 2, "total": len(pool)}

    if name == "solventless":
        pool = ctx.pool(slots)
        info = ctx.info_for(pool)
        n = sum(1 for p in pool if product_attrs.is_solventless(p, info(p)))
        opts = ([{"value": "true", "label": "Solventless only", "count": n, "slots": {"solventless": True}}]
                if 0 < n < len(pool) else [])
        return {"options": opts, "skip": not opts, "total": len(pool)}

    raise ValueError(f"unknown facet: {name}")


def facet(location: str, slots: dict, name: str) -> dict:
    slots = _clean_slots(slots)
    if name not in FACET_NAMES:
        raise ValueError(f"unknown facet: {name}")
    return _cached_v2(location, f"facet:{name}", slots,
                      lambda: {"facet": name, "label": FACET_LABELS[name], **_facet(_Ctx(location), slots, name)})


def family(slots: dict) -> str:
    cats, master = ranking.resolve_category(slots.get("category"))
    if not cats:
        return "any"
    c = set(cats)
    if c <= {"flower"}:
        return "flower"
    if master == "Infused Pre-roll" or c <= {"infused-blunt"}:
        return "infused"
    if c & {"pre-rolls", "blunt", "infused-blunt"}:
        return "preroll"
    if c <= {"concentrates"}:
        return "concentrate"
    if c <= {"vape-cartridges"}:
        return "vape"
    if c & {"edibles", "beverages", "mints", "capsules", "tinctures"}:
        return "edible"
    return "other"


def _answered(slots: dict, name: str) -> bool:
    f = ranking.parse_filters(slots)
    if name == "thc":
        return f["thc_min"] is not None or f["thc_max"] is not None
    if name == "pack":
        return bool(ranking.effective_size(slots))
    return False


def specify_more(location: str, slots: dict) -> dict:
    """The extra questions behind "Specify more" for this category, each already filtered to options that
    lead somewhere. Empty `groups` -> the UI hides the button."""
    slots = _clean_slots(slots)

    def run():
        ctx = _Ctx(location)
        groups = []
        for name in SPECIFY_MORE[family(slots)]:
            if _answered(slots, name):
                continue
            res = _facet(ctx, slots, name)
            if not res["skip"] and res["options"]:
                groups.append({"facet": name, "label": FACET_LABELS[name], "options": res["options"]})
        return {"groups": groups, "skip": not groups, "family": family(slots)}
    return _cached_v2(location, "specify", slots, run)
