"""Menu data layer: live register inventory ⋈ happytime enrichment, with
categories, facets, search/filter/sort, and profile-aware ranking per category.

Inventory comes from the live product_SearchV2 (the only rows addable to the cart),
cached per store (it's a slow full pull). Each row is enriched with happytime's
budtender_product fields (strain_type/effects/bucket/velocity/margin/image) so the
ranker has the full taste signal. Cart fields (ProductId/BatchId/SerialNo/...) are
preserved on every dict so the Add button posts exactly what the register needs.
"""

from __future__ import annotations

import logging

from django.core.cache import cache

from customers.intelligence import load_product_enrichment
from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import get_store

from . import imagemap, ranking, suggest

logger = logging.getLogger(__name__)

# Long TTL: the `warm_menu` command (every ~8 min) keeps the shared (Redis) cache
# hot, so a customer-facing request NEVER pays the slow product_SearchV2 pull. The
# entry only expires if warming stops. Tune with the warmer interval.
_INV_TTL = 3600

CAT_LABELS = {
    "flower": "Flower", "pre-rolls": "Pre-Rolls", "vapes": "Vapes",
    "concentrate": "Concentrates", "edibles": "Edibles", "topicals": "Topicals",
    "tinctures": "Tinctures", "other": "Other",
}
CAT_ORDER = ["flower", "pre-rolls", "vapes", "concentrate", "edibles", "tinctures", "topicals", "other"]


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _normalize(row, enr):
    pid = str(row.get("ProductId") or "")
    e = enr.get(pid) or enr.get(str(row.get("ProductNo") or "")) or {}
    cat_raw = row.get("ProductCategory") or row.get("MasterCategory") or ""
    d = {
        "product_id": pid,
        "name": row.get("ProductDescription") or row.get("ProductDesc") or "",
        "brand": (row.get("BrandName") or "").strip(),
        "raw_category": cat_raw, "category": cat_raw,
        "strain": (row.get("Strain") or "").strip(),
        "thc": row.get("THCContent") or e.get("thc_percent"),
        "cbd": row.get("CBDContent"),
        "total_terpenes": row.get("TotalTerpenesContent"),
        "price": _f(row.get("UnitPrice")),
        "qty": _f(row.get("TotalAvailable")),
        "image": row.get("ProductImageURL") or "",
        "received_date": row.get("ReceivedDate"),
        "vendor": (row.get("Vendor") or "").strip(),
        "unit_grams": row.get("UnitGrams") or row.get("UnitWeight"),
        # enrichment (sparse-safe)
        "strain_type": e.get("strain_type", ""), "terpene": e.get("terpene", ""),
        "effects": e.get("effects", []) or [], "flavors": e.get("flavors", []) or [],
        "potency_mg": e.get("potency_mg"), "bucket": e.get("bucket", ""),
        "velocity": _f(e.get("velocity")), "margin_pct": _f(e.get("margin_pct")),
        "price_z": _f(e.get("price_z")), "subcategory": e.get("subcategory", ""),
        "price_was": _f(e.get("price_was")),
        # cart fields (exact keys the Add form posts)
        "ProductId": row.get("ProductId"), "BatchId": row.get("BatchId"),
        "SerialNo": row.get("SerialNo"),
        "UnitPrice": _f(row.get("UnitPrice")),
        "RecUnitPrice": _f(row.get("RecUnitPrice") or row.get("UnitPrice")),
        "ProductDesc": row.get("ProductDescription") or row.get("ProductDesc") or "",
        "CannbisProduct": "Yes" if row.get("CannabisInventory") == "Yes" else "No",
    }
    d["cat_key"] = imagemap.category_key(cat_raw) or "other"
    d["cat_label"] = CAT_LABELS.get(d["cat_key"], d["cat_key"].title())
    img, is_static = imagemap.product_image(d)
    d["img"], d["img_static"] = img, is_static
    return d


