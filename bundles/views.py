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
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from budtender.models import PhoneCartDraft
from dutchie import lab as dutchie_lab
from pos import catalog as pos_catalog
from pos_core.ratelimit import _client_ip, rate_limit

from . import cart as cart_mod
from . import loyalty as loyalty_mod
from . import calibration, customers, emails, resolver, signing, tax
from .catalog import (STORE_ADDRESS, all_stores, get_bundle, store_info,
                      store_key_for, store_label)

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


STORE_COOKIE = "htco_loc"


def _store_from(request) -> str:
    """Which store the shopper is browsing.

    Order matters: an explicit `loc` always wins (a bundle link names its store and
    must not be overridden by a stale cookie), then the last store they chose, then
    Yakima — by far the largest, so it is the right default for anyone arriving
    without a preference.
    """
    store = (request.GET.get("loc") or request.POST.get("loc") or "").strip()[:32]
    if store in STORE_ADDRESS:
        return store
    remembered = (request.COOKIES.get(STORE_COOKIE) or "").strip()[:32]
    if remembered in STORE_ADDRESS:
        return remembered
    return DEFAULT_STORE


def _remember_store(response, store: str):
    """Keep the choice across pages so a Pullman shopper isn't bounced to Yakima
    on every navigation. Not HttpOnly — it is a preference, not a credential."""
    if store in STORE_ADDRESS:
        response.set_cookie(STORE_COOKIE, store, max_age=60 * 60 * 24 * 90,
                            samesite="Lax", secure=not getattr(settings, "DEBUG", False))
    return response


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


def _totals(ctx: dict, store: str) -> dict:
    """How the price the shopper actually pays decomposes for display.

    Prices here are Dutchie menu prices and this account sells tax-inclusive, so the
    total IS the menu total — nothing is added (bundles/tax.py).

    `quote["total"]` is deliberately the PRE-discount subtotal: the bundle discount is
    applied by the budtender at the register, not by us. So the tax share has to be
    taken off the discounted figure, or a $95 cart with 30% off reports $29.75 of tax
    against a $66.50 price — which is what shipped for one deploy.
    """
    quote = ctx.get("quote") or {}
    subtotal = float(quote.get("total") or 0)
    pct = float(quote.get("bundle_discount_pct") or 0)
    return tax.quote(round(subtotal * (1 - pct / 100), 2), store)


def _shell(request, store: str, ctx: dict) -> dict:
    """Context every page of the storefront needs."""
    return {
        "store": store,
        "store_label": store_label(store),
        "store_address": STORE_ADDRESS.get(store, ""),
        # Logo, nav and footer links point back at the marketing site. Absolute, so
        # they work both through the happytimeweed.com rewrite and when this host is
        # opened directly.
        "SITE_ORIGIN": getattr(settings, "SITE_ORIGIN", "https://happytimeweed.com"),
        # Full pickup detail for the current store, and the list for the picker.
        "store_info": store_info(store),
        "stores": all_stores(),
        **ctx,
    }


# ── the emailed bundle ───────────────────────────────────────────────────────
@require_GET
def landing(request):
    """GET /custom-order?b=&loc=&i=&exp=&sig= — the emailed bundle, live-resolved.

    Also seeds the shopper's cart, so "add bundle to cart" is genuinely one tap:
    they land already holding the items, and can browse for more.

    With NO bundle at all — someone typing /custom-order, or a link stripped back to
    its path — this is just the front door of the shop, so send them to the menu.
    It used to answer "This link didn't open", which is an error message for a person
    who never had a link.
    """
    if not request.GET.get("b"):
        return redirect(f"{reverse('bundle_menu')}?loc={_store_from(request)}")

    try:
        req = signing.parse(request.GET)
    except signing.BundleUrlError as exc:
        # Through _shell like every other full page: this one renders base.html too,
        # and without the shell context it came out with "Pickup at ." and an empty
        # footer — a broken-looking page shown to someone whose link already failed.
        store = _store_from(request)
        return render(request, "bundles/invalid.html",
                      _shell(request, store, {"reason": str(exc)}), status=400)

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
    cart_ctx = cart_mod.reprice(draft, inventory, confirm=True)

    # The emailed link IS the order: the products are already chosen and priced from
    # live register stock, so the shopper lands on the checkout holding all of it and
    # only has to give a phone number. Browsing is still one tap away, but it is no
    # longer a step between them and placing the order.
    response = render(request, "bundles/landing.html", _shell(request, req.store, {
        "bundle": bundle,
        "lines": lines,
        "result": result,
        "expired": req.expired,
        "cart_ctx": cart_ctx,
        "totals": _totals(cart_ctx, req.store),
        "draft_ttl": DRAFT_TTL_HOURS,
        "checkout_inline": True,
        "form": {},
        "errors": {},
    }))
    # The bundle link names its store; remember THAT, so a Pullman bundle doesn't
    # leave the shopper browsing Yakima afterwards.
    return _remember_store(cart_mod.attach_cookie(response, draft), req.store)


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
    # NOT confirm=True. This is pure browsing — an earlier automated edit matched
    # this call along with landing()'s (identical source line) and confirmed every
    # cart line's price on every menu page load. That would fire a price-check per
    # line each time someone opens the menu with items already in their cart, which
    # is exactly the register load the browse/checkout split exists to avoid.
    cart_ctx = cart_mod.reprice(draft, inventory)

    response = render(request, "bundles/menu.html", _shell(request, store, {
        "cats": pos_catalog.categories(sellable),
        "facets": pos_catalog.facets(sellable),
        "f": _filters(request),
        "cart_ctx": cart_ctx,
        "total_products": len(sellable),
        "inventory_live": bool(inventory),
    }))
    return _remember_store(cart_mod.attach_cookie(response, draft), store)


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


