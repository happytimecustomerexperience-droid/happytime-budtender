"""Loyalty balance by phone number, read live from the register.

THE PROGRAMME IS THE POS'S, NOT ALPINEIQ'S. Owner, 2026-08-05: "our loyalty is not
from alpineIQ." `GET /api/v1/loyalty/{uid}` returns an AlpineIQ object describing
nothing the register does, and two separate rounds of careful reasoning have already
been wasted deriving this ladder from it (see alpine-automations conf/account.toml).
Points come from Dutchie's own guest record and nowhere else.

The field is `LoyaltyPoints` on POST /api/v2/guest/details-light — 49 fields, versus
131 on /details, and the light row carries everything shown here.

MEASURED, not assumed (40 real customers, 2026-08-10): 7 of 40 hold a balance, the
largest 1095.32, and they are fractional. `IsLoyaltyMember` was False for all forty,
the 1095-point holder included, so it is NOT a membership flag and nothing here may
gate on it — see the note in `balance_for_phone`. The REST API's own
`isLoyaltyMember` is the mirror-image trap: True for all 29,762 customers, and that
row carries no points field at all.
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


def balance_for_phone(phone: str) -> tuple[str, dict | None]:
    """(state, balance) where state is found / none / unavailable.

    THREE outcomes, not two, and the third is the point. "We have no account for
    that number" and "we could not reach the register" are different facts, and a
    register outage must never tell a fifteen-year customer they do not exist —
    they would go and re-register, splitting their points across two accounts.

    Owner asked (2026-08-10) for a plain "that number isn't registered". That does
    make the page a membership oracle, which the earlier symmetric version avoided;
    the throttle is what stops enumeration now (5/min, 30/hour per IP), and it is
    doing the work it was always the real control for.

    `none` requires a CLEAN "no" from every store: Dutchie answered, and no guest
    matched. One store erroring downgrades the whole answer to `unavailable`,
    because a number registered at the store we failed to reach is not absent.

    Returns ONLY what the page shows. The Dutchie guest row behind this carries DOB,
    address, email and full purchase history; naming the four fields here rather than
    passing the row through means a field added upstream cannot silently widen it.
    """
    every_store_answered = True
    for slug in STORES:
        try:
            acct_id, _name, status = customers.lookup_by_phone(slug, phone)
        except Exception:
            logger.warning("loyalty lookup unavailable at %s", slug, exc_info=True)
            every_store_answered = False
            continue
        if status == "unresolved":       # lookup_by_phone swallowed its own error
            every_store_answered = False
            continue
        if status != "matched" or not acct_id:
            continue
        try:
            row = _details(slug, acct_id)
        except Exception:
            logger.warning("loyalty details unavailable at %s", slug, exc_info=True)
            return "unavailable", None
        points = float(row.get("LoyaltyPoints") or 0)
        # `IsLoyaltyMember` IS DELIBERATELY NOT READ. Measured against 40 real
        # customers on 2026-08-10: it came back False for every single one —
        # including the person holding 1095.32 points. Whatever it tracks, it is not
        # "has a loyalty balance", and gating this page on it would show nothing to
        # everyone. Same for the REST API's `isLoyaltyMember`, which is True for all
        # 29,762 customers and equally useless. LoyaltyPoints is the only field here
        # that carries a fact.
        return "found", {
            "points": int(points),          # balances are fractional; the counter rounds down
            "tier_name": (row.get("LoyaltyTierName") or "").strip(),
            "percent": percent_for(points),
            "next": next_tier(points),
        }
    return ("none" if every_store_answered else "unavailable"), None
