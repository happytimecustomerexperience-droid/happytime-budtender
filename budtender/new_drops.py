"""New Drops — brands received into inventory in the last N days, per store.

Feeds happytimeweed.com/new-drops via GET /api/v1/new-drops/?store=<slug>.

Pipeline (one run per store, every 30 min while a store is open — tasks.py):
  0. What was actually delivered: the POS REST receive history
     (GET /inventory/receivedinventory). Only transactions with status "Received"
     count — "Saved" is an unreceived draft (some are dated weeks ahead) — and a
     product is a "new drop" only if one of them delivered it (by product or batch id)
     inside the window. A package's own receivedDate is NOT proof of delivery:
     processing a customer return into the "Quarantine Room/Returns" room stamps a
     fresh one (2026-10-04: one returned cartridge made 57 Dank Czar products read
     "received today"). Returns-room packages are also excluded outright.
  1. Backoffice login (dutchie.session.PosClient — same login + 10-min session
     retention as the marketing dashboard) and ONE paginated GraphQL query:
     getPackagesV5 where receivedDate >= now-N days and quantity > 0, newest first.
     These supply the product details; step 0 decides which of them are drops.
  2. Lab result per BATCH (/api/v2/batches/{id}/lab-results) for potency,
     terpenes and the COA link. A batch's lab result never changes, so each is
     cached for 30 days and only new batches cost a call.
  3. Exact menu link: Dutchie's public menu returns each product's slug (cName)
     next to POSMetaData.canonicalID, which IS the backoffice product.id
     (verified 2026-10-01: 41/41 overlapping products matched, 0 same-name/
     different-id conflicts). Matched on that id, never on the name. The map is
     refreshed at most every 6 h and paced, because Cloudflare challenges the
     menu API after ~3 quick requests; a failed refresh keeps the last good map.
  4. The snapshot is stored in Setting(key="new_drops:<slug>") — durable across
     restarts. The view serves it as-is.

Store LocIds (3498/3499/3500) are confirmed against the backoffice's own
LocationName ("Happy Time (Yakima)", "Happy Time - Mt. Vernon", "Happy Time -
Pullman"). .env.dutchie carried 3501 for Yakima, which this user is not
authorized for — hence the explicit, verified constant below.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from dutchie.session import PosClient, Store

from .dutchie import _pos_get, _store, https_url
from .models import Setting

logger = logging.getLogger(__name__)

WINDOW_DAYS = 20  # owner, 2026-10-01: "keep info for 20 days"
PAGE_SIZE = 500

# Dutchie backoffice allows 60 calls/min per login ("Too many requests - only 60
# per minute allowed", observed 2026-10-01) and the dashboard may share the user,
# so stay at <=50/min with headroom.
MIN_CALL_INTERVAL = 1.2
# Lab lookups per store per run, newest arrivals first. Lab results are cached, so
# a cold start fills over a few runs and steady state only fetches new batches —
# a run always fits inside its 30-minute slot.
MAX_LAB_LOOKUPS_PER_RUN = 150

# Verified 2026-10-01 against the backoffice's own LocationName (see module doc).
LOC_IDS = {"yakima": 3498, "mount-vernon": 3499, "pullman": 3500}
# Dutchie dispensary ids for the public menu (the embed hashes the site already uses).
MENU_DISPENSARY_IDS = {
    "yakima": "r8kjngwN38XgnWq9a",
    "mount-vernon": "5ff74ae00e162400b9aa8e4e",
    "pullman": "5ff746ac02d1da221d7215c6",
}
ORG_ID = 8002  # backoffice org (session-block field; constant per tenant)

# getPackagesV5, verbatim from the backoffice web app (captured 2026-10-01).
_QUERY = (Path(__file__).with_name("new_drops_packages.graphql")).read_text(encoding="utf-8")

# Edible-type categories report potency as % of the item's WEIGHT (0.001%) —
# meaningless to a shopper, so they show no potency rather than a misleading one.
_NO_POTENCY_CATEGORIES = {"solid edible", "liquid edible", "edible", "beverage", "topical",
                          "tincture", "capsule", "other"}
THCA_FACTOR = 0.877  # Total THC = THC + 0.877 * THCA (the figure WA labels carry)


class BackofficeClient(PosClient):
    """PosClient against the backoffice, paced under Dutchie's per-minute limit."""

    _last_call = 0.0  # shared by every instance in the process

    def __init__(self, store: Store):
        super().__init__(store)
        self.base_origin = store.base_url.rstrip("/")

    def post(self, path, body, *, _retry=False, raw=False):
        wait = BackofficeClient._last_call + MIN_CALL_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        BackofficeClient._last_call = time.monotonic()
        try:
            return super().post(path, body, _retry=_retry, raw=raw)
        except Exception as exc:
            if "too many requests" not in str(exc).lower() or getattr(self, "_rate_retry", False):
                raise
            logger.info("new_drops: Dutchie rate limit hit on %s; waiting 61s", path)
            time.sleep(61)
            self._rate_retry = True
            try:
                return self.post(path, body, _retry=_retry, raw=raw)
            finally:
                self._rate_retry = False