@require_GET
def product_lab(request, product_id):
    """GET /custom-order/lab/<product_id> — potency (THCA + Total) and terpenes.

    On demand, one product at a time — never called for a whole grid page. The
    lab endpoint is one Dutchie request PER BATCH (`dutchie/lab.py`); firing it for
    every tile on a 24-item page would multiply register load 24x for numbers most
    shoppers never look at. A shopper who wants the detail asks for it here.

    `product_id` is public (it is what's already in every cart link); the BatchId
    that keys the lab lookup is resolved server-side from live inventory and never
    reaches the client — same rule as SerialNo in `cart.confirm_live_price`.
    """
    store = _store_from(request)
    inventory = cart_mod.inventory_for(store)
    live = resolver.find_live(inventory, str(product_id))
    result = None
    if live:
        batch_id = live.get("BatchId")
        try:
            result = dutchie_lab.lab_result(store_key_for(store), batch_id)
        except Exception:
            logger.warning("lab lookup failed for product %s", product_id, exc_info=True)
    return render(request, "bundles/_lab.html", {
        "lab": result, "potency": resolver.public_potency(result),
    })


# ── cart ─────────────────────────────────────────────────────────────────────
def _cart_response(request, store: str, draft, *, error: str = "", status: int = 200):
    ctx = cart_mod.reprice(draft)
    ctx["error"] = error
    response = render(request, "bundles/_cart.html", _shell(request, store, {"cart_ctx": ctx}),
                      status=status)
    return _remember_store(cart_mod.attach_cookie(response, draft), store)


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


# A phone number in, a real person's NAME out, with no login in front of it. That
# is a PII oracle, so the throttle is the control that matters, not a nicety:
#   * 5/minute stops a burst,
#   * 30/hour stops the patient version — one number a minute, all day, which is
#     what an enumeration script actually looks like.
# Both scopes are separate from bundle-checkout on purpose: an abuser burning the
# lookup budget must not also lock real shoppers out of placing orders.
LOOKUP_PER_MINUTE = 5
LOOKUP_PER_HOUR = 30


@require_http_methods(["GET", "POST"])
# methods=("POST",): reading the page must not spend the budget for using it. Without
# it, six refreshes of the form lock someone out of the lookup they came for — and the
# limiter keys on IP, so one household or one store's wifi is a single bucket.
@rate_limit("loyalty-hour", limit=LOOKUP_PER_HOUR, window=3600, methods=("POST",))
@rate_limit("loyalty", limit=LOOKUP_PER_MINUTE, window=60, methods=("POST",))
def loyalty(request):
    """Public /loyalty — "how many points do I have?", answered by phone number.

    Same PII-oracle shape as `lookup_customer` below, so it carries the same throttle
    and its own scope: someone burning the loyalty budget must not also lock real
    shoppers out of the order lookup.

    It says LESS than lookup_customer does. That one returns a name; this returns a
    points figure, a tier and what the balance redeems for — never a name, never an
    email, never an address, never a purchase. A balance is not identifying on its
    own, and there is no reason for this page to make it so.

    Not found and register-unavailable deliberately give the SAME answer. A
    distinguishable failure tells a prober which numbers are real even while the
    lookup is broken.
    """
    ctx = {"store_label": "Happy Time", "tiers": loyalty_mod.TIERS}
    if request.method == "POST":
        phone = _clean_phone(request.POST.get("phone"))
        ctx["phone"] = request.POST.get("phone") or ""
        if len(phone) != 10:
            ctx["error"] = "Enter a 10-digit phone number."
        else:
            ctx["result"] = loyalty_mod.balance_for_phone(phone)
            ctx["searched"] = True
    return render(request, "bundles/loyalty.html", ctx)


