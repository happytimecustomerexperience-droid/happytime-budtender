"""Resolve an emailed bundle against what is actually on the shelf right now.

An email goes out on Monday; the shopper opens it Thursday. Half the advertised
SKUs may be gone. So nothing in the link is trusted for stock or price — every
line is re-resolved against the live register pull at render time
(`pos.catalog.get_inventory`), and anything sold out is replaced with the closest
real thing.

Substitution gates, hardest first:
  1. category  — a vape never substitutes for flower
  2. size      — for flower and pre-rolls; a 1g cart does not satisfy a 3.5g slot,
                 and crossing sizes silently breaks the bundle's discount math
Then ranked by price proximity, brand, strain type, terpene and effect overlap.
Brand is a RANKING signal, never a gate: the owner's rule is to widen to same
category + weight, any brand, rather than leave a slot empty.
"""
from __future__ import annotations

import logging

from django.conf import settings

from budtender.models import Product
from pos import catalog as pos_catalog

from .catalog import Bundle, canon_category

logger = logging.getLogger(__name__)

# The owner's rule: "if >1 is in stock we can propose it". Anything at exactly 1
# is one walk-in away from being gone before this shopper arrives.
MIN_STOCK = int(getattr(settings, "BUNDLE_MIN_STOCK", 2))

# A substitute more than this far from the original's price isn't the same offer.
PRICE_BAND = 0.30

OK = "ok"
SUBSTITUTED = "substituted"
UNAVAILABLE = "unavailable"


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _size_of(item: dict) -> str:
    return str(item.get("subcategory") or "").strip().lower()


def _grams(item: dict) -> float:
    return _f(item.get("unit_grams"))


def _jaccard(a, b) -> float:
    sa, sb = {str(x).lower() for x in (a or [])}, {str(x).lower() for x in (b or [])}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def in_stock(item: dict) -> bool:
    return _f(item.get("qty")) >= MIN_STOCK


def find_live(inventory: list[dict], sku: str) -> dict | None:
    """Match a URL sku against live rows by product_id, SerialNo or ProductNo."""
    s = str(sku).strip()
    if not s:
        return None
    for p in inventory:
        if str(p.get("product_id") or "") == s:
            return p
    for p in inventory:
        if str(p.get("SerialNo") or "") == s or str(p.get("package_id") or "") == s:
            return p
    return None


def advertised_identity(location_slug: str, sku: str) -> dict:
    """What the email actually advertised, for substitution matching when the SKU
    is no longer on the floor. Identity (name/brand/category/size) is legitimately
    DB-backed — it does not change. Stock and price never come from here."""
    p = (Product.objects
         .filter(location_slug=location_slug)
         .filter(sku=str(sku))
         .first()
         or Product.objects
         .filter(location_slug=location_slug, product_id=str(sku))
         .first())
    if not p:
        return {}
    return {
        "sku": p.sku,
        "product_id": p.product_id or "",
        "name": p.name,
        "brand": p.brand or "",
        "cat_key": canon_category(p.category or ""),
        "subcategory": (p.subcategory or "").lower(),
        "unit_grams": p.unit_weight,
        "potency_mg": p.potency_mg,
        "strain_type": p.strain_type or "",
        "terpene": p.dominant_terpene or "",
        "effects": p.effects or [],
        "flavors": p.flavors or [],
        "price": float(p.price or 0),
    }


def _score(cand: dict, target: dict) -> float:
    """How good a stand-in `cand` is for `target`. Higher is better."""
    score = 0.0
    t_price = _f(target.get("price"))
    c_price = _f(cand.get("price"))
    if t_price > 0 and c_price > 0:
        delta = abs(c_price - t_price) / t_price
        score += max(0.0, 1.0 - delta / PRICE_BAND) * 3.0
    if target.get("subcategory") and _size_of(cand) == str(target["subcategory"]).lower():
        score += 2.5
    if target.get("brand") and (cand.get("brand") or "").lower() == str(target["brand"]).lower():
        score += 2.0                       # tiebreak, never a gate
    if target.get("strain_type") and cand.get("strain_type") == target["strain_type"]:
        score += 1.5
    if target.get("terpene") and cand.get("terpene") == target["terpene"]:
        score += 1.0
    score += _jaccard(cand.get("effects"), target.get("effects")) * 1.0
    score += _jaccard(cand.get("flavors"), target.get("flavors")) * 0.5
    # Break ties toward what actually sells, so an empty taste profile still
    # produces a sensible pick rather than an arbitrary one.
    score += min(_f(cand.get("velocity")), 5.0) * 0.05
    return score


def candidates_for(inventory: list[dict], target: dict, *, slot=None,
                   exclude: set[str] | None = None) -> list[dict]:
    """Live, in-stock rows that could stand in for `target`, best first."""
    exclude = exclude or set()
    cat = canon_category(target.get("cat_key") or "")
    want_size = str(target.get("subcategory") or "").lower()
    want_grams = _f(target.get("unit_grams"))
    strict = bool(slot and slot.strict_size)

    out = []
    for p in inventory:
        if str(p.get("product_id") or "") in exclude:
            continue
        if not in_stock(p):
            continue
        if cat and canon_category(p.get("cat_key") or "") != cat:
            continue          # hard gate
        if slot and not slot.accepts(p):
            continue
        if strict:
            # Hard size gate. Prefer the labelled subcategory; fall back to grams
            # so a row missing enrichment isn't silently dropped from every slot.
            if want_size and _size_of(p):
                if _size_of(p) != want_size:
                    continue
            elif want_grams and _grams(p):
                if abs(_grams(p) - want_grams) > 0.01:
                    continue
        out.append(p)
    out.sort(key=lambda p: _score(p, target), reverse=True)
    return out


