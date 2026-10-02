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
import os
import re
import sys
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from budtender.models import PhoneCartDraft
from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import get_store
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

# Price confirmations are shared across shoppers for this long. Every checkout page,
# bundle landing and add-to-cart re-checks each line against the register, and the
# cart cookie is free to mint — without the cache one visitor could aim thousands of
# Dutchie calls at the register's API key.
PRICE_CHECK_TTL = 60


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


def reserved_units(location_slug: str) -> dict[str, int]:
    """product_id -> units already spoken for at this store by placed orders.

    Only a RELEASED order that has not expired holds stock: someone submitted
    checkout with a phone number and is driving in to collect it. A cart being
    browsed holds nothing — the cookie is free to mint, so a hold per cart let one
    visitor strip the shelf of everything. The shopper's own cart is OPEN, so it is
    never counted against itself.

    A CLAIMED order is already in the budtender's hands and its stock left the shelf
    at the register, so it must NOT be counted again here.
    """
    qs = (PhoneCartDraft.objects
          .filter(location_slug=location_slug, status=PhoneCartDraft.Status.RELEASED,
                  expires_at__gt=timezone.now())
          .only("lines"))

    held: dict[str, int] = {}
    for draft in qs:
        for line in (draft.lines or []):
            if not isinstance(line, dict):
                continue
            pid = str(line.get("product_id") or "")
            if not pid:
                continue
            held[pid] = held.get(pid, 0) + max(int(_f(line.get("quantity"), 0)), 0)
    return held


def available_after_holds(item: dict, held: dict[str, int]) -> int:
    """Units this shopper may actually take: what the register says, minus holds."""
    on_hand = int(_f(item.get("qty")))
    return max(on_hand - held.get(str(item.get("product_id") or ""), 0), 0)


def confirm_live_price(location_slug: str, item: dict) -> float | None:
    """This ONE item's price, straight from the register. None if we couldn't ask.

    `inventory_for` serves a shared snapshot the warmer refreshes every ~8 minutes —
    fine for browsing 4,700 products, wrong for the number someone is about to commit
    to. So at the moments that bind (add to cart, checkout) we do exactly what the POS
    does at `pos/views.py` `cart_add`: re-check THIS package serial against
    /api/v2/inventory/price-check.

    Best-effort by design. Dutchie being unreachable must never stop someone ordering;
    the caller keeps the snapshot price and the line records which it used.
    """
    # Same guard as budtender/live_stock and dutchie/stores: a unit test must never
    # reach the real register. Without it the suite makes one HTTP call per cart line
    # and every one of them times out.
    if "pytest" in sys.modules or os.environ.get("BUDTENDER_TESTING"):
        return None
    serial = str(item.get("SerialNo") or "").strip()
    if not serial:
        return None
    # Only answers are cached — "couldn't ask" is not a price and must be retried.
    key = f"bundle:pricecheck:{store_key_for(location_slug)}:{serial}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        store = get_store(store_key_for(location_slug))
        got = PosRegisterClient.parse_price_check(PosRegisterClient(store).price_check(serial))
    except Exception:
        logger.warning("cart: price-check failed for %s (keeping snapshot price)",
                       serial, exc_info=True)
        return None
    # 0.00 is a real answer (samples exist); only None means "no answer".
    price = got.get("price")
    if price is not None:
        cache.set(key, price, PRICE_CHECK_TTL)
    return price


