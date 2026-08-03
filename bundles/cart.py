"""The shopper's cart on /custom-order.

The cart IS a `PhoneCartDraft` in `open` state, keyed by a token in a long-lived
cookie. That choice buys three things at once:

  * retention — the shopper closes the tab, comes back next week, cart intact
  * the POS already knows how to display and claim it (`_queue_panel`, `phone_cart_claim`)
  * one code path for "phone order" and "online order", so staff learn one thing

Prices and stock are re-read from live inventory on EVERY mutation and on every
render. A cart that sat in a cookie for a week is repriced before the shopper sees
it, and anything that sold out is flagged rather than silently carried to the
counter. The client never sends a price — only a product id and a quantity.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from budtender.models import PhoneCartDraft
from pos import catalog as pos_catalog

from . import resolver
from .catalog import store_key_for

logger = logging.getLogger(__name__)

COOKIE = "htco"
# Shape of PhoneCartDraft.draft_token: "pc-" + secrets.token_urlsafe(18).
_TOKEN_RE = re.compile(r"\Apc-[A-Za-z0-9_-]{16,61}\Z")
COOKIE_MAX_AGE = 60 * 60 * 24 * 30          # 30 days of retention
MAX_LINES = 30
MAX_QTY = 12
# Carts are abandoned constantly; don't leave them claimable forever.
OPEN_TTL_DAYS = 30


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def inventory_for(location_slug: str) -> list[dict]:
    try:
        return pos_catalog.get_inventory(store_key_for(location_slug))
    except Exception:
        logger.warning("cart: live inventory unavailable for %s", location_slug, exc_info=True)
        return []


def get_cart(request, location_slug: str, *, create: bool = False) -> PhoneCartDraft | None:
    """The shopper's open cart for this store, or None.

    Scoped by store on purpose: a Yakima cart must not follow someone to the
    Pullman page and quote them product that store doesn't carry.
    """
    # Exact match or nothing. The cookie IS the access control, so normalising it
    # (the old .strip()) meant " <token>" and "<token>\x00" both resolved to the
    # same cart — no bypass on its own, since you still need the 144-bit token, but
    # a bearer credential should not have fuzzy edges, and a NUL byte reaching a
    # text column raises DataError and 500s the page.
    raw = request.COOKIES.get(COOKIE) or ""
    token = raw if _TOKEN_RE.match(raw) else ""
    draft = None
    if token:
        draft = PhoneCartDraft.objects.filter(
            draft_token=token, status=PhoneCartDraft.Status.OPEN,
            location_slug=location_slug,
        ).first()
    if draft is None and create:
        draft = PhoneCartDraft.objects.create(
            location_slug=location_slug,
            source=PhoneCartDraft.Source.ONLINE,
            session_token="online",
            status=PhoneCartDraft.Status.OPEN,
            expires_at=timezone.now() + timedelta(days=OPEN_TTL_DAYS),
        )
    return draft


def attach_cookie(response, draft: PhoneCartDraft):
    response.set_cookie(
        COOKIE, draft.draft_token, max_age=COOKIE_MAX_AGE,
        httponly=True, samesite="Lax",
        secure=not getattr(settings, "DEBUG", False),
    )
    return response


def _line_for(item: dict, qty: int) -> dict:
    pub = resolver._public(item)
    return {
        "sku": pub["product_id"], "product_id": pub["product_id"],
        "name": pub["name"], "brand": pub["brand"], "category": pub["category"],
        "size": pub["size"], "image": pub["image"],
        "quantity": qty,
        "unit_price": pub["price"], "price_was": None, "discount_each": 0,
        "line_total": round(pub["price"] * qty, 2),
        "stock_on_hand": pub["qty"],
        "quote_source": "live_register",
    }


def reprice(draft: PhoneCartDraft, inventory: list[dict] | None = None) -> dict:
    """Re-resolve every line against live stock/price. Returns a render context.

    Out-of-stock lines are KEPT and flagged rather than deleted — silently removing
    something the shopper chose is worse than telling them it's gone.
    """
    inv = inventory if inventory is not None else inventory_for(draft.location_slug)
    lines, subtotal, issues = [], 0.0, 0
    for raw in (draft.lines or []):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("product_id") or "")
        qty = min(max(int(_f(raw.get("quantity"), 1)), 1), MAX_QTY)
        live = resolver.find_live(inv, pid) if inv else None
        if live and resolver.in_stock(live):
            line = _line_for(live, qty)
            available = int(_f(live.get("qty")))
            if qty > available:                    # partial: cap, don't drop
                line["quantity"] = available
                line["line_total"] = round(line["unit_price"] * available, 2)
                line["issue"] = "reduced"
                issues += 1
            line["in_stock"] = True
        else:
            line = dict(raw)
            line["in_stock"] = False
            line["issue"] = "sold_out"
            line["line_total"] = 0.0
            issues += 1
        lines.append(line)
        subtotal += line["line_total"]

    draft.lines = lines
    subtotal = round(subtotal, 2)
    quote = {
        "subtotal": subtotal,
        "discounts": 0.0,
        "total": subtotal,
        "currency": "USD",
        "source": "live_register" if inv else "unavailable",
        "generated_at": timezone.now().isoformat(),
        "final_total_note": "Register revalidates availability, discounts, taxes and final total.",
    }
    if draft.bundle_slug:
        from .catalog import get_bundle
        bundle = get_bundle(draft.bundle_slug)
        if bundle:
            quote["bundle"] = bundle.slug
            quote["bundle_name"] = bundle.name
            quote["bundle_discount_pct"] = bundle.discount_pct
    draft.quote = quote
    draft.save(update_fields=["lines", "quote", "updated_at"])
    return {
        "cart": draft, "lines": lines, "quote": quote,
        "count": sum(int(_f(x.get("quantity"), 0)) for x in lines if x.get("in_stock", True)),
        "issues": issues,
        "inventory_live": bool(inv),
    }


def add(draft: PhoneCartDraft, product_id: str, qty: int = 1,
        inventory: list[dict] | None = None) -> tuple[bool, str]:
    """Add or increment a line. Returns (ok, error_code)."""
    inv = inventory if inventory is not None else inventory_for(draft.location_slug)
    live = resolver.find_live(inv, product_id)
    if not live or not resolver.in_stock(live):
        return False, "not_in_stock"

    lines = [x for x in (draft.lines or []) if isinstance(x, dict)]
    for line in lines:
        if str(line.get("product_id")) == str(product_id):
            line["quantity"] = min(int(_f(line.get("quantity"), 1)) + qty, MAX_QTY)
            break
    else:
        if len(lines) >= MAX_LINES:
            return False, "cart_full"
        lines.append(_line_for(live, min(max(qty, 1), MAX_QTY)))
    draft.lines = lines
    draft.save(update_fields=["lines", "updated_at"])
    return True, ""


def set_qty(draft: PhoneCartDraft, product_id: str, qty: int) -> None:
    lines = [x for x in (draft.lines or []) if isinstance(x, dict)]
    if qty <= 0:
        lines = [x for x in lines if str(x.get("product_id")) != str(product_id)]
    else:
        for line in lines:
            if str(line.get("product_id")) == str(product_id):
                line["quantity"] = min(qty, MAX_QTY)
                break
    draft.lines = lines
    draft.save(update_fields=["lines", "updated_at"])


def remove(draft: PhoneCartDraft, product_id: str) -> None:
    set_qty(draft, product_id, 0)


def seed_from_bundle(draft: PhoneCartDraft, resolved: dict, bundle_slug: str) -> None:
    """Put an emailed bundle's resolved lines into an empty cart.

    Only seeds when the cart is empty — a returning shopper's own cart must never
    be overwritten by re-opening the email.
    """
    # Claim the bundle even when we don't seed. A shopper who added something
    # before opening the email still came from that bundle, and without the slug
    # the cart, checkout and success pages lose the "mention your X% at the
    # counter" line — so the budtender is never told which discount to apply and
    # the offer silently evaporates.
    if not draft.bundle_slug and bundle_slug:
        draft.bundle_slug = bundle_slug
        draft.save(update_fields=["bundle_slug", "updated_at"])

    if draft.lines:
        return
    lines = []
    for line in resolved.get("lines", []):
        product = line.product if hasattr(line, "product") else line.get("product")
        if not product:
            continue
        lines.append(_line_for_public(product, line.qty if hasattr(line, "qty") else 1))
    if not lines:
        return
    draft.lines = lines
    draft.bundle_slug = bundle_slug
    draft.save(update_fields=["lines", "bundle_slug", "updated_at"])


def _line_for_public(pub: dict, qty: int) -> dict:
    """Same shape as `_line_for`, from an already-projected public dict."""
    price = _f(pub.get("price"))
    return {
        "sku": pub.get("product_id", ""), "product_id": pub.get("product_id", ""),
        "name": pub.get("name", ""), "brand": pub.get("brand", ""),
        "category": pub.get("category", ""), "size": pub.get("size", ""),
        "image": pub.get("image", ""),
        "quantity": qty, "unit_price": price, "price_was": None, "discount_each": 0,
        "line_total": round(price * qty, 2),
        "stock_on_hand": int(_f(pub.get("qty"))),
        "quote_source": "live_register",
    }
