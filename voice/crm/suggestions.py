"""Personalized suggestion feed for a customer profile (P6) — a lean port of the analytics
``customerIntelligence`` engine (favorite replenish / basket cross-sell / tier upgrade / cold
start), each with a prebuilt speakable ``reason`` sentence.

This is the STAFF-facing feed shown on the customer profile page (and the shape the budtender
persona's live picks mirror). It does NOT filter to live inventory — that happens at call time via
the budtender service (``suggest_products`` is the in-stock, leak-safe path). Here we surface what
the history says the customer likes, so staff can see "what we'd lead with" for a returning caller.
"""

from __future__ import annotations

# Confidence gates on order count — a one-time buyer's "favorite" is a weak signal (cold-start aware).
_HIGH, _MED = 6, 2


def _num(v) -> float:
    """Coerce a possibly-string POS value to a number (0.0 on failure) — favorites/lift feed into
    arithmetic and a raw "6" from the export would otherwise TypeError and crash the render."""
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _confidence(orders: int) -> str:
    if orders >= _HIGH:
        return "high"
    if orders >= _MED:
        return "medium"
    return "low"


def build_feed(profile, *, baskets_index: dict | None = None, limit: int = 6) -> list[dict]:
    """Return up to ``limit`` suggestions for a CustomerProfile, highest-score first. Each is
    ``{kind, title, reason, score, confidence}``. ``baskets_index`` is the optional
    ``frequentlyBoughtWith`` map (product → [pairs]) from ``baskets.json`` for cross-sell."""
    feed: list[dict] = []
    conf = _confidence(profile.orders or 0)

    # 1) Replenish favorites — the strongest signal for a returning buyer.
    for fav in (profile.favorites or [])[:3]:
        name = fav.get("product") or fav.get("Product Name") or fav.get("name") or ""
        if not name:
            continue
        units = _num(fav.get("units") or fav.get("Units"))
        feed.append({
            "kind": "favorite",
            "title": name,
            "reason": f"A usual favorite — bought {units:g} unit(s) historically. Lead with a restock.",
            "score": 100 + units,
            "confidence": conf,
        })

    # 2) Cross-sell from the basket index (frequently-bought-with the top favorite).
    if baskets_index and profile.favorites:
        anchor = (profile.favorites[0].get("product")
                  or profile.favorites[0].get("Product Name") or "")
        for pair in (baskets_index.get(anchor) or [])[:2]:
            with_name = pair.get("with") or pair.get("Product") or ""
            lift = pair.get("lift") or pair.get("Lift") or 0
            if not with_name:
                continue
            feed.append({
                "kind": "pair",
                "title": with_name,
                "reason": f"Frequently bought with {anchor} (lift {lift}). Natural add-on.",
                "score": 65 + float(lift or 0),
                "confidence": conf,
            })

    # 3) Tier upgrade — a category they shop at Core/Middle → offer the Top/Profit tier.
    for cat, tier in (profile.tier_by_category or {}).items():
        if str(tier).lower() in ("middle", "core", "bottom"):
            feed.append({
                "kind": "profit_upgrade",
                "title": f"Premium {cat}",
                "reason": f"Shops {cat} at the {tier} tier — a premium {cat} is an easy step up.",
                "score": 45,
                "confidence": conf,
            })
            break

    # 4) Cold start — no favorites/history → lean on top categories / persona.
    if not feed:
        cats = profile.top_categories or []
        if cats:
            top = cats[0].get("category") or cats[0].get("Category") or "staff picks"
            feed.append({
                "kind": "cold_start",
                "title": f"Popular {top}",
                "reason": f"New or light history — start with our staff-favorite {top}.",
                "score": 30,
                "confidence": "low",
            })
        elif profile.persona:
            feed.append({
                "kind": "cold_start",
                "title": f"{profile.persona} picks",
                "reason": f"Matches the {profile.persona} profile — lead with that group's favorites.",
                "score": 25,
                "confidence": "low",
            })

    feed.sort(key=lambda s: s["score"], reverse=True)
    return feed[:limit]