def get_cart(request, location_slug: str, *, create: bool = False) -> PhoneCartDraft | None:
    """The shopper's open cart for this store, or None.

    Scoped by store on purpose: a Yakima cart must not follow someone to the
    Pullman page and quote them product that store doesn't carry.

    `create=True` only where a cart is actually being built (a verified bundle link,
    a real add-to-cart). Browsing GETs pass nothing, so a crawler mints no rows.
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


def attach_cookie(response, draft: PhoneCartDraft | None):
    if draft is not None:       # no cart yet, no cookie
        response.set_cookie(
            COOKIE, draft.draft_token, max_age=COOKIE_MAX_AGE,
            httponly=True, samesite="Lax",
            secure=not getattr(settings, "DEBUG", False),
        )
    return response


def _line_for(item: dict, qty: int, live_price: float | None = None) -> dict:
    """One cart line. `live_price`, when given, is a per-serial confirmation from the
    register and overrides the snapshot price."""
    pub = resolver._public(item)
    price = pub["price"] if live_price is None else round(float(live_price), 2)
    return {
        "sku": pub["product_id"], "product_id": pub["product_id"],
        "name": pub["name"], "brand": pub["brand"], "category": pub["category"],
        "size": pub["size"], "image": pub["image"],
        "image_is_category": pub["image_is_category"],
        "quantity": qty,
        "unit_price": price, "price_was": None, "discount_each": 0,
        "line_total": round(price * qty, 2),
        "stock_on_hand": pub["qty"],
        # Honest provenance. This said "live_register" for every line regardless, which
        # made an hour-old snapshot indistinguishable from a per-serial confirmation.
        "quote_source": "price_check" if live_price is not None else "menu_snapshot",
    }


def _quote_source(lines: list[dict], inv) -> str:
    if not inv:
        return "unavailable"
    priced = [x for x in lines if x.get("in_stock")]
    if priced and all(x.get("quote_source") == "price_check" for x in priced):
        return "price_check"
    return "menu_snapshot"


def reprice(draft: PhoneCartDraft, inventory: list[dict] | None = None,
            *, confirm: bool = False) -> dict:
    """Re-resolve every line against live stock/price. Returns a render context.

    Out-of-stock lines are KEPT and flagged rather than deleted — silently removing
    something the shopper chose is worse than telling them it's gone.

    `confirm=True` additionally re-checks each line's package serial against the
    register (`confirm_live_price`), which is what makes a price binding rather than a
    snapshot up to ~8 minutes old. Pass it wherever the number is about to become a
    commitment — the checkout form and the order write — and leave it off for browsing.
    Bounded by MAX_LINES, so at most 30 calls.
    """
    inv = inventory if inventory is not None else inventory_for(draft.location_slug)
    # What placed orders are holding right now. Clamping against raw shelf quantity
    # was what let twenty shoppers each be confirmed for two units.
    held = reserved_units(draft.location_slug)
    lines, subtotal, issues = [], 0.0, 0
    for raw in (draft.lines or []):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("product_id") or "")
        qty = min(max(int(_f(raw.get("quantity"), 1)), 1), MAX_QTY)
        live = resolver.find_live(inv, pid) if inv else None
        if live and resolver.in_stock(live):
            confirmed = confirm_live_price(draft.location_slug, live) if confirm else None
            line = _line_for(live, qty, confirmed)
            available = available_after_holds(live, held)
            line["stock_on_hand"] = available
            if available <= 0:                     # someone else got the last one
                line["in_stock"] = False
                line["issue"] = "sold_out"
                line["line_total"] = 0.0
                issues += 1
                lines.append(line)
                subtotal += line["line_total"]
                continue
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
        # Was a flat "live_register" whatever the truth. A cart is only price-confirmed
        # if EVERY in-stock line was; one fallback makes the whole quote a snapshot.
        "source": _quote_source(lines, inv),
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


def availability(location_slug: str, product_id: str, inv: list[dict]) -> tuple[dict | None, int]:
    """(live row, units a shopper could take right now); (None, 0) if it can't be sold.

    What is left once placed, unexpired orders are counted. Adding to a cart only ever
    CHECKS this — the units are reserved when the order is placed, not before.
    """
    live = resolver.find_live(inv, product_id)
    if not live or not resolver.in_stock(live):
        return None, 0
    return live, available_after_holds(live, reserved_units(location_slug))


def empty_ctx() -> dict:
    """The render context for a shopper who has no cart yet."""
    return {"cart": None, "lines": [], "quote": {}, "count": 0, "issues": 0}


def add(draft: PhoneCartDraft, product_id: str, qty: int = 1,
        inventory: list[dict] | None = None) -> tuple[bool, str]:
    """Add or increment a line. Returns (ok, error_code)."""
    inv = inventory if inventory is not None else inventory_for(draft.location_slug)
    # CLAMP rather than refuse: asking for 20 when 5 are free should give you 5, which
    # is the contract the rest of the cart already follows (reprice caps, it doesn't
    # drop). Only a genuinely empty shelf is a refusal.
    live, spare = availability(draft.location_slug, product_id, inv)
    if spare <= 0:
        return False, "not_in_stock"

    lines = [x for x in (draft.lines or []) if isinstance(x, dict)]
    for line in lines:
        if str(line.get("product_id")) == str(product_id):
            line["quantity"] = min(int(_f(line.get("quantity"), 1)) + qty, MAX_QTY, spare)
            break
    else:
        if len(lines) >= MAX_LINES:
            return False, "cart_full"
        # Confirm this serial against the register the moment it is chosen — the same
        # thing pos/views.py cart_add does for a walk-in, so the two paths price alike.
        lines.append(_line_for(live, min(max(qty, 1), MAX_QTY, spare),
                               confirm_live_price(draft.location_slug, live)))
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


def seed_from_bundle(draft: PhoneCartDraft, resolved: dict, bundle_slug: str,
                     recipient: str = "") -> None:
    """Put an emailed bundle's resolved lines into an empty cart.

    Only seeds when the cart is empty — a returning shopper's own cart must never
    be overwritten by re-opening the email.

    An empty `bundle_slug` seeds the lines without the offer (an expired link).
    `recipient` is the link's `c` token — the phone the link was sent to. It rides on
    `phone_hash` until checkout replaces it with the token of the phone actually
    given, and checkout only honours the bundle when the two agree.
    """
    # Claim the bundle even when we don't seed. A shopper who added something
    # before opening the email still came from that bundle, and without the slug
    # the cart, checkout and success pages lose the "mention your X% at the
    # counter" line — so the budtender is never told which discount to apply and
    # the offer silently evaporates.
    if not draft.bundle_slug and bundle_slug:
        draft.bundle_slug = bundle_slug
        draft.phone_hash = recipient
        draft.save(update_fields=["bundle_slug", "phone_hash", "updated_at"])

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
    if bundle_slug:
        draft.bundle_slug, draft.phone_hash = bundle_slug, recipient
    draft.save(update_fields=["lines", "bundle_slug", "phone_hash", "updated_at"])


def _line_for_public(pub: dict, qty: int) -> dict:
    """Same shape as `_line_for`, from an already-projected public dict."""
    price = _f(pub.get("price"))
    return {
        "sku": pub.get("product_id", ""), "product_id": pub.get("product_id", ""),
        "name": pub.get("name", ""), "brand": pub.get("brand", ""),
        "category": pub.get("category", ""), "size": pub.get("size", ""),
        "image": pub.get("image", ""),
        "image_is_category": bool(pub.get("image_is_category")),
        "quantity": qty, "unit_price": price, "price_was": None, "discount_each": 0,
        "line_total": round(price * qty, 2),
        "stock_on_hand": int(_f(pub.get("qty"))),
        "quote_source": "live_register",
    }
