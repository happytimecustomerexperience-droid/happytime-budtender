"""Next-best-item suggestions — ported from the reference dashboard's 5-pass
lib/customerIntelligence.ts logic, adapted to our enriched live inventory + the
happytime purchase_history. Passes: fresh_favorite / favorite / similar /
profit_upgrade (basket-pairs need co-purchase data we don't have here — omitted).

Input: enriched product dicts (same shape as ranking.py) + profile dict.
Output: ranked list of dicts with type/score/confidence/why + the product fields.
"""

from __future__ import annotations

import re
from datetime import date, datetime


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _fresh_days(p):
    rd = p.get("received_date")
    if not rd:
        return None
    try:
        d = datetime.fromisoformat(str(rd)[:19]).date()
        return (date.today() - d).days
    except (ValueError, TypeError):
        return None


def _is_fresh(p):
    days = _fresh_days(p)
    return days is not None and 0 <= days <= 14


def _confidence(orders, score):
    if orders >= 3 and score >= 70:
        return "high"
    if orders >= 2 and score >= 45:
        return "medium"
    return "low"


def suggest(inventory, profile, limit=8):
    """Rank next-best items for a customer over in-stock live inventory."""
    if not profile:
        return []
    stock = [p for p in inventory if _f(p.get("qty")) > 0]
    if not stock:
        return []
    by_pid = {}
    for p in stock:
        pid = p.get("product_id")
        if pid is not None:
            by_pid.setdefault(str(pid), p)

    hist = profile.get("purchase_history") or []
    cat_aff = profile.get("category_affinity") or {}
    brand_aff = profile.get("brand_affinity") or {}
    top_cats = {c for c, v in cat_aff.items() if _f(v) > 0}
    top_brand = max(brand_aff, key=lambda k: _f(brand_aff[k])) if brand_aff else None
    fav_strains = {_norm(h.get("strain")) for h in hist if isinstance(h, dict) and h.get("strain")}
    fav_strains.discard("")

    out = {}  # product_id -> best suggestion

    def add(p, typ, score, reason, orders=0):
        pid = str(p.get("product_id"))
        s = dict(p)
        s.update(type=typ, score=round(score, 1), why=reason,
                 confidence=_confidence(orders, score), fresh=_is_fresh(p))
        if pid not in out or score > out[pid]["score"]:
            out[pid] = s

    # Pass 1 — favorites (exact re-buys in stock), fresh boosted.
    for h in hist:
        if not isinstance(h, dict):
            continue
        p = by_pid.get(str(h.get("product_id")))
        if not p:
            continue
        orders = int(_f(h.get("times_bought"), 1))
        fresh = _is_fresh(p)
        score = 100 + (35 if fresh else 0) + _f(p.get("qty")) + _f(p.get("velocity")) * 5
        reason = ("Fresh batch in for a usual favorite." if fresh
                  else "A usual favorite, in stock now.")
        add(p, "fresh_favorite" if fresh else "favorite", score, reason, orders)

    # Pass 2 — similar (category/brand/strain match they haven't necessarily bought).
    for p in stock:
        cat_match = p.get("category") in top_cats or p.get("cat_key") in top_cats
        brand_match = top_brand and p.get("brand") == top_brand
        strain_match = _norm(p.get("strain")) in fav_strains and bool(p.get("strain"))
        if not (cat_match or brand_match or strain_match):
            continue
        score = ((22 if cat_match else 0) + (20 if brand_match else 0) + (25 if strain_match else 0)
                 + 8 + (8 if _is_fresh(p) else 0) + _f(p.get("velocity")) * 3)
        if score < 25:
            continue
        reason = (f"Matches a favorite strain ({p.get('strain')})." if strain_match
                  else "Matches their usual brand." if brand_match
                  else "In a top category for them.")
        add(p, "similar", score, reason, int(_f(profile.get("orders"))))

    # Pass 3 — profit upgrade (Profit-bucket step-up in a category they buy Core in).
    core_cats = {h.get("category") for h in hist if isinstance(h, dict)
                 and (h.get("bucket") == "core")}
    for p in stock:
        if p.get("bucket") != "profit" or (
                p.get("category") not in core_cats and p.get("cat_key") not in core_cats):
            continue
        score = 72 + (8 if _is_fresh(p) else 0) + _f(p.get("velocity")) * 2
        add(p, "profit_upgrade", score, f"A premium step-up in {p.get('category')}.")

    ranked = sorted(out.values(), key=lambda s: s["score"], reverse=True)
    return ranked[:limit]
