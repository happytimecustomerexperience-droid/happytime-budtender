"""
Dutchie POS REST adapter (ported from marketing_dashboard's dutchie_pos_client).

Inventory, products, customers and transactions all come from the POS REST API
(`https://api.pos.dutchie.com`) using each store's per-location API key (HTTP
Basic, key as username). The key both authenticates AND scopes to that store —
no location id needed. This is the same source the dashboard uses.

All READ-ONLY. Missing key → returns [] so the service boots and the website
falls back to its local catalog.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.text import slugify

logger = logging.getLogger(__name__)
POS_API_BASE = "https://api.pos.dutchie.com"
TIMEOUT = 120

# Inventory packages not modified in this many days are treated as stale "zombie"
# stock — leftover quantity that lingers in /reporting/inventory but is no longer
# on the live sales-floor menu. A real, selling package gets modified as units
# move; one untouched for months (qty never reconciled to 0) 404s on Shop Now.
# This was THE cause of suggesting products the consumer menu doesn't carry.
FRESH_DAYS = 60

def _norm_category(raw: str) -> str:
    """Map Dutchie's free-form categories (e.g. 'DOH Approved Flower',
    'Disposable Vape', 'Infused Pre-Roll', 'RSO') to our canonical slugs.
    Returns '' for non-sellable items (trade samples) so they're skipped.
    Order matters: pre-roll before flower, etc."""
    s = (raw or "").lower()
    if not s:
        return ""
    if "trade sample" in s or "sample" in s and "mixed" in s:
        return ""
    if "pre-roll" in s or "preroll" in s or "pre roll" in s:
        return "pre-rolls"
    # Concentrate-specific terms first (so 'budder' doesn't fall into flower's 'bud').
    if any(w in s for w in ("concentrate", "rso", "bho", "wax", "shatter", "resin", "rosin", "dab", "badder", "budder", "sugar", "sauce", "diamond", "hash", "kief", "distillate", "applicator")):
        return "concentrates"
    if "flower" in s or "popcorn" in s or "smalls" in s or "shake" in s:
        return "flower"
    if "disposable" in s or "vape" in s or "cartridge" in s or "cart" in s:
        return "vape-cartridges"
    if any(w in s for w in ("edible", "gummy", "gummies", "chocolate", "candy", "cookie", "caramel", "chew", "lozenge", "syrup")):
        return "edibles"
    if "tincture" in s:
        return "tinctures"
    if any(w in s for w in ("topical", "balm", "lotion", "salve", "cream")):
        return "topicals"
    if any(w in s for w in ("beverage", "drink", "soda", "seltzer", "tea")):
        return "beverages"
    if any(w in s for w in ("capsule", "tablet", "pill", "softgel")):
        return "capsules"
    return slugify(s)


def _store(location_slug: str) -> dict:
    return settings.DUTCHIE["stores"].get(location_slug, {})


# Dutchie's WAF 403s requests without Accept/User-Agent — both are required.
_POS_HEADERS = {"Accept": "application/json", "User-Agent": "happytime-budtender/0.1"}


def _pos_get(api_key: str, path: str, params: dict | None = None) -> list | dict | None:
    try:
        r = requests.get(f"{POS_API_BASE}{path}", params=params or {},
                         headers=_POS_HEADERS, auth=(api_key, ""), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Dutchie POS GET %s failed: %s", path, e)
        return None


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _stale(last_modified, max_age_days: int = FRESH_DAYS) -> bool:
    """True if an inventory package hasn't changed in > max_age_days (zombie stock
    not on the live menu). Missing/unparseable date → NOT stale (fail-open: never
    drop a product over a parse error)."""
    s = str(last_modified or "")[:10]
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (datetime.now(timezone.utc).date() - d).days > max_age_days


# ── Inventory (per store, via POS key) ───────────────────────────────────────
def _get_inventory(api_key: str) -> list[dict]:
    """In-stock items. /reporting/inventory carries qty + price + unitCost +
    brand/category/strain/labResults in one call (the /inventory endpoint is
    empty for these read-only keys, so we use the reporting snapshot).

    `includeRoomQuantities=true` makes each row carry `roomQuantities` (per-room
    stock), so we can count ONLY the Sales Floor room — not back-stock or the
    Quarantine/Returns room — when deciding what's buyable."""
    data = _pos_get(api_key, "/inventory", {"includeLabResults": "true", "includeRoomQuantities": "true"})
    if isinstance(data, list) and data:
        return data
    fallback = _pos_get(api_key, "/reporting/inventory", {"includeRoomQuantities": "true"})
    return fallback if isinstance(fallback, list) else []


# Stock in the SALES FLOOR room is the only stock a customer can actually buy.
# `quantityAvailable` sums ALL rooms — including "Quarantine Room/Returns" and
# back-stock — so we count the Sales Floor room only. Room-exact: nothing in
# quarantine/returns/back can leak into suggestions. Matched by room NAME so it's
# store-agnostic across differing roomIds.
def _sales_floor_qty(item: dict) -> float | None:
    """Quantity in the Sales Floor room. None when the row carries no room
    breakdown (older/edge feed) → the caller falls back to the all-rooms
    aggregate rather than hide the whole catalog."""
    rq = item.get("roomQuantities")
    if not isinstance(rq, list) or not rq:
        return None
    return sum(
        _to_float(_first(e, "quantityAvailable", "quantity", default=0))
        for e in rq
        if isinstance(e, dict) and "sales floor" in str(_first(e, "room", "roomName", default="")).lower()
    )