def _public(item: dict) -> dict:
    """Customer-safe projection of a live row.

    `pos.catalog._normalize` emits 38 keys including margin_pct, velocity,
    price_z, bucket and the register plumbing (BatchId/SerialNo/RecUnitPrice).
    None of that may reach a public page — allowlist, never blocklist.
    """
    return {
        "product_id": str(item.get("product_id") or ""),
        "name": item.get("name") or "",
        "brand": item.get("brand") or "",
        "category": item.get("cat_key") or "",
        "category_label": item.get("cat_label") or "",
        "size": item.get("subcategory") or "",
        "strain": item.get("strain") or "",
        "strain_type": item.get("strain_type") or "",
        "thc": _f(item.get("thc")) or None,
        "price": round(_f(item.get("price")), 2),
        "qty": int(_f(item.get("qty"))),
        "image": item.get("img") or item.get("image") or "",
        "image_static": bool(item.get("img_static")),
    }


class Line:
    """One bundle line after resolution."""

    def __init__(self, status: str, slot=None, product: dict | None = None,
                 requested: dict | None = None, qty: int = 1):
        self.status = status
        self.slot = slot
        self.product = product
        self.requested = requested or {}
        self.qty = qty

    @property
    def line_total(self) -> float:
        return round(_f((self.product or {}).get("price")) * self.qty, 2)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "slot": self.slot.key if self.slot else "",
            "slot_label": self.slot.label if self.slot else "",
            "qty": self.qty,
            "product": self.product,
            "replaces": {
                "name": self.requested.get("name", ""),
                "brand": self.requested.get("brand", ""),
            } if self.status in (SUBSTITUTED, UNAVAILABLE) and self.requested else None,
            "line_total": self.line_total,
        }


def resolve(bundle: Bundle, location_slug: str, items: list[tuple[str, int]],
            *, inventory: list[dict] | None = None) -> dict:
    """Resolve an emailed bundle against live inventory.

    Returns a dict of lines + pricing. Never raises on a missing SKU — an
    unresolvable line becomes an `unavailable` slot the shopper can fill from the
    search UI, which is far better than a 500 or a silently short bundle.
    """
    store_key = None
    if inventory is None:
        from .catalog import store_key_for
        store_key = store_key_for(location_slug)
        try:
            inventory = pos_catalog.get_inventory(store_key)
        except Exception:
            logger.warning("bundle: live inventory unavailable for %s", store_key, exc_info=True)
            inventory = []

    slots = list(bundle.slots)
    remaining = {s.key: s.qty for s in slots}
    by_key = {s.key: s for s in slots}
    lines: list[Line] = []
    used: set[str] = set()

    for sku, qty in items:
        live = find_live(inventory, sku)
        identity = advertised_identity(location_slug, sku)
        target = identity or (_slim(live) if live else {"sku": sku})

        slot = _slot_for(slots, remaining, live or identity)
        take = min(qty, remaining.get(slot.key, qty)) if slot else qty
        take = max(take, 1)

        if live and in_stock(live):
            lines.append(Line(OK, slot, _public(live), target, take))
            used.add(str(live.get("product_id") or ""))
        else:
            alt = next(iter(candidates_for(inventory, target, slot=slot, exclude=used)), None)
            if alt:
                lines.append(Line(SUBSTITUTED, slot, _public(alt), target, take))
                used.add(str(alt.get("product_id") or ""))
            else:
                lines.append(Line(UNAVAILABLE, slot, None, target, take))
        if slot:
            remaining[slot.key] = max(0, remaining.get(slot.key, 0) - take)

    # Slots the email never covered (or that fell through) still need showing, so
    # the shopper sees the whole promise rather than a quietly short bundle.
    for key, left in remaining.items():
        if left > 0:
            lines.append(Line(UNAVAILABLE, by_key[key], None, {}, left))

    subtotal = round(sum(line.line_total for line in lines), 2)
    discount = round(subtotal * bundle.discount_pct / 100.0, 2)
    return {
        "bundle": bundle,
        "lines": lines,
        "complete": all(line.status != UNAVAILABLE for line in lines),
        "subtotal": subtotal,
        "discount": discount,
        "total": round(subtotal - discount, 2),
        "substitutions": sum(1 for line in lines if line.status == SUBSTITUTED),
        "missing": sum(1 for line in lines if line.status == UNAVAILABLE),
        "inventory_live": bool(inventory),
    }


def _slim(live: dict) -> dict:
    return {
        "sku": str(live.get("product_id") or ""),
        "name": live.get("name") or "",
        "brand": live.get("brand") or "",
        "cat_key": canon_category(live.get("cat_key") or ""),
        "subcategory": _size_of(live),
        "unit_grams": live.get("unit_grams"),
        "strain_type": live.get("strain_type") or "",
        "terpene": live.get("terpene") or "",
        "effects": live.get("effects") or [],
        "flavors": live.get("flavors") or [],
        "price": _f(live.get("price")),
    }


def _slot_for(slots, remaining: dict, item: dict):
    """First slot with room that this item satisfies; else first with room."""
    if item:
        for s in slots:
            if remaining.get(s.key, 0) > 0 and s.accepts(_as_slot_input(item)):
                return s
    for s in slots:
        if remaining.get(s.key, 0) > 0:
            return s
    return slots[0] if slots else None


def _as_slot_input(item: dict) -> dict:
    return {
        "cat_key": item.get("cat_key") or item.get("category") or "",
        "subcategory": item.get("subcategory") or "",
    }
