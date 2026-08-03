"""Public /custom-order — our own menu, our own cart, order lands in the POS.

These are the ONLY unauthenticated HTML views in this app. Auth here is per-view
(`@login_required` everywhere else), so a new view is public by omission —
everything rendered goes through `resolver._public`, never a raw `pos.catalog`
row, which carries margin_pct / velocity / price_z / bucket and register plumbing.

There is no Dutchie embed. The shopper browses OUR menu (same live inventory and
the same filters the POS uses), builds a full cart, and checks out with name +
phone + optional email. That becomes a `PhoneCartDraft` a budtender claims at the
register — no Dutchie sign-in, and no dependency on an embedded cart we cannot
write to (see docs/custom-order-bundles.md).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from budtender.models import PhoneCartDraft
from pos import catalog as pos_catalog
from pos_core.ratelimit import _client_ip, rate_limit

from . import cart as cart_mod
from . import calibration, customers, emails, resolver, signing
from .catalog import STORE_ADDRESS, get_bundle, store_label

logger = logging.getLogger(__name__)

PAGE_SIZE = 24
DRAFT_TTL_HOURS = int(getattr(settings, "BUNDLE_DRAFT_TTL_HOURS", 4))
MAX_ORDER_TOTAL = float(getattr(settings, "BUNDLE_MAX_ORDER_TOTAL", 300))
DEFAULT_STORE = "yakima"

# [^0-9] rather than \D: \D is Unicode-aware, so Arabic-Indic digits ("٥٠٩...")
# survive the strip, pass the 10-character length check, and get stored as a
# phone number nobody can dial — with phone_last4 in a script staff can't read.
_PHONE_RE = re.compile(r"[^0-9]+")
# Postgres text columns reject NUL, so a %00 anywhere in the form crashes the
# INSERT with a 500 *after* the shopper thinks they've ordered. Strip the whole
# C0 control range: none of it is meaningful in a name or an email.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _store_from(request) -> str:
    store = (request.GET.get("loc") or request.POST.get("loc") or "").strip()[:32]
    if store in STORE_ADDRESS:
        return store
    return DEFAULT_STORE


def _filters(request) -> dict:
    q = request.GET
    return {
        "q": (q.get("q") or "").strip()[:80],
        "cat": (q.get("cat") or "").strip()[:32],
        "subcat": (q.get("subcat") or "").strip()[:32],
        "brand": (q.get("brand") or "").strip()[:64],
        "brand_q": (q.get("brand_q") or "").strip()[:64],
        "strain_type": (q.get("strain_type") or "").strip()[:16],
        "effect": (q.get("effect") or "").strip()[:32],
        "price_min": _int(q.get("price_min")),
        "price_max": _int(q.get("price_max")),
        "thc_min": _int(q.get("thc_min")),
        "doh_only": q.get("doh_only") in ("1", "true", "on"),
        "sort": (q.get("sort") or "popular").strip()[:16],
    }


def _in_stock(inventory: list[dict]) -> list[dict]:
    return [p for p in inventory if resolver.in_stock(p)]


def _shell(request, store: str, ctx: dict) -> dict:
    """Context every page of the storefront needs."""
    return {
        "store": store,
        "store_label": store_label(store),
        "store_address": STORE_ADDRESS.get(store, ""),
        "stores": [{"slug": s, "label": store_label(s)} for s in STORE_ADDRESS],
        **ctx,
    }


# ── the emailed bundle ───────────────────────────────────────────────────────
@require_GET
def landing(request):
    """GET /custom-order?b=&loc=&i=&exp=&sig= — the emailed bundle, live-resolved.

    Also seeds the shopper's cart, so "add bundle to cart" is genuinely one tap:
    they land already holding the items, and can browse for more.
    """
    try:
        req = signing.parse(request.GET)
    except signing.BundleUrlError as exc:
        return render(request, "bundles/invalid.html",
                      {"reason": str(exc), "store": DEFAULT_STORE}, status=400)

    bundle = get_bundle(req.bundle)
    if not bundle:
        raise Http404("unknown bundle")
    if req.store not in STORE_ADDRESS:
        raise Http404("unknown store")

    inventory = cart_mod.inventory_for(req.store)
    result = resolver.resolve(bundle, req.store, req.items, inventory=inventory)
    lines = [line.as_dict() for line in result["lines"]]

    draft = cart_mod.get_cart(request, req.store, create=True)
    cart_mod.seed_from_bundle(draft, result, bundle.slug)
    cart_ctx = cart_mod.reprice(draft, inventory)

    response = render(request, "bundles/landing.html", _shell(request, req.store, {
        "bundle": bundle,
        "lines": lines,
        "result": result,
        "expired": req.expired,
        "cart_ctx": cart_ctx,
        "draft_ttl": DRAFT_TTL_HOURS,
    }))
    return cart_mod.attach_cookie(response, draft)


# ── the menu ─────────────────────────────────────────────────────────────────
@require_GET
def menu(request):
    """GET /custom-order/menu — the full public storefront.

    Same live inventory and the same `pos.catalog.query` filters the in-store menu
    uses, so staff and shoppers see one truth.
    """
    store = _store_from(request)
    inventory = cart_mod.inventory_for(store)
    sellable = _in_stock(inventory)
    draft = cart_mod.get_cart(request, store, create=True)
    cart_ctx = cart_mod.reprice(draft, inventory)

    response = render(request, "bundles/menu.html", _shell(request, store, {
        "cats": pos_catalog.categories(sellable),
        "facets": pos_catalog.facets(sellable),
        "f": _filters(request),
        "cart_ctx": cart_ctx,
        "total_products": len(sellable),
        "inventory_live": bool(inventory),
    }))
    return cart_mod.attach_cookie(response, draft)


@require_GET
def results(request):
    """GET /custom-order/results — the product grid partial (HTMX target)."""
    store = _store_from(request)
    inventory = cart_mod.inventory_for(store)
    f = _filters(request)

    # Slot scoping keeps a bundle swap inside sizes that still satisfy the bundle.
    slot_key = (request.GET.get("slot") or "").strip()[:32]
    bundle = get_bundle(request.GET.get("b") or "")
    slot = next((s for s in (bundle.slots if bundle else ()) if s.key == slot_key), None)

    items = pos_catalog.query(inventory, None, f)
    items = [p for p in items if resolver.in_stock(p)]
    if slot:
        items = [p for p in items if slot.accepts(p)]

    page = max(_int(request.GET.get("page"), 1) or 1, 1)
    total = len(items)
    start = (page - 1) * PAGE_SIZE
    products = [resolver._public(p) for p in items[start:start + PAGE_SIZE]]

    ctx = {
        "products": products, "total": total, "page": page,
        "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        "has_prev": page > 1, "has_next": start + PAGE_SIZE < total,
        "f": f, "store": store, "slot": slot_key,
        "b": (request.GET.get("b") or "")[:32],
    }
    if request.GET.get("format") == "json":
        return JsonResponse({"products": products, "total": total, "page": page})
    return render(request, "bundles/_results.html", ctx)


# ── cart ─────────────────────────────────────────────────────────────────────
def _cart_response(request, store: str, draft, *, error: str = "", status: int = 200):
    ctx = cart_mod.reprice(draft)
    ctx["error"] = error
    response = render(request, "bundles/_cart.html", _shell(request, store, {"cart_ctx": ctx}),
                      status=status)
    return cart_mod.attach_cookie(response, draft)


@require_GET
def cart_view(request):
    store = _store_from(request)
    draft = cart_mod.get_cart(request, store, create=True)
    return _cart_response(request, store, draft)


@csrf_exempt
@require_POST
@rate_limit("bundle-cart", limit=240, window=60)
def cart_add(request):
    store = _store_from(request)
    draft = cart_mod.get_cart(request, store, create=True)
    pid = str(request.POST.get("product_id") or "").strip()[:64]
    qty = min(max(_int(request.POST.get("qty"), 1) or 1, 1), cart_mod.MAX_QTY)
    ok, err = cart_mod.add(draft, pid, qty)
    message = {"not_in_stock": "That just sold out.",
               "cart_full": "Your cart is full."}.get(err, "")
    return _cart_response(request, store, draft, error=message)


@csrf_exempt
@require_POST
@rate_limit("bundle-cart", limit=240, window=60)
def cart_update(request):
    store = _store_from(request)
    draft = cart_mod.get_cart(request, store, create=True)
    pid = str(request.POST.get("product_id") or "").strip()[:64]
    cart_mod.set_qty(draft, pid, min(max(_int(request.POST.get("qty"), 0) or 0, 0), cart_mod.MAX_QTY))
    return _cart_response(request, store, draft)


@csrf_exempt
@require_POST
@rate_limit("bundle-cart", limit=240, window=60)
def cart_remove(request):
    store = _store_from(request)
    draft = cart_mod.get_cart(request, store, create=True)
    cart_mod.remove(draft, str(request.POST.get("product_id") or "").strip()[:64])
    return _cart_response(request, store, draft)


# ── checkout ─────────────────────────────────────────────────────────────────
def _clean_phone(raw: str) -> str:
    digits = _PHONE_RE.sub("", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


@require_http_methods(["GET", "POST"])
@rate_limit("bundle-checkout", limit=20, window=3600)
def checkout(request):
    """GET renders the form; POST places the order as a released PhoneCartDraft.

    Nothing here writes to Dutchie. Staff claims the draft into the POS cart and
    the register remains the only thing that can check out.
    """
    store = _store_from(request)
    draft = cart_mod.get_cart(request, store)
    if draft is None or not draft.lines:
        response = render(request, "bundles/checkout.html",
                          _shell(request, store, {"cart_ctx": {"lines": [], "quote": {}, "count": 0},
                                                  "empty": True}))
        return response

    ctx = cart_mod.reprice(draft)

    if request.method == "GET":
        response = render(request, "bundles/checkout.html",
                          _shell(request, store, {"cart_ctx": ctx}))
        return cart_mod.attach_cookie(response, draft)

    name = _CONTROL_RE.sub("", str(request.POST.get("name") or "")).strip()[:120]
    phone = _clean_phone(request.POST.get("phone"))
    email = _CONTROL_RE.sub("", str(request.POST.get("email") or "")).strip()[:254]

    errors = {}
    if len(name) < 2:
        errors["name"] = "Please enter your full name."
    if len(phone) != 10:
        errors["phone"] = "Please enter a 10-digit phone number."
    if email and not _EMAIL_RE.match(email):
        errors["email"] = "That email doesn't look right."
    if ctx["issues"]:
        errors["cart"] = "Some items changed. Please review your cart."
    if not ctx["count"]:
        errors["cart"] = "Your cart is empty."
    cap = calibration.cap_for(store)
    if ctx["quote"]["total"] > cap:
        errors["cart"] = (f"Online orders are capped at ${cap:.0f}. "
                          "Please remove a few items — or just come in and see us.")

    if errors:
        response = render(request, "bundles/checkout.html",
                          _shell(request, store, {"cart_ctx": ctx, "errors": errors,
                                                  "form": {"name": name, "email": email}}),
                          status=400)
        return cart_mod.attach_cookie(response, draft)

    draft.pickup_name = name
    draft.contact_phone = phone
    draft.contact_email = email
    draft.phone_last4 = phone[-4:]
    # Wire the order to a Dutchie customer. Read-only here; if there's no account
    # the POS creates one when a budtender claims it (bundles/customers.py).
    customers.attach(draft)
    draft.phone_hash = signing.customer_token(phone) if getattr(settings, "BUNDLE_URL_SECRET", "") else ""
    draft.source = PhoneCartDraft.Source.ONLINE
    draft.status = PhoneCartDraft.Status.RELEASED
    draft.released_at = timezone.now()
    draft.expires_at = timezone.now() + timedelta(hours=DRAFT_TTL_HOURS)
    audit = list(draft.audit or [])
    audit.append({"at": timezone.now().isoformat(), "action": "online_order_placed",
                  "ip": _client_ip(request), "lines": len(draft.lines)})
    draft.audit = audit[-100:]
    draft.save()

    logger.info("online order %s placed at %s (%d lines, $%.2f)",
                draft.draft_token, store, len(draft.lines), ctx["quote"]["total"])

    # Best-effort: the order is saved and already in the staff queue, so a mail
    # failure must never surface to the shopper as a failed checkout.
    emails.send_order_confirmation(draft, store_label(store), STORE_ADDRESS.get(store, ""))

    request.session["htco_success"] = draft.draft_token
    response = render(request, "bundles/success.html", _shell(request, store, {
        "order": draft, "cart_ctx": ctx, "draft_ttl": DRAFT_TTL_HOURS,
    }))
    # The cart is now an order — clear the cookie so a refresh starts a fresh cart
    # instead of letting the shopper edit a cart staff is already picking.
    response.delete_cookie(cart_mod.COOKIE)
    return response
