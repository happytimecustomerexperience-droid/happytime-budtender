"""Today's deals on each store's Dutchie online menu. READ-ONLY (HTTP GET); feeds GET /api/v1/deals/.

Two feeds per store (each key is scoped to its store):
  * ``/discounts/v2/list``   - automatic deals, with the name/description the online menu shows.
  * ``/reporting/discounts`` - manual "button" deals; only those flagged ``isAvailableOnline``.
POS-only discounts (employee, senior, veteran, medical, points...) are never online, so they never
appear here.

"Current" is computed here, in Pacific time: Dutchie's ``isActive`` is only an enabled flag (true
for expired deals too). A deal's end is an EXCLUSIVE instant, usually midnight at the start of the
day after the last day ("through Oct 31" is stored as Nov 1 00:00, give or take a few minutes of
slop) - verified 2026-10-01: Dutchie's own default list drops a deal the moment its end passes. So
``ends`` reports the last day the deal can run during store hours (end minus 8 h).

A feed that cannot be read RAISES ``DealsUnavailable``: "unreachable" is not "no deals", and the
caller must never treat a failed read as an authoritative empty list.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.core.cache import cache

from .dutchie import _pos_get, _store
from .models import STORES

logger = logging.getLogger(__name__)

LA = ZoneInfo("America/Los_Angeles")
CACHE_SECONDS = 600
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_KINDS = {"PERCENT_OFF": "percent", "PRICE_TO_AMOUNT": "price", "AMOUNT_OFF_TOTAL": "amount"}
_MANUAL_KINDS = {"percent": "percent", "price to amount": "price"}
_BUTTON = re.compile(r"\s*\b(?:manual\s+)?button\b", re.I)  # staff jargon in manual-deal names


class DealsUnavailable(Exception):
    """A store's discount feed could not be read (unknown - not 'no deals')."""


def _wall(utc_iso: str) -> datetime:
    """Dutchie UTC timestamp ('2026-10-01T07:00:00.0000000Z') -> naive Pacific wall clock."""
    return datetime.fromisoformat(utc_iso[:19]).replace(tzinfo=timezone.utc).astimezone(LA).replace(tzinfo=None)


def _value(raw, scale: int):
    """Dutchie's 0.3 -> 30 (percent) or 14.0 -> 14 (dollars); None when absent."""
    if raw is None:
        return None
    v = round(float(raw) * scale, 2)
    return int(v) if v == int(v) else v


def _days(has) -> list[str] | None:
    days = [d for d in _DAYS if has(d)]
    return days if 0 < len(days) < 7 else None  # none or all seven = every day


def _deal(store, id_, title, description, kind, value, days, start_time, end_time, start, end, source) -> dict:
    return {
        "id": id_, "store": store, "title": title, "description": description, "kind": kind,
        "value": value, "days": days, "start_time": (start_time or "")[:5] or None,
        "end_time": (end_time or "")[:5] or None, "starts": start.date().isoformat(),
        "ends": None if end.year >= 3000 else max(start, end - timedelta(hours=8)).date().isoformat(),
        "source": source,
    }


def _menu_deal(store: str, x: dict, now: datetime) -> dict | None:
    md, rw = x.get("menuDisplay") or {}, x.get("reward") or {}
    title = (x.get("onlineName") or md.get("menuDisplayName") or "").strip()
    start, end = _wall(x["validDateFrom"]), _wall(x["validDateTo"])
    if not title or not x.get("isActive") or not start <= now < end:
        return None
    kind = _KINDS.get(rw.get("calculationMethod"), "other")
    if rw.get("thresholdType") == "NUMBER_OF_ITEMS" and rw.get("applyToOnlyOneItem"):
        kind = "bogo"  # buy N, discount one item
    return _deal(store, x["id"], title, (md.get("menuDisplayDescription") or "").strip(), kind,
                 _value(rw.get("discountValue"), 100 if kind in ("percent", "bogo") else 1),
                 _days(lambda d: x.get(d)), x.get("startTime"), x.get("endTime"), start, end, "menu")


def _manual_deal(store: str, x: dict, now: datetime) -> dict | None:
    if x.get("applicationMethod") != "Manual" or not x.get("isAvailableOnline") or x.get("isDeleted"):
        return None
    title = " ".join(_BUTTON.sub("", x.get("discountName") or "").split())
    start, end = _wall(x["validFrom"]), _wall(x["validUntil"])
    if not title or not start <= now < end:
        return None
    w = x.get("weeklyRecurrenceInfo") or {}
    kind = _MANUAL_KINDS.get(str(x.get("discountType")).lower(), "other")
    return _deal(store, x["discountId"], title, "", kind,
                 _value(x.get("discountAmount"), 100 if kind == "percent" else 1),
                 _days(lambda d: w.get("appliesOn" + d.capitalize())), w.get("startTime"), w.get("endTime"),
                 start, end, "manual")


