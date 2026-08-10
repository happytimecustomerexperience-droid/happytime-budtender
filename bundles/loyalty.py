"""Loyalty balance by phone number, read live from the register.

THE PROGRAMME IS THE POS'S, NOT ALPINEIQ'S. Owner, 2026-08-05: "our loyalty is not
from alpineIQ." `GET /api/v1/loyalty/{uid}` returns an AlpineIQ object describing
nothing the register does, and two separate rounds of careful reasoning have already
been wasted deriving this ladder from it (see alpine-automations conf/account.toml).
Points come from Dutchie's own guest record and nowhere else.

The field is `LoyaltyPoints` on POST /api/v2/guest/details-light — 49 fields, versus
131 on /details, and the light row carries everything shown here. Verified live
against a real guest record: LoyaltyPoints, IsLoyaltyMember and LoyaltyTierName are
all present.
"""
from __future__ import annotations

import logging

from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import get_store, store_key

from . import customers

logger = logging.getLogger(__name__)

# The real ladder, owner-supplied 2026-08-05. `percent` is a PERCENTAGE OFF the
# basket at the register. It pays AT the steps only: a balance between two rungs
# redeems at the one BELOW it, so 500 points is the 450 rung (20%), never
# "nearly 25%". Below 125 it buys nothing, and this page must not imply otherwise.
TIERS = [(125, 10), (250, 15), (450, 20), (600, 25), (900, 30)]

# Every store a balance might be registered at. Dutchie's guest search is
# location-scoped, so someone who signed up at Pullman is invisible to a Yakima-only
# search — the number is theirs, the store is an accident of where they first shopped.
STORES = ("yakima", "mount-vernon", "pullman")


def percent_for(points: float) -> int:
    """What `points` redeems for, as a percentage off the basket. 0 below the first
    rung, because that is the honest answer — copy that names a points figure must
    also name what it is worth, or the reader has a number instead of an offer."""
    earned = [pct for need, pct in TIERS if points >= need]
    return earned[-1] if earned else 0


def next_tier(points: float) -> tuple[int, int] | None:
    """(points_needed, percent) of the next rung up, or None at the top."""
    for need, pct in TIERS:
        if points < need:
            return need - int(points), pct
    return None


def _details(location_slug: str, acct_id: str) -> dict:
    store = get_store(store_key(location_slug))
    data = PosRegisterClient(store).guest_details_light(int(acct_id)).get("Data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def balance_for_phone(phone: str) -> dict | None:
    """The balance behind a phone number, or None if there is no account.

    None is also what a broken register returns. That is deliberate and it is the
    security property: a distinguishable failure ("we couldn't reach the register"
    vs "no account") tells someone probing numbers which ones are real even while
    the lookup is broken. Same reasoning as `views.lookup_customer`.

    Returns ONLY what the page shows. The Dutchie guest row behind this carries DOB,
    address, email and full purchase history; naming the four fields here rather than
    passing the row through means a field added upstream cannot silently widen it.
    """
    for slug in STORES:
        try:
            acct_id, _name, status = customers.lookup_by_phone(slug, phone)
        except Exception:
            logger.warning("loyalty lookup unavailable at %s", slug, exc_info=True)
            continue
        if status != "matched" or not acct_id:
            continue
        try:
            row = _details(slug, acct_id)
        except Exception:
            logger.warning("loyalty details unavailable at %s", slug, exc_info=True)
            return None
        points = float(row.get("LoyaltyPoints") or 0)
        return {
            "points": int(points),
            "is_member": bool(row.get("IsLoyaltyMember")),
            "tier_name": (row.get("LoyaltyTierName") or "").strip(),
            "percent": percent_for(points),
            "next": next_tier(points),
        }
    return None
