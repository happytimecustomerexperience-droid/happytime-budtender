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
