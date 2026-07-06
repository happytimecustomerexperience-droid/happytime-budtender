"""Budtender-facing customer summary — a one-glance read of who this shopper is.

Pure derivation from the real profile (Numbers-Guard: never invents a signal; omits what
it doesn't know). Segment is a light RFM read; the line strings together only the affinities
the customer actually has.
"""

from __future__ import annotations

import datetime

_TIER_LABEL = {"value": "budget", "mid": "mid-tier", "top": "premium"}


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _top(aff):
    if not isinstance(aff, dict) or not aff:
        return None
    return max(aff.items(), key=lambda kv: _f(kv[1]))[0]


def _recency_days(last_purchase):
    if not last_purchase:
        return None
    try:
        d = datetime.date.fromisoformat(str(last_purchase)[:10])
        return max(0, (datetime.date.today() - d).days)
    except (ValueError, TypeError):
        return None


def segment(orders, recency_days):
    if orders <= 1:
        return "New"
    if recency_days is not None and recency_days > 60:
        return "Lapsed"
    if orders >= 12 and (recency_days is None or recency_days <= 21):
        return "Champion"
    if orders >= 6:
        return "Loyal"
    return "Regular"


def summarize(profile):
    """-> {segment, line, chips, orders, recency_days} or None. `line` is a soft one-liner
    of real signals; unknown bits are dropped so it never reads as fabricated."""
    if not profile:
        return None
    orders = int(_f(profile.get("orders")))
    rec = _recency_days(profile.get("last_purchase"))
    seg = segment(orders, rec)

    bits = []
    st, cat = _top(profile.get("strain_type_affinity")), _top(profile.get("category_affinity"))
    taste = " ".join(x for x in (st or "", cat or "") if x).strip().lower()
    if taste:
        bits.append(f"usually {taste}")
    terp = _top(profile.get("terpene_affinity"))
    if terp:
        bits.append(f"{terp.lower()}-forward")
    lo, hi = profile.get("thc_min"), profile.get("thc_max")
    tier_label = _TIER_LABEL.get(profile.get("price_tier"))
    if lo is not None and hi is not None:
        bits.append(f"{round(_f(lo))}–{round(_f(hi))}% THC")
    elif tier_label:
        bits.append(tier_label)
    nov = profile.get("novelty_score")
    if nov is not None:
        bits.append("explorer" if _f(nov) >= 0.5 else "creature of habit")
    if rec is not None:
        bits.append("new today" if rec == 0 else f"last in {rec}d")

    chips = []
    if orders:
        chips.append(f"{orders} orders")
    if tier_label:
        chips.append(tier_label)
    return {"segment": seg, "line": " · ".join(bits), "chips": chips,
            "orders": orders, "recency_days": rec}
