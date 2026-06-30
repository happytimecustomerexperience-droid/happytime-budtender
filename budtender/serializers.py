"""
Client-facing serialization.

CRITICAL: we build the public product dict from an explicit ALLOWLIST. `cost`
and `margin` are never referenced here, so they can never reach the website or
browser. A regression test (tests/test_no_leak.py) enforces this.
"""
from decimal import Decimal

from .models import Product

# The only fields the website/browser may ever see for a product.
PUBLIC_PRODUCT_FIELDS = (
    "rank", "sku", "name", "brand", "strain", "price", "price_was",
    "thc_percent", "dominant_terpene", "stock_on_hand", "dutchie_link",
    "image_url", "why_this",
)


def _num(v):
    return float(v) if isinstance(v, Decimal) else v


def public_product(p: Product, rank: int = 1, why_this: str | None = None) -> dict:
    """Map a Product to the website's SearchResultPublic shape (NO cost/margin)."""
    return {
        "rank": rank,
        "sku": p.sku,
        "name": p.name,
        "brand": p.brand or "",
        "strain": p.strain or None,
        "price": _num(p.price) or 0,
        "price_was": _num(p.price_was) if p.price_was else None,
        "thc_percent": p.thc_percent,
        "dominant_terpene": p.dominant_terpene or None,
        "stock_on_hand": p.quantity_on_hand,
        "dutchie_link": f"/catalog/product/{p.slug}" if p.slug else "/catalog",
        "image_url": p.image_url or None,
        "why_this": why_this,
    }


def public_message(m) -> dict:
    return {"id": str(m.id), "role": m.role, "content": m.content, "chips": m.chips, "ts": int(m.ts.timestamp() * 1000)}


def profile_summary(profile) -> dict:
    """Generic, non-PII profile hints for the website (no raw purchase history)."""
    if not profile:
        return {"has_history": False, "top_categories": [], "price_tier": ""}
    cats = sorted(profile.category_affinity.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "has_history": profile.total_orders > 0,
        "top_categories": [c for c, _ in cats[:3]],
        "price_tier": profile.price_tier or "",
    }


# ── Staff-facing customer profile (P7) — for the voice dashboard's Customers browse ──
# Built from an explicit allowlist of customer-facing aggregates. CustomerProfile has NO
# cost/margin field; purchase_history carries retail last_price/price_z (customer-facing), never
# cost. A regression test (tests/test_no_leak.py) asserts customer_detail emits no cost/margin.

def _top_categories(profile, n=4) -> list[dict]:
    cats = sorted((profile.category_affinity or {}).items(), key=lambda kv: kv[1], reverse=True)
    return [{"category": c, "share": round(float(w), 3)} for c, w in cats[:n]]


def customer_row(profile) -> dict:
    """One row for the customer list (no raw purchase history — kept light for the roster)."""
    return {
        "id": profile.id,  # opaque key for the detail link (so a phone never lands in a URL)
        "phone": profile.phone,
        "name": profile.name or "",
        "total_orders": profile.total_orders,
        "last_purchase_at": profile.last_purchase_at.isoformat() if profile.last_purchase_at else "",
        "price_tier": profile.price_tier or "",
        "novelty_score": round(float(profile.novelty_score or 0), 3),
        "top_categories": _top_categories(profile, 3),
        "computed_at": profile.computed_at.isoformat() if profile.computed_at else "",
    }


def customer_detail(profile) -> dict:
    """Full staff profile: the row + affinity maps + bucket mix + favorite products (top items by
    purchase count, name-joined) + thc band. Leak-safe (no cost/margin)."""
    hist = profile.purchase_history or []
    favs = sorted(hist, key=lambda h: int(h.get("times_bought", 0) or 0), reverse=True)[:10]
    # Join sku → product name for friendly favorites (purchase_history stores sku/ids, not names).
    skus = [h.get("sku") for h in favs if h.get("sku")]
    name_by_sku = dict(
        Product.objects.filter(sku__in=skus).exclude(name="").values_list("sku", "name")
    ) if skus else {}
    favorites = [
        {
            "product": name_by_sku.get(h.get("sku")) or h.get("sku") or h.get("product_id") or "",
            "brand": h.get("brand", ""),
            "category": h.get("category", ""),
            "units": int(h.get("times_bought", 0) or 0),
            "last_bought_at": h.get("last_bought_at") or "",
        }
        for h in favs
    ]
    brands = sorted((profile.brand_affinity or {}).items(), key=lambda kv: kv[1], reverse=True)
    return {
        **customer_row(profile),
        "brand_affinity": profile.brand_affinity or {},
        "category_affinity": profile.category_affinity or {},
        "strain_type_affinity": profile.strain_type_affinity or {},
        "subcategory_affinity": profile.subcategory_affinity or {},
        "bucket_mix": profile.bucket_mix or {},
        "top_brand": brands[0][0] if brands else "",
        "favorites": favorites,
        "purchase_count": len(hist),
        "thc_min": profile.thc_min,
        "thc_max": profile.thc_max,
    }