def current_deals(store_key: str, now: datetime | None = None) -> list[dict]:
    """Every deal running today on ``store_key``'s online menu. ``now`` = naive Pacific wall clock
    (tests). Raises ``DealsUnavailable`` when a feed cannot be read or has an unexpected shape."""
    key = _store(store_key).get("pos_key")
    if not key:
        raise DealsUnavailable("no Dutchie key configured")
    now = now or datetime.now(LA).replace(tzinfo=None)
    menu = _pos_get(key, "/discounts/v2/list", {"includeInactive": "true", "includeInclusionExclusionData": "true"})
    manual = _pos_get(key, "/reporting/discounts")
    if not isinstance(menu, list) or not isinstance(manual, list):
        raise DealsUnavailable("Dutchie discounts unreadable")
    try:
        deals = [_menu_deal(store_key, x, now) for x in menu] + [_manual_deal(store_key, x, now) for x in manual]
    except (KeyError, TypeError, ValueError) as e:
        raise DealsUnavailable(f"unexpected Dutchie record shape: {e!r}") from e
    return [d for d in deals if d]


def snapshot() -> dict:
    """The /api/v1/deals/ payload. Per-store lists are cached 10 minutes; an unreachable store is
    ``None`` (+ an ``errors`` entry) and is NEVER cached, so the next call retries it."""
    stores, errors = {}, {}
    for slug, _label in STORES:
        deals = cache.get(f"deals:v1:{slug}")
        if deals is None:
            try:
                deals = current_deals(slug)
            except DealsUnavailable as e:
                logger.warning("deals %s unreachable: %s", slug, e)
                stores[slug], errors[slug] = None, "unreachable"
                continue
            cache.set(f"deals:v1:{slug}", deals, CACHE_SECONDS)
        stores[slug] = deals
    return {"ok": not errors, "stores": stores, "errors": errors, "fetched_at": datetime.now(timezone.utc).isoformat()}


# ── tailoring: the same deals, the ones that speak to what this customer buys first ──
# Pure re-ordering of an already-fetched list (no deal is added, dropped or changed): a deal whose
# title/description names a ratio, format, extraction or category the customer buys (customer_model's
# `derived`) moves up. Low confidence / no derived -> the list as given.
_CAT_WORDS = {
    "flower": ("flower", "eighth", "eighths", "ounce", "oz"),
    "pre-rolls": ("pre-roll", "pre-rolls", "preroll", "prerolls", "joint", "joints", "blunt"),
    "vape-cartridges": ("vape", "vapes", "cart", "carts", "cartridge", "cartridges", "disposable"),
    "concentrates": ("concentrate", "concentrates", "dab", "dabs", "wax", "shatter", "rosin", "resin"),
    "edibles": ("edible", "edibles", "gummy", "gummies", "chocolate", "chocolates"),
    "beverages": ("drink", "drinks", "beverage", "beverages", "soda", "seltzer"),
    "tinctures": ("tincture", "tinctures"),
    "topicals": ("topical", "topicals", "balm", "lotion"),
}
_FORM_WORDS = {"gummy": ("gummy", "gummies"), "chocolate": ("chocolate", "chocolates"), "drink": ("drink", "drinks"),
               "tincture": ("tincture", "tinctures"), "capsule": ("capsule", "capsules"), "topical": ("topical",)}


def for_customer(deals: list[dict], derived: dict | None) -> list[dict]:
    """`deals` (current_deals / snapshot rows) re-ordered for one customer, stable within equal relevance."""
    if not isinstance(derived, dict) or derived.get("confidence") not in ("med", "high") or not deals:
        return list(deals or [])

    def words(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9:]+(?:-[a-z]+)?", text.lower()))

    cats = list(dict.fromkeys([*(derived.get("next_likely") or []), *(derived.get("price_by_cat") or {})]))
    ratios = {str(r) for r in derived.get("ratio_pref") or []}
    if (derived.get("cbd_lean") or 0) >= 0.5:
        ratios.add("cbd")
    forms = [set(_FORM_WORDS.get(f, (f,))) for f, share in (derived.get("forms") or {}).items() if share >= 0.3]
    phrases = [m.replace("-", " ") for m, share in (derived.get("extraction") or {}).items() if share >= 0.3]

    def score(d: dict) -> int:
        text = f"{d.get('title') or ''} {d.get('description') or ''}".lower()
        w = words(text)
        s = 3 * len(ratios & w) + 2 * sum(1 for f in forms if f & w) + 2 * sum(1 for ph in phrases if ph in text)
        s += sum(1 for c in cats if w & set(_CAT_WORDS.get(c, (c,))))
        return s

    ranked = sorted(enumerate(deals), key=lambda t: (-score(t[1]), t[0]))
    return [d for _, d in ranked]