def _client(location_slug: str) -> BackofficeClient:
    cfg = settings.DUTCHIE
    users = cfg.get("backoffice_users") or []
    if not users:
        raise RuntimeError("DUTCHIE_BACKOFFICE_USERS is empty")
    base = cfg.get("backoffice_base_url") or "https://ash.backoffice.dutchie.com/"
    lsp = int((cfg["stores"].get(location_slug) or {}).get("lsp_id") or 1745)
    return BackofficeClient(Store(
        name=f"newdrops-{location_slug}", base_url=base, pos_base_url="https://ash.pos.dutchie.com",
        org_id=ORG_ID, lsp_id=lsp, loc_id=LOC_IDS[location_slug], register_id=0,
        username=users[0]["username"], password=users[0]["password"],
    ))


# ── 1. received packages ─────────────────────────────────────────────────────
def fetch_received(client: BackofficeClient, days: int = WINDOW_DAYS, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sb = client.session_block()
    out: list[dict] = []
    skip = 0
    while True:
        variables = {
            "lspId": int(sb["LspId"]), "locId": int(sb["LocId"]), "userId": int(sb["UserId"]),
            "orderClause": [{"receivedDate": "DESC"}],
            "whereClause": {"and": [{"receivedDate": {"gte": since}}, {"quantity": {"gt": 0}}]},
            "skip": skip, "take": PAGE_SIZE, "excludeAllocatedInventorySum": True,
            "excludeLastAuditedDateUtc": True, "excludeLastDatePackageAuditedUtc": True,
            "showLowInventory": False,
        }
        data = client.post("/api/graphql", {"query": _QUERY, "variables": variables,
                                            "operationName": "getPackagesV5"}, raw=True)
        if data.get("errors"):
            raise RuntimeError(f"getPackagesV5: {data['errors'][0].get('message')}")
        block = data["data"]["data"]
        items = block.get("items") or []
        out.extend(items)
        skip += len(items)
        if len(items) < PAGE_SIZE or skip >= (block.get("totalCount") or 0):
            return out


# ── 1b. what was actually delivered (POS REST receive history) ───────────────
def _iso_utc(stamp: str | None) -> str:
    """'2026-10-03T02:00:00.0000000' (UTC, no zone) -> '2026-10-03T02:00:00.000Z', the
    same shape as a package receivedDate, so the two compare as plain strings.
    '' when it is not a timestamp."""
    s = (stamp or "").strip()
    return s[:19] + ".000Z" if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d.*", s) else ""


def fetch_receipts(location_slug: str, days: int = WINDOW_DAYS, now: datetime | None = None) -> list[dict]:
    """Raw receive transactions. Raises when Dutchie cannot be reached: an empty list
    means "nothing was received", which must never be inferred from a failed call —
    the run fails and the last good snapshot keeps serving."""
    key = _store(location_slug).get("pos_key")
    if not key:
        raise RuntimeError(f"no POS key for {location_slug}")
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = _pos_get(key, "/inventory/receivedinventory", {"startDate": since})
    if not isinstance(data, list):
        raise RuntimeError(f"receivedinventory unavailable for {location_slug}")
    return data


def received_index(receipts: list[dict], now: datetime, days: int = WINDOW_DAYS) -> dict[str, str]:
    """Pure: {"p:<productId>" / "b:<batchId>" -> latest arrival time} over the
    transactions that really happened: status "Received" (not a "Saved" draft),
    arrived inside the window and not in the future.

    The arrival time is when the entry was ADDED to Dutchie (`addedOn`, a system
    timestamp), not `deliveredOn`: that one is the date typed on the manifest and can
    be days older than the day the order was actually entered (owner, 2026-10-05:
    invoices entered today showed as "Received Thu, Oct 1"). `deliveredOn` is only the
    fallback when `addedOn` is missing."""
    lo = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    hi = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out: dict[str, str] = {}
    for tx in receipts:
        when = _iso_utc(tx.get("addedOn")) or _iso_utc(tx.get("deliveredOn"))
        if tx.get("status") != "Received" or not when or not lo <= when <= hi:
            continue
        for item in tx.get("items") or []:
            for key in (f"p:{item.get('productId')}", f"b:{item.get('batchId')}"):
                if key not in ("p:None", "b:None") and when > out.get(key, ""):
                    out[key] = when
    return out


# ── 2. lab results per batch ─────────────────────────────────────────────────
def _pretty_terpene(key: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", key).split()
    if words and words[0] in ("Alpha", "Beta", "Gamma", "Delta", "Trans", "Cis"):
        return f"{words[0]}-{' '.join(words[1:])}" if len(words) > 1 else words[0]
    return " ".join(words)


def _value(section: dict | None, key: str) -> float | None:
    v = ((section or {}).get(key) or {})
    if not isinstance(v, dict) or v.get("Value") is None:
        return None
    try:
        return float(v["Value"])
    except (TypeError, ValueError):
        return None


def summarize_lab(data: dict, category: str) -> dict:
    """Pure: lab-results Data -> {thc, cbd, potency_unit, terpenes, coa_url}."""
    cann = data.get("Cannabinoids") or {}
    coa = https_url((data.get("TestDetails") or {}).get("CoaUrl")) or None   # lands in an href
    unit_ok = all(((cann.get(k) or {}).get("UnitId") in (None, 2)) for k in ("Thc", "Thca", "Cbd", "Cbda"))
    thc = cbd = None
    if category.strip().lower() not in _NO_POTENCY_CATEGORIES and unit_ok:
        t = (_value(cann, "Thc") or 0) + THCA_FACTOR * (_value(cann, "Thca") or 0)
        c = (_value(cann, "Cbd") or 0) + THCA_FACTOR * (_value(cann, "Cbda") or 0)
        thc = round(t, 1) if t >= 1 else None
        cbd = round(c, 1) if c >= 1 else None
    terps = []
    for key, val in (data.get("Terpenes") or {}).items():
        if isinstance(val, dict) and (val.get("UnitId") in (None, 2)) and (val.get("Value") or 0) > 0:
            terps.append({"name": _pretty_terpene(key), "value": round(float(val["Value"]), 2)})
    terps.sort(key=lambda t: -t["value"])
    return {"thc": thc, "cbd": cbd, "potency_unit": "%" if (thc or cbd) else None,
            "terpenes": terps[:3], "coa_url": coa}


def lab_for_batch(client: BackofficeClient, batch_id: int) -> dict | None:
    """Raw lab-results Data for a batch, cached 30 days. Failures are NOT cached."""
    key = f"newdrops:lab:{batch_id}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        resp = client.post(f"/api/v2/batches/{batch_id}/lab-results", dict(client.session_block()))
    except Exception as exc:  # rate limit / transient: try again next run
        logger.info("new_drops lab-results batch=%s failed: %s", batch_id, exc)
        return None
    data = resp.get("Data") or {}
    cache.set(key, data, 30 * 24 * 3600)
    return data


# ── 3. menu slugs (public menu, matched on POS product id) ───────────────────
# Verified 2026-10-01 on the full Yakima menu (2,890 products):
#  - A menu product groups its SIZES; each size is its own POS product, listed in
#    POSMetaData.children[].canonicalID. Matching only the parent canonicalID found
#    550/1,168 received products; including children, 1,070.
#  - Paging is only stable with an explicit sort: unsorted pages repeat some
#    products and skip others (a Dabstract cart matched in one pull, vanished in the
#    next; the owner's No Mids Popcorn Bud Grape Z was never returned at all).
#  - bypassOnlineThresholds:true includes low-stock items that are still on the menu.
_MENU_Q = """query($id:String!,$p:Int!){ filteredProducts(filter:{dispensaryId:$id, Status:"Active", types:[],
  bypassOnlineThresholds:true, sortBy:"name", sortDirection:1}, page:$p, perPage:100){
  products{ cName POSMetaData{ canonicalID children{ canonicalID } } } queryInfo{ totalPages } } }"""
MENU_FULL_REFRESH = 6 * 3600
MENU_GAP_REFRESH = 25 * 60  # unmatched new arrivals: re-pull at most this often


def _fetch_menu_map(location_slug: str, pause: float = 3.0, retries: int = 4) -> tuple[dict[str, str], bool]:
    """(map of every POS product id -> cName, complete?). A page Cloudflare
    challenges is retried after a pause; if it still fails the partial map is
    returned — every entry in it is keyed on a POS id, so partial is never wrong."""
    from curl_cffi import requests as cffi

    headers = {"content-type": "application/json", "accept": "application/json",
               "origin": "https://dutchie.com", "referer": "https://dutchie.com/"}
    out: dict[str, str] = {}
    page, pages, fails = 0, 1, 0
    while page < pages:
        try:
            resp = cffi.post("https://dutchie.com/graphql", impersonate="chrome", timeout=60, headers=headers,
                             json={"query": _MENU_Q, "variables": {"id": MENU_DISPENSARY_IDS[location_slug], "p": page}})
            fp = resp.json()["data"]["filteredProducts"]
        except Exception as exc:  # Cloudflare challenge / non-JSON
            fails += 1
            logger.info("new_drops menu map %s page %s failed (%s/%s): %s", location_slug, page, fails, retries, exc)
            if fails > retries:
                return out, False
            time.sleep(20)
            continue
        for p in fp.get("products") or []:
            cname = p.get("cName")
            pm = p.get("POSMetaData") or {}
            if not cname:
                continue
            for cid in [pm.get("canonicalID")] + [c.get("canonicalID") for c in pm.get("children") or []]:
                if cid:
                    out[str(cid)] = cname
        pages = int((fp.get("queryInfo") or {}).get("totalPages") or 1)
        page += 1
        time.sleep(pause)
    return out, True


def menu_map(location_slug: str, needed: set[str] | None = None, now: float | None = None) -> dict[str, str]:
    """{POS product id -> Dutchie cName}. Full re-pull every 6 h, or sooner (at most
    every 25 min) while received products still have no slug. A failed or partial
    pull only ever ADDS id-keyed entries; it never empties the map."""
    key, ts_key = f"newdrops:menu:{location_slug}", f"newdrops:menu-ts:{location_slug}"
    current = cache.get(key) or {}
    now = now if now is not None else time.time()
    age = now - (cache.get(ts_key) or 0)
    missing = bool(needed and (needed - current.keys()))
    if age < MENU_FULL_REFRESH and not (missing and age >= MENU_GAP_REFRESH):
        return current
    fetched, _complete = _fetch_menu_map(location_slug)
    current = {**current, **fetched}
    cache.set(key, current, None)
    cache.set(ts_key, now, None)  # also stamps failures, so a blocked menu is not hammered
    return current


# ── 4. build + store the snapshot ────────────────────────────────────────────
_SUFFIXES = {"Llc": "LLC", "Inc": "Inc.", "Co": "Co.", "Lp": "LP", "Llp": "LLP"}


def _title(s: str | None) -> str | None:
    """Vendor names arrive ALL CAPS from the backoffice ("JSM LLC"). Title-case them,
    keeping company suffixes readable ("Jsm Llc" -> "Jsm LLC")."""
    s = (s or "").strip()
    if not (s and s.isupper() and len(s) > 3):
        return s or None
    return re.sub(r"\b(Llc|Inc|Co|Lp|Llp)\b\.?", lambda m: _SUFFIXES[m.group(1)], s.title())


_RETURN_ROOM = re.compile(r"quarantine|return", re.I)
# Trade samples arrive as "<Brand> Trade Sample Mixed" at $0 and Dutchie's own isSample
# flag is True on only some of them (2026-10-04: 31 of 148), so the flag, the name and
# the price each exclude on their own. "Sampler" is a real product and is not matched.
_SAMPLE_NAME = re.compile(r"\bsamples?\b", re.I)


def _is_trade_sample(pkg: dict, name: str, price) -> bool:
    return pkg.get("isSample") is True or bool(_SAMPLE_NAME.search(name)) or not price or float(price) <= 0


def build_snapshot(location_slug: str, packages: list[dict], labs: dict[int, dict | None],
                   slugs: dict[str, str], receipts: dict[str, str], now: datetime,
                   days: int = WINDOW_DAYS) -> dict:
    """Pure: group received products by brand, newest first. One row per product.
    A product is a drop only if `receipts` (received_index) says a real delivery of
    its product or batch landed in the window; that delivery's time is its received_at.
    Trade samples, $0 items and packages sitting in the returns room are not drops."""
    products: dict[str, dict] = {}
    for pkg in packages:
        prod = pkg.get("product") or {}
        name = (prod.get("whseProductsDescription") or "").strip()
        price = pkg.get("recUnitPrice") or pkg.get("unitPrice") or prod.get("whseProductsRecUnitPrice")
        if not name or _is_trade_sample(pkg, name, price):
            continue
        if _RETURN_ROOM.search((pkg.get("room") or {}).get("roomNo") or ""):
            continue
        pid = str(prod.get("id") or "")
        batch = (pkg.get("batch") or {}).get("id")
        received = max((receipts.get(k) or "" for k in (f"p:{pid}", f"b:{batch}")), default="")
        if not received:
            continue
        if pid in products and products[pid]["received_at"] >= received:
            continue
        category = ((prod.get("productTypeNavigation") or {}).get("masterCategory") or "").strip()
        batch_id = (pkg.get("batch") or {}).get("id")
        lab = summarize_lab(labs.get(batch_id) or {}, category) if batch_id else summarize_lab({}, category)
        products[pid] = {
            "brand": ((prod.get("brand") or {}).get("brandName") or "").strip() or _title((pkg.get("vendor") or {}).get("vendorName")),
            "vendor": _title((pkg.get("vendor") or {}).get("vendorName")),
            "row": {
                "name": name,
                "category": category or None,
                "strain": ((prod.get("strain") or {}).get("strainName") or "").strip() or None,
                "strain_type": None,
                "received_at": received,
                "price": round(float(price), 2),
                "menu_slug": slugs.get(pid),
                **lab,
            },
            "received_at": received,
        }
    brands: dict[str, dict] = {}
    for p in products.values():
        if not p["brand"]:
            continue
        b = brands.setdefault(p["brand"], {"brand": p["brand"], "vendor": p["vendor"],
                                           "last_received": "", "products": []})
        b["products"].append(p["row"])
        b["last_received"] = max(b["last_received"], p["received_at"])
    out = sorted(brands.values(), key=lambda b: b["last_received"], reverse=True)
    for b in out:
        b["products"].sort(key=lambda r: r["received_at"], reverse=True)
    return {"store": location_slug, "generated_at": now.isoformat().replace("+00:00", "Z"),
            "window_days": days, "brands": out}


def refresh_store(location_slug: str, now: datetime | None = None,
                  max_lookups: int = MAX_LAB_LOOKUPS_PER_RUN) -> dict:
    now = now or datetime.now(timezone.utc)
    receipts = received_index(fetch_receipts(location_slug, now=now), now)  # first: a failure costs no backoffice call
    client = _client(location_slug)
    packages = fetch_received(client, now=now)
    labs: dict = {}
    lookups = 0
    # Packages arrive newest first; dict.fromkeys keeps that order, so the newest
    # drops get their lab numbers first when the per-run budget runs out.
    for bid in dict.fromkeys((p.get("batch") or {}).get("id") for p in packages):
        if bid is None:
            continue
        cached = cache.get(f"newdrops:lab:{bid}")
        if cached is not None:
            labs[bid] = cached
        elif lookups < max_lookups:
            labs[bid] = lab_for_batch(client, bid)
            lookups += 1
    needed = {str((p.get("product") or {}).get("id")) for p in packages if (p.get("product") or {}).get("id")}
    snap = build_snapshot(location_slug, packages, labs, menu_map(location_slug, needed), receipts, now)
    Setting.objects.update_or_create(key=f"new_drops:{location_slug}", defaults={"value": snap})
    logger.info("new_drops %s: %d packages -> %d brands", location_slug, len(packages), len(snap["brands"]))
    return snap


_SLUG_MEMO: dict[str, tuple[float, dict]] = {}


def menu_slug(location_slug: str, product_id: str) -> str | None:
    """Exact Dutchie menu slug for a POS product id (the map menu_map keeps), or
    None. Read from the cache — never a menu call on the request path. The map is
    memoized per process for 60 s so a 5-card search reads Redis once."""
    if not product_id:
        return None
    ts, m = _SLUG_MEMO.get(location_slug, (0.0, {}))
    if time.monotonic() - ts > 60:
        m = cache.get(f"newdrops:menu:{location_slug}") or {}
        _SLUG_MEMO[location_slug] = (time.monotonic(), m)
    return m.get(str(product_id))


def cached_coa(batch_id: str) -> str:
    """COA link from the cached backoffice lab result, or ''. Cache read only —
    never a Dutchie call, so it is safe on the chat request path."""
    if not batch_id:
        return ""
    data = cache.get(f"newdrops:lab:{batch_id}") or {}
    return https_url((data.get("TestDetails") or {}).get("CoaUrl"))


def backfill_lab(location_slug: str, max_lookups: int = MAX_LAB_LOOKUPS_PER_RUN) -> int:
    """Warm the lab cache for in-stock batches the POS gave no COA for, fastest
    sellers first (what the chat suggests most). Shares the paced client, so it
    stays under Dutchie's per-minute limit; a lab result is cached 30 days, so
    after the first fill only new batches cost a call. Returns lookups made."""
    from .models import Product

    batches = (Product.objects.filter(location_slug=location_slug, availability=True, coa_url="")
               .exclude(batch_id="").order_by("-velocity").values_list("batch_id", flat=True))
    todo = [b for b in dict.fromkeys(batches) if cache.get(f"newdrops:lab:{b}") is None][:max_lookups]
    if todo:
        client = _client(location_slug)
        for bid in todo:
            lab_for_batch(client, bid)
    return len(todo)


def get_snapshot(location_slug: str) -> dict | None:
    row = Setting.objects.filter(key=f"new_drops:{location_slug}").first()
    return row.value if row and row.value else None
