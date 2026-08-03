"""Live stock + price for every customer-facing answer.

The `Product` table is ENRICHMENT — strain type, terpenes, effects, flavors,
images, subcategory, and the margin/velocity classification. That data changes
slowly and is correctly persisted.

Stock and price are NOT enrichment. Answering "is this in stock, what does it
cost" out of a table refreshed on a 10-minute beat is how a caller on the phone
gets told yes about something that sold out twenty minutes ago. Those two facts
come from here instead: the SAME Dutchie pull `tasks.sync_inventory` uses
(`dutchie.fetch_inventory` — sales-floor room only, medical/retired/zombie rows
already dropped), called live behind a short-TTL cache rather than persisted.

Never raises. On a failed pull we serve the last good snapshot, and past that we
report `source="db"` so the caller can degrade honestly instead of silently
quoting stale numbers.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


def _offline() -> bool:
    """No network pulls under test — same guard dutchie/stores.py uses.

    Without this a unit test hits the real Dutchie API, and any SKU the suite
    invented is (correctly) absent from real inventory, so every fixture looks
    sold out. Tests that want live behaviour call `prime()` with their own rows.
    """
    return "pytest" in sys.modules or bool(os.environ.get("BUDTENDER_TESTING"))

# Short enough that a sellout is visible within a couple of minutes; long enough
# that a burst of chat/voice traffic doesn't fan out into one Dutchie pull each.
TTL = 90
# A cold pull is a full-store REST call. Losers on the lock serve the stale entry
# rather than pile on — same pattern as pos.catalog.get_inventory.
LOCK_TTL = 60
# Kept well past TTL so a Dutchie outage degrades to "a few minutes old" instead
# of falling all the way back to the beat-refreshed table.
STALE_TTL = 3600

# cost/margin never enter a live row — this module feeds customer-facing paths.
_FIELDS = ("sku", "product_id", "name", "brand", "category", "price", "price_was",
           "quantity_on_hand")


def _key(location_slug: str) -> str:
    return f"livestock:{location_slug}"


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class StockMap:
    """Live stock/price for one store, indexed by both join keys.

    `source` is "live" (fresh pull), "cache" (within TTL), "stale" (served past
    TTL because the pull failed) or "db" (no live data at all — caller must treat
    stock/price as unverified).
    """

    location_slug: str
    source: str = "db"
    generated_at: str = ""
    by_sku: dict[str, dict] = field(default_factory=dict)
    by_product_id: dict[str, dict] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """True when this map can authoritatively answer stock/price."""
        return self.source != "db" and bool(self.by_sku)

    def get(self, sku: str = "", product_id: str = "") -> dict | None:
        if sku:
            hit = self.by_sku.get(str(sku))
            if hit:
                return hit
        if product_id:
            return self.by_product_id.get(str(product_id))
        return None

    def qty(self, sku: str = "", product_id: str = "") -> float:
        row = self.get(sku, product_id)
        return _f(row.get("quantity_on_hand")) if row else 0.0

    def buyable(self, sku: str = "", product_id: str = "", *, min_stock: int = 1) -> bool:
        """Is this SKU actually on the sales floor in sellable quantity right now?

        When there is no live data we cannot answer, so we do not veto — the
        caller keeps the DB's own availability gate. Vetoing here would empty the
        menu during a Dutchie outage.
        """
        if not self.usable:
            return True
        return self.qty(sku, product_id) >= min_stock


def _build(location_slug: str, rows: list[dict], source: str) -> StockMap:
    by_sku: dict[str, dict] = {}
    by_pid: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        rec = {k: r.get(k) for k in _FIELDS}
        rec["quantity_on_hand"] = _f(r.get("quantity_on_hand"))
        rec["price"] = _f(r.get("price"))
        pw = r.get("price_was")
        rec["price_was"] = _f(pw) if pw not in (None, "") else None
        sku = str(r.get("sku") or "")
        pid = str(r.get("product_id") or "")
        if sku:
            by_sku[sku] = rec
        if pid:
            by_pid[pid] = rec
    return StockMap(location_slug=location_slug, source=source,
                    generated_at=timezone.now().isoformat(),
                    by_sku=by_sku, by_product_id=by_pid)


def prime(location_slug: str, rows: list[dict]) -> None:
    """Seed the cache from rows a caller already pulled (sync_inventory has them
    in hand, so the beat sync doubles as the warmer and costs nothing extra)."""
    try:
        cache.set(_key(location_slug), list(rows), STALE_TTL)
        cache.set(_key(location_slug) + ":fresh", "1", TTL)
    except Exception:
        logger.debug("live_stock.prime failed for %s", location_slug, exc_info=True)


def stock_map(location_slug: str, *, force: bool = False) -> StockMap:
    """Live stock/price for one store. Never raises."""
    ck = _key(location_slug)
    try:
        if not force and cache.get(ck + ":fresh"):
            cached = cache.get(ck)
            if cached is not None:
                return _build(location_slug, cached, "cache")
    except Exception:
        logger.debug("live_stock cache read failed for %s", location_slug, exc_info=True)

    # Only one worker pays for the pull; the rest serve what we already have.
    lock = ck + ":lock"
    try:
        got_lock = force or cache.add(lock, "1", LOCK_TTL)
    except Exception:
        got_lock = True
    if not got_lock:
        try:
            stale = cache.get(ck)
        except Exception:
            stale = None
        if stale is not None:
            return _build(location_slug, stale, "stale")
        return StockMap(location_slug=location_slug, source="db")

    try:
        if _offline():
            rows = []
        else:
            from . import dutchie  # local import: keeps this module importable without creds
            rows = dutchie.fetch_inventory(location_slug)
    except Exception:
        logger.warning("live_stock pull failed for %s", location_slug, exc_info=True)
        rows = []
    finally:
        try:
            cache.delete(lock)
        except Exception:
            pass

    if rows:
        prime(location_slug, rows)
        return _build(location_slug, rows, "live")

    # Pull failed or the store has no key — last good snapshot, else give up and
    # tell the caller so it can fall back to the table rather than quote nothing.
    try:
        stale = cache.get(ck)
    except Exception:
        stale = None
    if stale:
        logger.warning("live_stock serving stale snapshot for %s", location_slug)
        return _build(location_slug, stale, "stale")
    return StockMap(location_slug=location_slug, source="db")