@require_POST
@rate_limit("bundle-lookup-hour", limit=LOOKUP_PER_HOUR, window=3600)
@rate_limit("bundle-lookup", limit=LOOKUP_PER_MINUTE, window=60)
def lookup_customer(request):
    """POST /custom-order/lookup-customer — "do we already know this number?"

    Purely a convenience: a returning shopper shouldn't have to retype the name
    Dutchie already has. So every failure mode collapses to the SAME answer,
    `{"found": false}`, and the shopper types their name as they would have anyway:

      * a phone that isn't 10 digits never reaches Dutchie at all,
      * Dutchie down, slow or angry is a 200, never a 500,
      * a match with no usable name is not a match.

    That symmetry is also the security property. A distinguishable failure ("we
    couldn't reach the register" vs "no account") tells a prober which numbers are
    real even when the lookup is broken, and a 500 tells them by timing.

    NOTE the allowlist at the bottom. `lookup_by_phone` hands back an AcctId too,
    and the Dutchie guest row behind it carries DOB, address, email and points.
    Only the two name fields are ever named in a response — never a dict passed
    through, so growing the tuple upstream cannot silently widen this endpoint.
    """
    phone = _clean_phone(request.POST.get("phone"))
    if len(phone) != 10:
        return JsonResponse({"found": False})

    store = _store_from(request)
    try:
        # Read-only. The create half lives behind staff auth at claim time
        # (bundles/customers.py) — an unauthenticated create is a spam vector.
        _acct, name, status = customers.lookup_by_phone(store, phone)
    except Exception:
        # lookup_by_phone swallows its own Dutchie errors today; this catches the
        # day it stops, because a shopper must never lose their cart to it.
        logger.warning("customer lookup unavailable at %s", store, exc_info=True)
        return JsonResponse({"found": False})

    matched = status == PhoneCartDraft.Customer.MATCHED
    first, last = customers.split_name(name) if matched else ("", "")
    # Last 4 only, same as `phone_last4` in the staff queue: enough to reconcile a
    # complaint against a real order, not enough to reconstitute the number from logs.
    logger.info("customer lookup at %s from %s for ...%s (%s)",
                store, _client_ip(request), phone[-4:], "match" if first else "no match")
    if not first:
        # A matched row with an unusable name would show "we found you" over two
        # empty boxes — worse than not asking.
        return JsonResponse({"found": False})
    return JsonResponse({"found": True, "first_name": first, "last_name": last})