def _extract_thc(item: dict) -> float | None:
    # Lab results vary; try a few common shapes.
    labs = item.get("labResults") or item.get("cannabinoids") or []
    if isinstance(labs, list):
        for lab in labs:
            name = str(_first(lab, "name", "labResult", "type", default="")).upper()
            if name == "THC" or name.startswith("THC"):
                val = _to_float(_first(lab, "value", "percent", "result", default=0))
                if val:
                    return val
    direct = _first(item, "thc", "thcContent", "totalThc")
    return _to_float(direct) if direct else None


_RETIRED_TTL = 3600  # refresh the retired-product list hourly


def _off_menu_product_ids(api_key: str, location_slug: str) -> set[str]:
    """Dutchie productIds that are NOT on the live consumer menu, per the /products
    catalog of record. /reporting/inventory (our stock feed) is a SUPERSET of the
    menu — it lists back-office stock the published menu doesn't carry — so we
    subtract anything the catalog marks as:
      • RETIRED             — isActive is False (archived / off the sales floor)
      • not online-for-sale — onlineAvailable / onlineProduct is False
      • not e-comm listed   — ecomCategory 'N/A' (never categorized for the menu, so
                              it can't be browsed there). This was the "fresh +
                              in-stock + every flag green but still not on the menu"
                              leak (e.g. SnickleFritz LR Sugar White Runtz 1g).

    Cached hourly: /products is a large, slow-changing catalog, so we don't refetch
    it on every 10-min inventory sync. FAIL-OPEN — on a fetch error we return an
    empty set ('don't filter') rather than risk dropping the whole catalog, and we
    do NOT cache the failure so the next sync retries."""
    ck = f"dutchie:offmenu_pids:{location_slug}"
    cached = cache.get(ck)
    if cached is not None:
        return cached
    prods = _pos_get(api_key, "/products")
    if not isinstance(prods, list):
        return set()
    off: set[str] = set()
    for p in prods:
        pid = p.get("productId")
        if pid is None:
            continue
        if (p.get("isActive") is False
                or p.get("onlineAvailable") is False
                or p.get("onlineProduct") is False
                or str(p.get("ecomCategory") or "").strip().upper() == "N/A"):
            off.add(str(pid))
    cache.set(ck, off, timeout=_RETIRED_TTL)
    return off


def _is_purchasable(item: dict, off_menu: set[str], floor_qty: float) -> bool:
    """THE single gate for "is this product actually on the live online menu and
    purchasable." EVERY suggestion — questionnaire, free-text chat, pairing, and
    the upsell popup — is drawn from the Product table this builds, and both
    rank_products and pair_for read ONLY that table, so they all share this one
    definition. Nothing bypasses it. Purchasable iff every condition holds:
      • SALES FLOOR stock  — floor_qty > 0 (vault / Quarantine-Returns / back-stock
        already excluded by the room filter)
      • NOT medical-only   — rec customers can't buy it
      • sellable CATEGORY  — not a trade sample / non-sellable
      • has a retail PRICE — priceless rows are back-office / not-for-sale
      • ON THE LIVE MENU   — per /products: isActive AND onlineAvailable AND
        onlineProduct AND ecomCategory != 'N/A' (see _off_menu_product_ids)
      • FRESH              — lastModifiedDateUtc within FRESH_DAYS (no zombie stock)
    """
    if floor_qty <= 0:
        return False
    if item.get("medicalOnly") is True:
        return False
    if not _norm_category(str(_first(item, "category", "masterCategory") or "")):
        return False
    if _to_float(_first(item, "unitPrice", "recUnitPrice", "price", default=0)) <= 0:
        return False
    if off_menu and str(_first(item, "productId", default="")) in off_menu:
        return False
    if _stale(_first(item, "lastModifiedDateUtc", default="")):
        return False
    return True


