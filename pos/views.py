"""Budtender screen views — scan/lookup/profile/inventory/cart/submit + auth.

Function views, server-rendered + HTMX partials. Write paths are @login_required.
Dutchie calls are wrapped so missing creds/endpoints degrade to a visible error, never
a 500. Cart lives in the session. Public-ish endpoints are throttled. Lists paginate.
"""

from __future__ import annotations

import logging
import os
from collections import Counter

from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Max, OuterRef, Q, Subquery, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from pos_core.ratelimit import rate_limit
from customers import tracking
from customers.intelligence import load_customer_history, load_profile_full_cached
from customers.models import Customer, ShopEvent, ShopVisit
from customers.services import record_write, upsert_customer
from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import load_stores

from . import catalog, education, imagemap, pairing, persona, ranking

logger = logging.getLogger(__name__)

MAX_LIST = 40  # cap any rendered list (pagination ceiling)
MENU_PAGE = 24  # products per menu page (paginated)


# ── helpers ──────────────────────────────────────────────────────────────────
def _stores():
    try:
        return load_stores()
    except Exception as exc:
        logger.warning("load_stores failed: %s", exc)
        return {}


def _active_store(request):
    stores = _stores()
    # Per-instance lock: set BUDTENDER_LOCK_STORE to pin this deployment to one
    # store and ignore any client-supplied store (store-isolation hardening).
    lock = os.environ.get("BUDTENDER_LOCK_STORE", "").strip()
    if lock and lock in stores:
        request.session["store"] = lock
        return stores[lock]
    name = request.POST.get("store") or request.GET.get("store") or request.session.get("store")
    if name and name in stores:
        request.session["store"] = name
        return stores[name]
    if stores:
        first = next(iter(stores))
        request.session["store"] = first
        return stores[first]
    return None


def _client(store):
    return PosRegisterClient(store)


