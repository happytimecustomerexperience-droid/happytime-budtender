"""Next-best-item suggestions for POS.

Returns customer-safe, explainable recommendation rows with a stable contract:
recommendation_type, why_this, score, confidence, fresh, exclude_skus, mode.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .imagemap import category_key


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _cat_keys(*values):
    out = set()
    for value in values:
        text = str(value or "").strip()
        if text:
            out.add((category_key(text) or text).lower())
    return out


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


def _rtype(mode, kind):
    if mode == "usual":
        return "usual" if kind in {"fresh_favorite", "favorite"} else kind
    if mode == "surprise" and kind == "similar":
        return "adjacent_new"
    return {
        "fresh_favorite": "usual",
        "favorite": "usual",
        "similar": "similar",
        "profit_upgrade": "profit_step_up",
    }.get(kind, kind)


def _why(p, kind, fresh, profile):
    brand = p.get("brand") or ""
    cat = p.get("category") or p.get("cat_key") or ""
    sub = p.get("subcategory") or ""
    name = p.get("name") or p.get("product_name") or "this pick"
    if kind == "fresh_favorite":
        return f"Your usual {brand} pick, back in fresh stock · {name}."
    if kind == "favorite":
        return f"A familiar one you already like · {name}."
    if kind == "similar":
        lane = sub or cat or "that lane"
        if profile and profile.get("price_tier") == "value":
            return f"Right in your value lane · {name}."
        return f"Right in your {lane} lane · {name}."
    if kind == "profit_upgrade":
        return f"A little upgrade that still fits your taste · {name}."
    if fresh:
        return f"Fresh batch, same lane · {name}."
    return f"A solid in-stock option · {name}."


def suggest(inventory, profile, limit=8, *, exclude_skus=None, mode="default"):
    """Rank next-best items for a customer over in-stock live inventory."""
    if not profile:
        return []
    stock = [p for p in inventory if _f(p.get("qty")) > 0]
    if not stock:
        return []
    exclude = {str(s) for s in (exclude_skus or []) if s}
    stock = [p for p in stock if str(p.get("sku") or p.get("product_id") or p.get("ProductId") or "") not in exclude]
    if not stock:
        return []
    by_pid = {}
    for p in stock:
        for pid in (p.get("product_id"), p.get("ProductId"), p.get("sku")):
            if pid:
                by_pid.setdefault(str(pid), p)

    hist = profile.get("purchase_history") or []
    cat_aff = profile.get("category_affinity") or {}
    brand_aff = profile.get("brand_affinity") or {}
    sub_aff = profile.get("subcategory_affinity") or {}
    top_cats = {key for c, v in cat_aff.items() if _f(v) > 0 for key in _cat_keys(c)}
    top_brands = {b for b, v in brand_aff.items() if _f(v) >= 0.25}
    top_subs = {s for s, v in sub_aff.items() if _f(v) >= 0.25}
    fav_strains = {_norm(h.get("strain")) for h in hist if isinstance(h, dict) and h.get("strain")}
    fav_strains.discard("")

    out = {}  # product_id -> best suggestion

    def add(p, typ, score, reason, orders=0):
        pid = str(p.get("product_id") or p.get("ProductId") or p.get("sku"))
        s = dict(p)
        fresh = _is_fresh(p)
        rec_type = _rtype(mode, typ)
        s.update(
            type=typ,
            recommendation_type=rec_type,
            score=round(score, 1),
            why=reason,
            why_this=reason,
            confidence=_confidence(orders, score),
            fresh=fresh,
            exclude_skus=sorted(exclude),
            mode=mode,
        )
        if pid not in out or score > out[pid]["score"]:
            out[pid] = s

    for h in hist:
        if not isinstance(h, dict):
            continue
        p = next((by_pid.get(str(h.get(k))) for k in ("product_id", "ProductId", "sku") if h.get(k)), None)
        if not p:
            continue
        orders = int(_f(h.get("times_bought"), 1))
        fresh = _is_fresh(p)
        score = 100 + (35 if fresh else 0) + _f(p.get("qty")) + _f(p.get("velocity")) * 5
        add(p, "fresh_favorite" if fresh else "favorite", score,
            _why(p, "fresh_favorite" if fresh else "favorite", fresh, profile), orders)

    for p in stock:
        cat_match = bool(_cat_keys(p.get("category"), p.get("cat_key")) & top_cats)
        brand_match = p.get("brand") in top_brands
        strain_match = _norm(p.get("strain")) in fav_strains and bool(p.get("strain"))
        sub_match = p.get("subcategory") in top_subs
        if not (cat_match or brand_match or strain_match or sub_match):
            continue
        score = ((22 if cat_match else 0) + (20 if brand_match else 0) + (25 if strain_match else 0)
                 + (14 if sub_match else 0) + 8 + (8 if _is_fresh(p) else 0) + _f(p.get("velocity")) * 3)
        if score < 25:
            continue
        name = p.get("name") or p.get("product_name") or "this pick"
        if strain_match:
            reason = f"Matches a favorite strain ({p.get('strain')}) · {name}."
        elif brand_match:
            reason = f"Matches a brand you usually buy · {name}."
        elif sub_match:
            reason = f"Same subcategory you already buy: {p.get('subcategory')} · {name}."
        else:
            reason = f"In one of your top categories · {name}."
        add(p, "similar", score, reason, int(_f(profile.get("orders") or profile.get("total_orders"))))

    core_cats = {
        key
        for h in hist
        if isinstance(h, dict) and (h.get("bucket") == "core")
        for key in _cat_keys(h.get("category"), h.get("cat_key"))
    }
    for p in stock:
        if p.get("bucket") != "profit" or not (_cat_keys(p.get("category"), p.get("cat_key")) & core_cats):
            continue
        score = 72 + (8 if _is_fresh(p) else 0) + _f(p.get("velocity")) * 2
        add(p, "profit_upgrade", score, _why(p, "profit_upgrade", _is_fresh(p), profile))

    ranked = sorted(out.values(), key=lambda s: s["score"], reverse=True)
    return ranked[:limit]