def fetch_inventory(location_slug: str) -> list[dict]:
    """Normalized in-stock rows for one store (incl. cost for margin).

    Sourced entirely from /reporting/inventory (8k rows incl. unitCost), so a
    sync is a single call per store. Stock counts the SALES FLOOR room ONLY
    (`_sales_floor_qty`), never back-stock or Quarantine/Returns. We also drop:
    medical-only packages (not sellable to rec customers), rows that aren't really
    on the live menu (RETIRED — POS isActive=False), and STALE zombie packages
    (lastModifiedDateUtc older than FRESH_DAYS — leftover stock the POS snapshot
    never reconciled to 0). So Shop Now never 404s and we never suggest something
    a customer can't grab off the floor.
    """
    key = _store(location_slug).get("pos_key")
    if not key:
        return []
    off_menu = _off_menu_product_ids(key, location_slug)
    # Aggregate by productId so a product split across multiple inventory
    # packages becomes ONE sellable entry with its TRUE total stock — and so we
    # never suggest the same product twice. Only items with a retail (rec) price
    # are kept: priceless rows are back-stock / non-menu items that aren't for
    # sale, which is what was leaking "not for sale" suggestions.
    agg: dict[str, dict] = {}
    for item in _get_inventory(key):
        name = _first(item, "productName", "name") or ""
        # Sales-floor stock only (room-exact); fall back to the all-rooms aggregate
        # ONLY when a row carries no room breakdown.
        floor_qty = _sales_floor_qty(item)
        qty = floor_qty if floor_qty is not None else _to_float(
            _first(item, "quantityAvailable", "quantity", "availableQuantity", default=0))
        # THE single purchasability gate — every suggestion path inherits this.
        if not name or not _is_purchasable(item, off_menu, qty):
            continue
        category = _norm_category(str(_first(item, "category", "masterCategory") or ""))
        price = _to_float(_first(item, "unitPrice", "recUnitPrice", "price", default=0))
        pid = str(_first(item, "productId", "sku", "packageId", "inventoryId", default=name))
        if pid in agg:
            agg[pid]["quantity_on_hand"] += int(qty)
            continue
        sku = str(_first(item, "sku", "inventoryId", "packageId", "productId", default=name))
        agg[pid] = {
            "sku": sku,
            "product_id": str(_first(item, "productId", default="") or ""),
            "name": name,
            "brand": _first(item, "brandName", "brand", default="") or "",
            "category": category,
            "strain": _first(item, "strainName", "strain", default="") or "",
            "strain_type": _first(item, "strainType", default="") or "",
            "thc_percent": _extract_thc(item),
            "dominant_terpene": "",
            "effects": item.get("effects") or [],
            "flavors": [],
            "price": price,
            "price_was": None,
            "cost": _to_float(_first(item, "unitCost", "cost", default=0)),
            "unit_weight": _to_float(_first(item, "unitWeight", default=0)) or None,
            "potency_mg": _to_float(_first(item, "effectivePotencyMg", default=0)) or None,
            "quantity_on_hand": int(qty),
            "slug": slugify(f"{name}-{sku}")[:200],
            "image_url": _first(item, "imageUrl", "image", default="") or "",
        }
    return list(agg.values())


# ── POS customers + transactions ─────────────────────────────────────────────
def get_customers(location_slug: str) -> list[dict]:
    key = _store(location_slug).get("pos_key")
    if not key:
        return []
    out, page = [], 1
    while True:
        data = _pos_get(key, "/customer/customers-paginated", {"PageSize": 1000, "PageNumber": page})
        rows = data if isinstance(data, list) else (data or {}).get("data", [])
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1000 or page > 50:
            break
        page += 1
    return out


def get_register_transactions(location_slug: str, from_iso: str, to_iso: str) -> list[dict]:
    key = _store(location_slug).get("pos_key")
    if not key:
        return []
    data = _pos_get(key, "/reporting/register-transactions",
                    {"fromLastModifiedDateUTC": from_iso, "toLastModifiedDateUTC": to_iso})
    if isinstance(data, list):
        return data
    return (data or {}).get("data", []) if isinstance(data, dict) else []


# Dutchie's /reporting/transactions 400s on a wide window (a full year in one call
# is rejected). It's safe up to ~1 month, so we page the requested range in
# <=31-day windows and concatenate. This is what makes the velocity + customer-
# history sync work at all (a single 365-day call returns 400 → zero history).
_TX_WINDOW_DAYS = 31


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO timestamp to a timezone-AWARE datetime. Dutchie's
    transactionDate is sometimes offset-naive — assume UTC so it can be compared
    against our aware cutoffs (else: 'can't compare naive and aware datetimes')."""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def get_transactions_detailed(location_slug: str, from_iso: str, to_iso: str) -> list[dict]:
    """Completed sales WITH line items. `/reporting/transactions?includeDetail=true`
    embeds an `items[]` array (productId, quantity, unitPrice, unitWeight, isReturned)
    and carries `customerId` + `transactionDate` on each header. This is the
    purchase-history + sales-velocity source.

    The Dutchie endpoint caps the date range, so we PAGE the window in <=31-day
    chunks and concatenate — a single wide call 400s and yields nothing."""
    key = _store(location_slug).get("pos_key")
    if not key:
        return []

    def _rows(payload):
        if isinstance(payload, list):
            return payload
        return (payload or {}).get("data", []) if isinstance(payload, dict) else []

    start, end = _parse_iso(from_iso), _parse_iso(to_iso)
    if start is None or end is None:          # unparseable → single best-effort call
        return _rows(_pos_get(key, "/reporting/transactions",
                              {"fromDateUTC": from_iso, "toDateUTC": to_iso, "includeDetail": "true"}))
    out: list[dict] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=_TX_WINDOW_DAYS), end)
        chunk = _pos_get(key, "/reporting/transactions",
                         {"fromDateUTC": cur.isoformat(), "toDateUTC": nxt.isoformat(),
                          "includeDetail": "true"})
        out.extend(_rows(chunk))
        cur = nxt
    return out