def _parse_guests(raw) -> list[dict]:
    """checkin_search_by_string Data -> [{acct_id, name, phone, patient_type, last}]."""
    rows = raw.get("Data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows[:MAX_LIST]:
        if not isinstance(r, dict):
            continue
        acct = r.get("Guest_id") or r.get("AcctId") or r.get("CustomerId") or r.get("Id")
        name = (r.get("Name") or f"{r.get('FirstName','')} {r.get('LastName','')}").strip()
        pt = (r.get("PatientType") or "").strip()
        is_med = "med" in pt.lower()
        out.append({"acct_id": acct, "name": name or "(unknown)",
                    "phone": r.get("PhoneNo") or r.get("Phone") or r.get("CellPhone") or "",
                    "patient_type": pt, "is_medical": is_med,
                    "pt_label": ("Medical" if is_med else "Rec") if pt else "",
                    "last": (r.get("LastTransaction") or "")[:10]})
    return out


# ── auth (budtender-facing login, mobile) ────────────────────────────────────
@rate_limit("login", limit=10, window=300)
def login_view(request):
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            tracking.track(request, "login")
            return redirect("screen")
    else:
        form = AuthenticationForm(request)
    return render(request, "pos/login.html", {"form": form})


def logout_view(request):
    auth_logout(request)
    return redirect("login")


# ── begin session (start gate: scan ID + phone -> 21 check -> find/create) ─────
@login_required
def begin(request):
    return render(request, "pos/begin.html", {
        "stores": list(_stores().keys()), "active": request.session.get("store"),
    })


@login_required
def end_session(request):
    tracking.end_visit(request, "abandoned")  # restart / new customer without checkout
    for k in ("acct_id", "acct_name", "acct_phone", "cart"):
        request.session.pop(k, None)
    return redirect("begin")


def _start_session(request, acct_id, name, phone, how="lookup", **meta):
    request.session["acct_id"] = acct_id
    request.session["acct_name"] = name
    request.session["acct_phone"] = phone
    request.session["cart"] = []
    allowed = request.session.get("guests") or {}
    allowed[str(acct_id)] = {"name": name or "", "phone": phone or ""}
    request.session["guests"] = allowed
    tracking.start_visit(request, acct_id=acct_id, name=name, phone=phone, how=how, **meta)
    return redirect("screen")


def _resolve_or_create(client, scan, phone):
    """Look up by PHONE then by NAME (phone wins when both match); create from the
    scan if neither exists. Returns (acct_id, name, how)."""
    name = (scan.get("accts_name") or "").strip()
    if phone:
        g = _parse_guests(client.guest_search(phone))
        if g:
            return g[0]["acct_id"], g[0]["name"], "phone"
    if name:
        g = _parse_guests(client.guest_search(name))
        if g:
            return g[0]["acct_id"], g[0]["name"], "name"
    if scan.get("first_name") and scan.get("birth_date"):
        gid = client.create_guest(
            first_name=scan["first_name"], last_name=scan.get("last_name", ""),
            dob=scan["birth_date"], phone=phone or scan.get("phone", ""),
            email=scan.get("email", ""), mj_state_id=scan.get("mjstateidno", ""))
        if gid:
            disp = scan.get("accts_name") or f"{scan['first_name']} {scan.get('last_name', '')}".strip()
            return gid, disp, "created"
    return None, None, "none"


@login_required
@rate_limit("start", limit=30, window=60)
@require_http_methods(["POST"])
def start(request):
    from pos_core.uploads import collect_id_images

    store = _active_store(request)
    phone = "".join(c for c in (request.POST.get("phone") or "") if c.isdigit())
    ctx = {"stores": list(_stores().keys()), "active": request.session.get("store"), "phone": phone}

    # "Continue as guest" — quick anonymous Dutchie guest, no profile needed.
    if request.POST.get("guest"):
        if not store:
            ctx["error"] = "no store configured"
            return render(request, "pos/begin.html", ctx)
        try:
            gid = _client(store).create_guest(first_name="Guest", last_name="", dob="", phone="")
        except Exception as exc:
            logger.warning("guest start failed: %s", exc)
            ctx["error"] = "Could not start a guest session — try again."
            return render(request, "pos/begin.html", ctx)
        if not gid:
            ctx["error"] = "could not start a guest session"
            return render(request, "pos/begin.html", ctx)
        return _start_session(request, gid, "Guest", "", how="guest")

    scan = {}
    files = request.FILES.getlist("images")
    if files:
        try:
            images = collect_id_images(files)
        except Exception as exc:
            ctx["error"] = f"upload rejected: {exc}"
            return render(request, "pos/begin.html", ctx)
        from idscan.pipeline import run_id_scan
        scan = run_id_scan(images)
        if scan.get("error"):
            ctx["error"] = f"scan failed: {scan['error']}"
            return render(request, "pos/begin.html", ctx)
        ctx["scan"] = scan
        if scan.get("over_21") is False:    # HARD age flag — do not start a session
            ctx["under21"] = True
            return render(request, "pos/begin.html", ctx)
    if not store:
        ctx["error"] = "no store configured"
        return render(request, "pos/begin.html", ctx)
    if not (phone or scan):
        ctx["error"] = "Scan an ID or enter a phone number to begin."
        return render(request, "pos/begin.html", ctx)

    phone = phone or "".join(c for c in (scan.get("phone") or "") if c.isdigit())
    try:
        acct_id, name, how = _resolve_or_create(_client(store), scan, phone)
    except Exception as exc:
        logger.warning("start lookup failed: %s", exc)
        ctx["error"] = "Lookup failed — try again."
        return render(request, "pos/begin.html", ctx)
    if not acct_id:
        # No match + nothing to create from → offer Create-profile (scan) or Guest.
        ctx["no_account"] = True
        return render(request, "pos/begin.html", ctx)

    if scan:
        upsert_customer({**scan, "phone": phone}, dutchie_acct_id=acct_id)
    how = "scan" if scan else how
    return _start_session(request, acct_id, name, phone, how=how,
                          scan_over21=scan.get("over_21") if scan else None)


# ── POS screen (requires an active session) ────────────────────────────────────
@login_required
def screen(request):
    if not request.session.get("acct_id"):
        return redirect("begin")
    return render(request, "pos/screen.html", {
        "stores": list(_stores().keys()),
        "active": request.session.get("store"),
        "cart": request.session.get("cart", []),
        "acct_id": request.session.get("acct_id"),
        "acct_name": request.session.get("acct_name"),
        "initial_cat": request.GET.get("cat", ""),
    })


@login_required
@rate_limit("scan", limit=20, window=60)
@require_http_methods(["POST"])
def scan(request):
    from pos_core.uploads import collect_id_images

    store = _active_store(request)
    ctx = {"store": store}
    try:
        images = collect_id_images(request.FILES.getlist("images"))
    except Exception as exc:
        tracking.track(request, "scan_failed", detail=f"upload: {exc}"[:120])
        ctx["error"] = f"upload rejected: {exc}"
        return render(request, "pos/_profile.html", ctx)

    from idscan.pipeline import run_id_scan

    scan_result = run_id_scan(images)
    if scan_result.get("error"):
        tracking.track(request, "scan_failed", detail=str(scan_result["error"])[:120])
        ctx["error"] = f"scan failed: {scan_result['error']}"
        return render(request, "pos/_profile.html", ctx)

    acct_id = None
    if store:
        try:
            q = scan_result.get("phone") or scan_result.get("accts_name") or ""
            guests = _parse_guests(_client(store).guest_search(q))
            if guests:
                acct_id = guests[0]["acct_id"]
        except Exception as exc:
            logger.warning("scan guest lookup unavailable: %s", exc)
            ctx["warn"] = "Customer lookup unavailable."
    upsert_customer(scan_result, dutchie_acct_id=acct_id)
    if acct_id:
        request.session["acct_id"] = acct_id
        request.session["acct_name"] = scan_result.get("accts_name")
        allowed = request.session.get("guests") or {}
        allowed[str(acct_id)] = {"name": scan_result.get("accts_name", ""),
                                 "phone": scan_result.get("phone", "")}
        request.session["guests"] = allowed
        tracking.start_visit(request, acct_id=acct_id, name=scan_result.get("accts_name", ""),
                             phone=scan_result.get("phone", ""), how="scan",
                             scan_over21=scan_result.get("over_21"))
    request.session["acct_phone"] = scan_result.get("phone") or ""
    ctx.update({"scan": scan_result, "acct_id": acct_id,
                "history": load_customer_history(acct_id=acct_id, phone=scan_result.get("phone"),
                                                 name=scan_result.get("accts_name"))})
    resp = render(request, "pos/_profile.html", ctx)
    resp["HX-Trigger"] = "customerChanged"
    return resp


@login_required
@rate_limit("lookup", limit=150, window=60)   # typeahead fires more often (debounced keyup)
@require_http_methods(["POST"])
def lookup(request):
    store = _active_store(request)
    q = (request.POST.get("phone") or request.POST.get("name") or "").strip()
    # mode=start (begin gate) -> a result fills the phone to start; else select a customer.
    mode = "start" if request.POST.get("mode") == "start" else "select"
    ctx = {"store": store, "query": q, "mode": mode}
    if not store:
        ctx["error"] = "no store configured (create stores.json)"
        return render(request, "pos/_guests.html", ctx)
    if len(q) < 3:                              # wait for a meaningful fragment (don't hammer Dutchie)
        ctx["guests"] = []
        return render(request, "pos/_guests.html", ctx)
    try:
        guests = _parse_guests(_client(store).guest_search(q))
    except Exception as exc:
        logger.warning("lookup failed: %s", exc)
        tracking.track(request, "lookup_failed", detail=str(exc)[:120])
        ctx["error"] = "Lookup failed — try again."
        return render(request, "pos/_guests.html", ctx)
    ctx["guests"] = guests
    tracking.track(request, "customer_search", detail=q[:120], results=len(guests))
    # Record which accounts THIS budtender is allowed to open (anchors `profile`
    # so it can't be used to enumerate arbitrary customers' PII).
    allowed = request.session.get("guests") or {}
    for g in guests:
        if g.get("acct_id") is not None:
            allowed[str(g["acct_id"])] = {"name": g.get("name", ""), "phone": g.get("phone", "")}
    request.session["guests"] = allowed
    return render(request, "pos/_guests.html", ctx)


@login_required
@rate_limit("profile", limit=40, window=60)
@require_http_methods(["POST"])
def profile(request):
    """POST-only (was a state-mutating GET — CSRF retarget). The customer's phone/
    name are taken from the SESSION allow-map populated by a prior lookup/scan, never
    from the request — so this can't be used to pull arbitrary customers' PII (IDOR)."""
    acct = request.POST.get("acct")
    allowed = (request.session.get("guests") or {}).get(str(acct))
    if not allowed:
        return render(request, "pos/_profile.html",
                      {"error": "Select a customer from a lookup first."})
    name, phone = allowed.get("name", ""), allowed.get("phone", "")
    request.session["acct_id"] = acct
    request.session["acct_name"] = name
    request.session["acct_phone"] = phone
    tracking.start_visit(request, acct_id=acct, name=name, phone=phone, how="lookup")
    upsert_customer({"accts_name": name, "phone": phone},
                    dutchie_acct_id=int(acct) if str(acct).isdigit() else None)
    resp = render(request, "pos/_profile.html", {
        "acct_id": acct, "scan": {"accts_name": name, "phone": phone},
        "history": load_customer_history(acct_id=acct, phone=phone, name=name),
    })
    resp["HX-Trigger"] = "customerChanged"  # re-rank the menu For-You
    return resp


# ── customer profile (2 pages: preview + full transaction history) ─────────────
def _affw(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ranked_affinity(aff, n=6):
    """Top-n of a {name: weight} affinity dict -> [{name, weight, pct, share}].
    `pct` = bar width relative to the top item; `share` = weight as a percent only
    when it looks like a fraction (0<w<=1), else None (so raw counts don't show 600%)."""
    if not aff:
        return []
    pairs = sorted(((str(k), _affw(v)) for k, v in aff.items()),
                   key=lambda x: x[1], reverse=True)[:n]
    mx = pairs[0][1] or 1.0
    return [{"name": k, "weight": w, "pct": round(w / mx * 100),
             "share": round(w * 100) if 0 < w <= 1 else None} for k, w in pairs]


def _fav_products(hist, n=6):
    rows = [h for h in (hist or []) if h.get("product") or h.get("sku")]
    return sorted(rows, key=lambda h: _affw(h.get("times_bought")), reverse=True)[:n]


def _fav_strains(hist, n=8):
    agg = {}
    for h in hist or []:
        s = (h.get("strain") or "").strip()
        if not s:
            continue
        a = agg.setdefault(s, {"strain": s, "times": 0, "type": h.get("strain_type") or ""})
        a["times"] += int(_affw(h.get("times_bought")) or 1)
    return sorted(agg.values(), key=lambda a: a["times"], reverse=True)[:n]


def _customer_ctx(request, full):
    acct_id = request.session.get("acct_id")
    phone = request.session.get("acct_phone") or ""
    name = request.session.get("acct_name") or ""
    profile = load_profile_full_cached(phone) if phone else None
    store = _active_store(request)
    cust = None
    if acct_id and str(acct_id).isdigit():
        cust = Customer.objects.filter(dutchie_acct_id=int(acct_id)).first()
    if cust is None and phone:
        cust = Customer.objects.filter(phone=phone).first()
    inv = []
    if store:
        try:
            inv = catalog.get_inventory(store.name)
        except Exception as exc:
            logger.warning("customer inv load failed: %s", exc)
    # purchase_history comes from an uncontrolled remote DB; keep only dict rows so a
    # stray null/string element degrades instead of 500-ing (same guard as ranking/suggest).
    # COPY each row — `profile` is now cached/shared, so the full-page `h["spend"]=` below
    # must not mutate the cached object.
    hist = [dict(h) for h in ((profile or {}).get("purchase_history") or []) if isinstance(h, dict)]
    ranked = catalog.query(inv, profile, {"sort": "foryou"}) if inv else []   # rank ONCE
    sugg = catalog.suggestions(store.name, profile, 8) if (store and profile) else []
    if not sugg and ranked:                   # new/anon customer: top picks, never empty
        sugg = ranked[:8]
    ctx = {
        "acct_id": acct_id, "acct_name": name, "acct_phone": phone,
        "cust": cust, "profile": profile, "cart": request.session.get("cart", []),
        "persona": persona.summarize(profile),
        "fav_categories": _ranked_affinity((profile or {}).get("category_affinity"), 6),
        "fav_brands": _ranked_affinity((profile or {}).get("brand_affinity"), 6),
        "fav_strain_types": _ranked_affinity((profile or {}).get("strain_type_affinity"), 5),
        "fav_subcats": _ranked_affinity((profile or {}).get("subcategory_affinity"), 6),
        "fav_terpenes": _ranked_affinity((profile or {}).get("terpene_affinity"), 6),
        "bucket_mix": _ranked_affinity((profile or {}).get("bucket_mix"), 3),
        "fav_strains": _fav_strains(hist, 8),
        "fav_products": _fav_products(hist, 6),
        "suggestions": sugg,
        "picks": _carousels(ranked, profile, n_cats=4, per=2),   # one rank + group (no N+1)
        "history_count": len(hist),
    }
    if full:
        for h in hist:
            h["spend"] = round(_affw(h.get("last_price")) * _affw(h.get("qty")), 2)
        ctx["history"] = sorted(hist, key=lambda h: str(h.get("last_bought_at") or ""), reverse=True)
        ctx["kpi_units"] = round(sum(_affw(h.get("qty")) for h in hist))
        ctx["kpi_spend"] = round(sum(h.get("spend", 0) for h in hist))
        ctx["kpi_products"] = len(hist)
    return ctx


@login_required
@rate_limit("customer", limit=60, window=60)
@require_http_methods(["GET"])
def customer(request):
    if not request.session.get("acct_id"):
        return redirect("screen")
    tracking.track(request, "profile_view")
    return render(request, "pos/customer_preview.html", _customer_ctx(request, full=False))


@login_required
@rate_limit("customer", limit=60, window=60)
@require_http_methods(["GET"])
def customer_full(request):
    if not request.session.get("acct_id"):
        return redirect("screen")
    tracking.track(request, "profile_full_view")
    return render(request, "pos/customer_full.html", _customer_ctx(request, full=True))


def _filters(request):
    g = request.GET

    def _int(k):
        v = (g.get(k) or "").strip()
        return int(v) if v.lstrip("-").isdigit() else None

    return {
        "q": g.get("q", ""), "cat": g.get("cat", ""), "brand": g.get("brand", ""),
        "strain_type": g.get("strain_type", ""), "effect": g.get("effect", ""),
        "sort": g.get("sort", "foryou"),
        "price_min": _int("price_min"), "price_max": _int("price_max"),
        "thc_min": _int("thc_min"), "doh_only": g.get("doh_only") == "1",
        "page": max(1, _int("page") or 1),
    }


@login_required
@rate_limit("menu", limit=180, window=60)
@require_http_methods(["GET"])
def menu(request):
    store = _active_store(request)
    ctx = {"store": store}
    if not store:
        ctx["error"] = "no store configured"
        return render(request, "pos/_menu.html", ctx)
    phone = request.session.get("acct_phone") or ""
    # Cached persisted taste (fast — no DB hit per filter change) blended with THIS visit's
    # live behavior, so every customer (new / guest / DB-down) gets a personalized feed.
    profile = load_profile_full_cached(phone) if phone else None
    eff = ranking.blend_session_taste(profile, request.session.get("taste"))
    f = _filters(request)
    try:
        items = catalog.get_inventory(store.name)
    except Exception as exc:
        logger.warning("menu load failed: %s", exc)
        ctx["error"] = "Menu unavailable — refresh in a moment."
        return render(request, "pos/_menu.html", ctx)
    facets = catalog.facets(items)
    # DOH defaults ON (owner rule) when the catalog has DOH products and the user
    # hasn't interacted with the filter form yet (the hidden `f=1` sentinel). Once
    # they toggle filters, the checkbox state is respected (unchecked -> off).
    if facets["has_doh"] and request.GET.get("f") != "1":
        f["doh_only"] = True
    results = catalog.query(items, eff, f)
    total = len(results)
    pages = max(1, (total + MENU_PAGE - 1) // MENU_PAGE)
    page = min(f["page"], pages)
    start_i = (page - 1) * MENU_PAGE
    # Personalized "home": per-category carousels + cart-aware pairs, only on the default
    # (unfiltered) view — once they tab/search, just show the filtered grid.
    default_view = not f.get("cat") and not f.get("q")
    carousels = _carousels(results, eff, n_cats=5, per=15) if (eff and default_view) else []
    cart = request.session.get("cart", [])
    anchor = _cart_anchor(store, cart) if (cart and default_view) else None
    cart_pairs = pairing.pair_for(items, anchor, eff, n=4) if anchor else []
    ctx.update(
        products=results[start_i:start_i + MENU_PAGE], total=total,
        page=page, pages=pages, has_prev=page > 1, has_next=page < pages,
        cats=catalog.categories(items), facets=facets, f=f,
        has_customer=bool(eff), acct_name=request.session.get("acct_name"),
        # The For-You strip is a fallback shown ONLY when there are no carousels (see
        # _menu.html) — don't run the 3-pass suggest scan on filtered/keystroke views.
        suggestions=catalog.suggestions(store.name, eff, 6) if (eff and default_view and not carousels) else [],
        carousels=carousels, cart_pairs=cart_pairs,
        persona=persona.summarize(profile),
    )
    _track_browse(request, f, total, ctx["suggestions"])
    return render(request, "pos/_menu.html", ctx)


def _carousels(ranked, profile, n_cats=5, per=15):
    """Per-category rails built by GROUPING the already-ranked feed (one rank, no re-query) —
    categories ordered by the customer's affinity, top-`per` of each."""
    if not ranked or not profile:
        return []
    groups = {}
    for p in ranked:
        groups.setdefault(p["cat_key"], []).append(p)   # ranked order preserved within each
    cat_aff = profile.get("category_affinity") or {}

    def affw(ck):
        return max((_affw(w) for raw, w in cat_aff.items()
                    if (imagemap.category_key(raw) or "other") == ck), default=0.0)

    out = []
    for ck in sorted(groups, key=affw, reverse=True):
        out.append({"key": ck, "label": catalog.CAT_LABELS.get(ck, ck.title()),
                    "items": groups[ck][:per]})
        if len(out) >= n_cats:
            break
    return out


def _cart_anchor(store, cart):
    """The priciest line in the cart — the cross-sell anchor (resolved to a full product)."""
    if not (store and cart):
        return None
    top = max(cart, key=lambda it: float(it.get("UnitPrice") or 0))
    try:
        return catalog.find_item(store.name, product_id=top.get("ProductId"))
    except Exception as exc:
        logger.warning("cart anchor resolve failed: %s", exc)
        return None


def _track_browse(request, f, total, suggestions):
    """Log meaningful browse activity only (deduped) so the menu's frequent reloads don't
    flood the event log: a search when the query changes, a category when it changes, and
    the suggested item-set once per distinct set."""
    if f.get("q"):
        if request.session.get("_lastsearch") != f["q"]:
            request.session["_lastsearch"] = f["q"]
            tracking.track(request, "search", detail=f["q"][:120], results=total)
    else:
        request.session.pop("_lastsearch", None)
        browse = f.get("cat") or "all"
        if request.session.get("_lastbrowse") != browse:
            request.session["_lastbrowse"] = browse
            kind = "category" if f.get("cat") else "menu_browse"
            tracking.track(request, kind, detail=f.get("cat") or "", sort=f.get("sort"), total=total)
    if suggestions:
        ids = [str(s.get("product_id")) for s in suggestions]
        request.session["_lastsugg"] = ids   # so cart_add can flag suggestion-sourced adds
        tracking.track(request, "suggestions_shown", dedupe_key=",".join(sorted(ids)),
                       detail=f"{len(ids)} suggested", ids=ids)


@login_required
@rate_limit("product", limit=240, window=60)
@require_http_methods(["GET"])
def product(request, product_id):
    """Full product detail page — lab data, terpene + effect explanations (Dutchie/
    happytimeweed style). Reads the trusted cached inventory row by ProductId."""
    if not request.session.get("acct_id"):
        return redirect("begin")
    store = _active_store(request)
    p = catalog.find_item(store.name, product_id=product_id) if store else None
    if not p:
        return render(request, "pos/product.html",
                      {"missing": True, "acct_name": request.session.get("acct_name")})
    effects = [(e, education.effect_info(e)) for e in (p.get("effects") or [])]
    terp_aroma_effect = education.terpene_info(p.get("terpene"))
    tracking.track(request, "product_view", product=p, dedupe_key=p.get("product_id"))
    tracking.accrue_taste(request, p, weight=1)   # live personalization signal
    similar = [s for s in catalog.query(catalog.get_inventory(store.name), None,
                                        {"cat": p["cat_key"], "sort": "popular"})
               if str(s.get("product_id")) != str(p.get("product_id"))][:6]
    return render(request, "pos/product.html", {
        "p": p, "effects": effects, "terp": terp_aroma_effect,
        "strain_blurb": education.strain_type_info(p.get("strain_type")),
        "similar": similar, "cart": request.session.get("cart", []),
        "acct_name": request.session.get("acct_name"),
    })


_TRUSTED_ITEM_KEYS = ("ProductId", "BatchId", "SerialNo", "UnitPrice",
                      "RecUnitPrice", "ProductDesc", "CannbisProduct")


@login_required
@require_http_methods(["POST"])
def cart_add(request):
    """SECURITY: the price/serial/batch are NEVER taken from the client. We re-resolve
    the line from the server's cached inventory by ProductId; only quantity is trusted
    from the request. (Audit finding: client-trusted cart line -> live register write.)"""
    store = _active_store(request)
    cart = request.session.get("cart", [])
    try:
        cnt = max(1, min(99, int(request.POST.get("Cnt") or 1)))
    except (TypeError, ValueError):
        cnt = 1
    p = catalog.find_item(store.name, product_id=request.POST.get("ProductId")) if store else None
    if not p:
        ctx = _cart_ctx(cart)
        ctx["add_error"] = "Item unavailable — refresh the menu."
        return render(request, "pos/_cart.html", ctx)
    item = {k: p.get(k) for k in _TRUSTED_ITEM_KEYS}
    item["Discount"] = 0.0
    # Live price-check at add: the browse cache can be ~8 min stale, so confirm the
    # current price + auto-discount + availability straight from Dutchie for THIS serial.
    # Best-effort — any failure falls back to the cached price so a hiccup never blocks a
    # sale. (The authoritative discounts still apply at submit via RunAutoDiscount=True.)
    serial = p.get("SerialNo")
    if store and serial:
        try:
            live = PosRegisterClient.parse_price_check(_client(store).price_check(serial))
            logger.info("price_check serial=%s -> %s", serial, live)
            if live["available"] is not None and live["available"] <= 0:
                ctx = _cart_ctx(cart)
                ctx["add_error"] = f"{item.get('ProductDesc') or 'Item'} is out of stock."
                return render(request, "pos/_cart.html", ctx)
            if live["price"]:
                item["UnitPrice"] = live["price"]
            if live["rec_price"]:
                item["RecUnitPrice"] = live["rec_price"]
            if live["discount"]:
                item["Discount"] = live["discount"]
        except Exception as exc:
            logger.warning("price_check failed for %s (using cached price): %s", serial, exc)
    item["Cnt"] = cnt
    cart.append(item)
    request.session["cart"] = cart
    # `item` is the trimmed trusted cart line (no brand/category) — pass them from the full
    # product `p`, and flag when this add came from a suggestion the customer was just shown.
    tracking.track(request, "item_add", product=item, price=item.get("UnitPrice"),
                   discount=item.get("Discount"), qty=cnt,
                   brand=p.get("brand"), category=p.get("cat_key") or p.get("category"),
                   from_suggestion=str(item.get("ProductId")) in (request.session.get("_lastsugg") or []))
    tracking.accrue_taste(request, p, weight=3)   # an add is a stronger signal than a view
    return render(request, "pos/_cart.html", _cart_ctx(cart))


def _cart_ctx(cart):
    total = sum(float(it.get("UnitPrice") or 0) * int(it.get("Cnt") or 1) for it in cart)
    return {"cart": cart, "cart_total": total}


@login_required
@require_http_methods(["POST"])
def cart_remove(request):
    idx = int(request.POST.get("idx", -1))
    cart = request.session.get("cart", [])
    if 0 <= idx < len(cart):
        tracking.track(request, "item_remove", product=cart.pop(idx))
    request.session["cart"] = cart
    return render(request, "pos/_cart.html", _cart_ctx(cart))


@login_required
@require_http_methods(["POST"])
def cart_submit(request):
    store = _active_store(request)
    cart = request.session.get("cart", [])
    acct_id = request.session.get("acct_id")
    ctx = {"store": store}
    if not (store and acct_id and cart):
        ctx["error"] = "need a store, a selected customer (AcctId), and at least one item"
        return render(request, "pos/_submit_result.html", ctx)
    try:
        result = _client(store).submit_cart(int(acct_id), cart)
        record_write(store.name, "submit", ok=True, acct_id=int(acct_id),
                     shipment_id=result["shipment_id"],
                     summary=f"{len(cart)} items -> Ready for pickup",
                     username=getattr(request.user, "username", ""))
        total = _cart_ctx(cart)["cart_total"]
        tracking.track(request, "checkout", detail=f"{len(cart)} items",
                       shipment_id=result["shipment_id"], total=total)
        tracking.end_visit(request, "checked_out", shipment_id=result["shipment_id"],
                           cart_total=total)
        # Checkout done → clear the session and bounce to the start page for the
        # next customer (HX-Redirect makes htmx do a full client-side navigation).
        for k in ("acct_id", "acct_name", "acct_phone", "cart"):
            request.session.pop(k, None)
        ctx["result"] = result
        resp = render(request, "pos/_submit_result.html", ctx)
        resp["HX-Redirect"] = reverse("begin")
        return resp
    except Exception as exc:
        record_write(store.name, "submit", ok=False,
                     acct_id=int(acct_id) if str(acct_id).isdigit() else None,
                     summary=str(exc)[:200], username=getattr(request.user, "username", ""))
        logger.warning("cart submit failed: %s", exc)
        tracking.track(request, "checkout_failed", detail=str(exc)[:120])
        ctx["error"] = "Submit failed — please try again."
    return render(request, "pos/_submit_result.html", ctx)


# ── session-activity dashboard (operator-facing, read-only) ───────────────────
_DATE_WINDOWS = {"today": 1, "7d": 7, "30d": 30, "all": None}

_EVENT_META = {
    "login": ("🔑", "Logged in"), "visit_start": ("🟢", "Visit started"),
    "id_scan": ("🪪", "ID scanned"), "customer_search": ("🔍", "Customer search"),
    "customer_selected": ("👤", "Customer selected"), "profile_view": ("📇", "Viewed profile"),
    "profile_full_view": ("📋", "Viewed full profile"), "menu_browse": ("🧭", "Browsed menu"),
    "search": ("🔎", "Searched"), "category": ("🗂️", "Category"),
    "product_view": ("👁️", "Viewed product"), "suggestions_shown": ("✨", "Suggestions shown"),
    "item_add": ("➕", "Added to cart"), "item_remove": ("➖", "Removed from cart"),
    "checkout": ("✅", "Checked out"), "abandon": ("🚪", "Abandoned"),
    "scan_failed": ("⚠️", "Scan failed"), "lookup_failed": ("⚠️", "Lookup failed"),
    "checkout_failed": ("❌", "Checkout failed"),
    "admin_close": ("🔒", "Admin closed"), "admin_delete": ("🗑️", "Admin deleted"),
}


def _visit_filters(request):
    """Shared store/budtender/outcome/date filters for the session views."""
    g = request.GET
    qs = ShopVisit.objects.all()
    store = g.get("store") or ""
    budtender = g.get("budtender") or ""
    outcome = g.get("outcome") or ""
    win = g.get("win") if g.get("win") in _DATE_WINDOWS else "7d"
    if store:
        qs = qs.filter(store=store)
    if budtender:
        qs = qs.filter(budtender=budtender)
    if outcome:
        qs = qs.filter(outcome=outcome)
    days = _DATE_WINDOWS[win]
    if days:
        qs = qs.filter(started_at__gte=timezone.now() - timezone.timedelta(days=days))
    f = {"store": store, "budtender": budtender, "outcome": outcome, "win": win}
    return qs, f


def _active_visits():
    # Annotate the last event kind in ONE bounded subquery — never `.events.last` in the
    # template (that clones the qs per row = N+1, and this panel polls every 5s).
    last_kind = ShopEvent.objects.filter(visit=OuterRef("pk")).order_by("-at").values("kind")[:1]
    return (ShopVisit.objects.filter(ended_at__isnull=True)
            .annotate(last_kind=Subquery(last_kind)).order_by("-started_at"))


def _require_sessions_admin(request):
    if not request.user.is_staff:
        raise PermissionDenied


def _session_metrics(qs):
    m = qs.aggregate(
        visits=Count("id"),
        active=Count("id", filter=Q(ended_at__isnull=True)),
        checkouts=Count("id", filter=Q(outcome="checked_out")),
        abandoned=Count("id", filter=Q(outcome="abandoned")),
        viewed=Sum("items_viewed"),
        added=Sum("items_added"),
        events=Sum("event_count"),
        revenue=Sum("cart_total", filter=Q(outcome="checked_out")),
    )
    visits = m.get("visits") or 0
    m["checkout_rate"] = round(100 * (m.get("checkouts") or 0) / visits) if visits else 0
    return m


def _admin_event(request, visit, kind, detail=""):
    ShopEvent.objects.create(
        visit=visit, kind=kind, budtender=request.user.username,
        acct_id=getattr(visit, "acct_id", None), detail=detail[:200],
    )
    if visit is not None:
        visit.event_count = (visit.event_count or 0) + 1
        visit.save(update_fields=["event_count"])


@login_required
@rate_limit("sessions", limit=120, window=60)
@require_http_methods(["GET"])
def sessions(request):
    _require_sessions_admin(request)
    qs, f = _visit_filters(request)
    completed = qs.exclude(ended_at__isnull=True)
    page = Paginator(completed, 40).get_page(request.GET.get("page"))
    stores = list(ShopVisit.objects.values_list("store", flat=True).distinct())
    budtenders = list(ShopVisit.objects.values_list("budtender", flat=True).distinct())
    return render(request, "pos/sessions_list.html", {
        "active": _active_visits(), "page": page, "f": f, "metrics": _session_metrics(qs),
        "stores": sorted(s for s in stores if s),
        "budtenders": sorted(b for b in budtenders if b),
        "outcomes": ["checked_out", "abandoned"], "windows": list(_DATE_WINDOWS),
    })


@login_required
@rate_limit("sessions", limit=600, window=60)
@require_http_methods(["GET"])
def sessions_active(request):
    """Live partial — polled by the dashboard every few seconds."""
    _require_sessions_admin(request)
    return render(request, "pos/_active_panel.html", {"active": _active_visits()})


@login_required
@rate_limit("sessions", limit=120, window=60)
@require_http_methods(["GET"])
def session_detail(request, visit_id):
    _require_sessions_admin(request)
    v = get_object_or_404(ShopVisit, pk=visit_id)
    events = [{"e": e, "icon": _EVENT_META.get(e.kind, ("•", e.kind))[0],
               "label": _EVENT_META.get(e.kind, ("•", e.kind))[1]} for e in v.events.all()]
    return render(request, "pos/session_detail.html", {"v": v, "events": events})


@login_required
@rate_limit("sessions", limit=120, window=60)
@require_http_methods(["POST"])
def session_close(request, visit_id):
    _require_sessions_admin(request)
    v = get_object_or_404(ShopVisit, pk=visit_id)
    outcome = request.POST.get("outcome")
    outcome = outcome if outcome in {"checked_out", "abandoned"} else "abandoned"
    if v.ended_at is None:
        v.ended_at = timezone.now()
        v.outcome = outcome
        v.save(update_fields=["ended_at", "outcome"])
        _admin_event(request, v, "admin_close", outcome)
    return redirect("session_detail", visit_id=v.id)


@login_required
@rate_limit("sessions", limit=60, window=60)
@require_http_methods(["POST"])
def session_delete(request, visit_id):
    _require_sessions_admin(request)
    v = get_object_or_404(ShopVisit, pk=visit_id)
    _admin_event(request, None, "admin_delete", f"session {v.id}: {v.acct_name or 'Guest'}")
    v.delete()
    return redirect("sessions")


@login_required
@rate_limit("sessions", limit=60, window=60)
@require_http_methods(["GET"])
def sessions_rollups(request):
    _require_sessions_admin(request)
    qs, f = _visit_filters(request)
    metrics = _session_metrics(qs)
    by_budtender = list(qs.values("budtender").annotate(
        visits=Count("id"),
        checkouts=Count("id", filter=Q(outcome="checked_out")),
        items_added=Sum("items_added"), items_viewed=Sum("items_viewed"),
        revenue=Sum("cart_total", filter=Q(outcome="checked_out")),
    ).order_by("-visits"))
    for b in by_budtender:
        b["rate"] = round(100 * (b["checkouts"] or 0) / b["visits"]) if b["visits"] else 0
    by_customer = list(qs.filter(acct_id__isnull=False).values("acct_id", "acct_name").annotate(
        visits=Count("id"), last=Max("started_at"),
        bought=Count("id", filter=Q(outcome="checked_out")),
    ).order_by("-visits")[:30])
    # Bound the heavy event GROUP BY even when the window is "all" (the event table only
    # grows — retention is indefinite). Cap the scan at <=365d regardless of window.
    floor = timezone.now() - timezone.timedelta(days=_DATE_WINDOWS[f["win"]] or 365)
    ev = ShopEvent.objects.filter(visit__in=qs, at__gte=floor)
    top_lookup = list(ev.filter(kind="product_view").exclude(product_name="")
                      .values("product_id", "product_name").annotate(n=Count("id")).order_by("-n")[:20])
    top_search = list(ev.filter(kind="search").exclude(detail="")
                      .values("detail").annotate(n=Count("id")).order_by("-n")[:20])
    by_event = list(ev.values("kind").annotate(n=Count("id")).order_by("-n")[:12])
    # Top suggested: ids live in each suggestions_shown event's meta list — tally in Python
    # over a bounded slice, resolving names from the looked-up/added events we already have.
    names = {str(r["product_id"]): r["product_name"] for r in top_lookup}
    counter = Counter()
    for e in ev.filter(kind="suggestions_shown").order_by("-at")[:1000]:
        for pid in (e.meta or {}).get("ids", []):
            counter[str(pid)] += 1
    top_suggested = [{"product_id": pid, "product_name": names.get(pid, pid), "n": n}
                     for pid, n in counter.most_common(20)]
    return render(request, "pos/sessions_rollups.html", {
        "f": f, "by_budtender": by_budtender, "by_customer": by_customer,
        "top_lookup": top_lookup, "top_search": top_search, "top_suggested": top_suggested,
        "by_event": by_event, "metrics": metrics,
    })