@require_http_methods(["GET", "POST"])
# POST only, and a store-wide budget rather than a per-shopper one.
#
# `_client_ip` takes the LAST X-Forwarded-For hop, which is correct when our own
# Traefik is the edge — but happytimeweed.com/custom-order is a Vercel rewrite, so
# for every shopper on the on-brand link the last hop is a Vercel egress IP. Proven
# by the only real online order in production: its audit ip is 34.222.117.230, which
# resolves into AWS us-west-2, where Vercel's pdx1 runs. So this bucket is shared by
# EVERYONE, not per person, and at 20/hour it capped the whole business at roughly
# ten orders an hour — with GETs counting, a shopper could be refused before
# submitting anything.
#
# Two changes: GETs no longer spend it, and the number is set for what it actually
# is — a store-wide abuse ceiling, not a per-person one. Nothing here writes to
# Dutchie, so this throttle guards our own DB and mail, not the register.
# ponytail: per-shopper throttling needs the proxy chain sorted out (trusting the
# FIRST hop is spoofable); raise this again or fix the chain if abuse ever shows up.
@rate_limit("bundle-checkout", limit=300, window=3600, methods=("POST",))
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

    # confirm=True: this is where the price stops being a quote. It lands in
    # draft.lines, the confirmation email and the POS queue, and is never
    # revalidated again before someone pays it.
    ctx = cart_mod.reprice(draft, confirm=True)

    if request.method == "GET":
        response = render(request, "bundles/checkout.html",
                          _shell(request, store, {"cart_ctx": ctx, "totals": _totals(ctx, store)}))
        return _remember_store(cart_mod.attach_cookie(response, draft), store)

    def _clean(field, limit):
        return _CONTROL_RE.sub("", str(request.POST.get(field) or "")).strip()[:limit]

    first_name, last_name = _clean("first_name", 60), _clean("last_name", 60)
    name = f"{first_name} {last_name}".strip()[:120]
    phone = _clean_phone(request.POST.get("phone"))
    email = _clean("email", 254)

    errors = {}
    # Phone, first and last name — the same three Dutchie's own pickup checkout makes
    # required, so a shopper who has ordered there before meets no new questions. Name
    # is split rather than one free-text field because `customers.ensure_customer`
    # feeds `create_guest(first_name=, last_name=)`; splitting a single string is
    # guesswork on anyone with two surnames or a middle name.
    if len(phone) != 10:
        errors["phone"] = "Please enter a 10-digit phone number."
    if not first_name:
        errors["first_name"] = "Please enter your first name."
    if not last_name:
        errors["last_name"] = "Please enter your last name."
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
                          _shell(request, store, {
                              "cart_ctx": ctx, "totals": _totals(ctx, store), "errors": errors,
                              "form": {"first_name": first_name, "last_name": last_name,
                                       "phone": phone, "email": email}}),
                          status=400)
        return _remember_store(cart_mod.attach_cookie(response, draft), store)

    draft.pickup_name = name
    draft.contact_phone = phone
    draft.contact_email = email
    draft.phone_last4 = phone[-4:]
    # Wire the order to a Dutchie customer. Read-only here; if there's no account
    # the POS creates one when a budtender claims it (bundles/customers.py).
    customers.attach(draft)
    # The name is required now, so this only fires if it somehow arrived blank — the
    # staff queue must never show a row nobody can call out across the counter.
    if not draft.pickup_name:
        draft.pickup_name = (draft.customer_name or "").strip() or f"Phone {phone[-4:]}"
    draft.phone_hash = signing.customer_token(phone) if getattr(settings, "BUNDLE_URL_SECRET", "") else ""
    draft.source = PhoneCartDraft.Source.ONLINE
    draft.status = PhoneCartDraft.Status.RELEASED
    draft.released_at = timezone.now()
    draft.expires_at = timezone.now() + timedelta(hours=DRAFT_TTL_HOURS)
    audit = list(draft.audit or [])
    audit.append({"at": timezone.now().isoformat(), "action": "online_order_placed",
                  "ip": _client_ip(request), "lines": len(draft.lines)})
    draft.audit = audit[-100:]
    # Only release a cart that is still OPEN, and do it in one atomic UPDATE.
    #
    # A bare draft.save() wrote every column, so a second click that landed AFTER a
    # budtender had already claimed the order reset status claimed -> released and
    # claimed_at -> None. The order went back into the staff queue and a second
    # budtender could pick and sell it again — the one path from this page to a
    # duplicate real-world sale. Verified: "after the 2nd click lands: status=
    # 'released' claimed_at=None / in the released queue again? 1 row(s)".
    #
    # filter(status=OPEN) makes the release idempotent: the loser of a double-click
    # updates 0 rows and falls through to the same confirmation page.
    released = PhoneCartDraft.objects.filter(
        pk=draft.pk, status=PhoneCartDraft.Status.OPEN,
    ).update(
        pickup_name=draft.pickup_name, contact_phone=draft.contact_phone,
        contact_email=draft.contact_email, phone_last4=draft.phone_last4,
        phone_hash=draft.phone_hash, source=draft.source, status=draft.status,
        released_at=draft.released_at, expires_at=draft.expires_at,
        audit=draft.audit, dutchie_acct_id=draft.dutchie_acct_id,
        customer_status=draft.customer_status, customer_name=draft.customer_name,
        lines=draft.lines, quote=draft.quote, updated_at=timezone.now(),
    )
    if released:
        logger.info("online order %s placed at %s (%d lines, $%.2f)",
                    draft.draft_token, store, len(draft.lines), ctx["quote"]["total"])
        # Best-effort: the order is saved and already in the staff queue, so a mail
        # failure must never surface to the shopper as a failed checkout.
        emails.send_order_confirmation(draft, store_label(store), STORE_ADDRESS.get(store, ""))
    else:
        # The impatient second click, or a second tab. Their order already exists —
        # show the same confirmation, but do not email or log it twice.
        logger.info("online order %s already released — ignoring duplicate submit",
                    draft.draft_token)

    request.session["htco_success"] = draft.draft_token
    response = render(request, "bundles/success.html", _shell(request, store, {
        "order": draft, "cart_ctx": ctx, "draft_ttl": DRAFT_TTL_HOURS,
    }))
    # The cart is now an order — clear the cookie so a refresh starts a fresh cart
    # instead of letting the shopper edit a cart staff is already picking.
    response.delete_cookie(cart_mod.COOKIE)
    return response