def get_inventory(store_key, force=False):
    """Cached, normalized + enriched live inventory for a store. A stampede lock
    keeps 3-5 concurrent cold requests from each firing the slow pull — losers serve
    the stale entry. The warmer keeps it hot so requests almost never pull live."""
    ck = f"inv:{store_key}"
    if not force:
        cached = cache.get(ck)
        if cached is not None:
            return cached
    lock = f"inv:lock:{store_key}"
    if not force and not cache.add(lock, "1", 90):
        stale = cache.get(ck)
        if stale is not None:
            return stale  # another worker is refreshing; serve stale
    try:
        rows = PosRegisterClient(get_store(store_key)).product_search()
        enr = load_product_enrichment(store_key)
        items = [_normalize(r, enr) for r in rows if isinstance(r, dict)]
        cache.set(ck, items, _INV_TTL)
        return items
    finally:
        cache.delete(lock)


def categories(items):
    counts = {}
    for p in items:
        counts[p["cat_key"]] = counts.get(p["cat_key"], 0) + 1
    out = [{"key": k, "label": CAT_LABELS.get(k, k.title()), "count": counts[k],
            "tile": imagemap._CATS.get(k)} for k in CAT_ORDER if k in counts]
    return out


def facets(items):
    brands = sorted({p["brand"] for p in items if p["brand"]})
    strain_types = sorted({p["strain_type"] for p in items if p["strain_type"]})
    effects = sorted({e for p in items for e in (p["effects"] or []) if e})
    prices = [p["price"] for p in items if p["price"] > 0]
    has_doh = any("doh" in (p["name"] or "").lower() for p in items)
    return {
        "brands": brands, "strain_types": strain_types, "effects": effects[:20],
        "price_min": int(min(prices)) if prices else 0,
        "price_max": int(max(prices)) + 1 if prices else 0,
        "has_doh": has_doh,
    }


_SORTS = {
    "price_asc": lambda items, prof: sorted(items, key=lambda p: p["price"]),
    "price_desc": lambda items, prof: sorted(items, key=lambda p: p["price"], reverse=True),
    "thc_desc": lambda items, prof: sorted(items, key=lambda p: _f(p["thc"]), reverse=True),
    "popular": lambda items, prof: sorted(items, key=lambda p: p["velocity"], reverse=True),
}


def find_item(store_key, product_id=None, serial=None):
    """Authoritative product row from the cached inventory (trusted price/serial/
    batch) — used to re-resolve a cart line server-side so the client can't forge
    price or serial. Returns the normalized dict or None."""
    pid = str(product_id) if product_id is not None else None
    for p in get_inventory(store_key):
        if pid and str(p.get("product_id")) == pid:
            return p
        if serial and p.get("SerialNo") == serial:
            return p
    return None


def query(items, profile, f):
    """Apply search + filters + sort. `f` is a dict of request filters.
    Always restricted to in-stock items (owner rule)."""
    q = (f.get("q") or "").strip().lower()
    out = [p for p in items if p.get("qty", 0) > 0]   # always in-stock only
    if f.get("cat"):
        out = [p for p in out if p["cat_key"] == f["cat"]]
    if q:
        out = [p for p in out if q in
               f"{p['name']} {p['brand']} {p['strain']} {p['raw_category']}".lower()]
    if f.get("brand"):
        out = [p for p in out if p["brand"] == f["brand"]]
    if f.get("strain_type"):
        out = [p for p in out if p["strain_type"] == f["strain_type"]]
    if f.get("effect"):
        out = [p for p in out if f["effect"] in (p["effects"] or [])]
    if f.get("price_min") is not None:
        out = [p for p in out if p["price"] >= f["price_min"]]
    if f.get("price_max") is not None:
        out = [p for p in out if p["price"] <= f["price_max"]]
    if f.get("thc_min"):
        out = [p for p in out if _f(p["thc"]) >= f["thc_min"]]
    if f.get("doh_only"):
        out = [p for p in out if "doh" in (p["name"] or "").lower()]

    sort = f.get("sort") or "foryou"
    if sort in _SORTS:
        return _SORTS[sort](out, profile)
    return ranking.rank(out, profile)  # "foryou" (margin-first when profile None)


def suggestions(store_key, profile, limit=8):
    if not profile:
        return []
    return suggest.suggest(get_inventory(store_key), profile, limit=limit)
