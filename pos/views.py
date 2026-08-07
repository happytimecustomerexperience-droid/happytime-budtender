"""Budtender screen views â€” scan/lookup/profile/inventory/cart/submit + auth.

Function views, server-rendered + HTMX partials. Write paths are @login_required.
Dutchie calls are wrapped so missing creds/endpoints degrade to a visible error, never
a 500. Cart lives in the session. Public-ish endpoints are throttled. Lists paginate.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import os
import sys
from collections import Counter

from django.contrib.auth import login as auth_login
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
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
from customers.models import Customer, ShopEvent, ShopVisit, StaffSession
from customers.services import record_write, upsert_customer
from budtender.models import PhoneCartDraft
from budtender.profile_tree import build_category_pref_tree
from budtender.product_similarity import similarity as product_similarity
from dutchie.pos_read import PosReadClient
from dutchie.pos_register_client import PosRegisterClient
from dutchie.backoffice_customer_client import BackofficeCustomerClient
from bundles import customers as bundle_customers
from bundles.resolver import public_potency
from dutchie import lab as dutchie_lab
from dutchie import stores as dutchie_stores
from dutchie.stores import load_stores

from . import catalog, education, imagemap, pairing, persona, ranking

logger = logging.getLogger(__name__)

MAX_LIST = 40  # cap any rendered list (pagination ceiling)
MENU_PAGE = 24  # products per menu page (paginated)


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        return _with_register(request, stores[lock])
    name = request.POST.get("store") or request.GET.get("store") or request.session.get("store")
    if name and name in stores:
        request.session["store"] = name
        return _with_register(request, stores[name])
    if stores:
        first = next(iter(stores))
        request.session["store"] = first
        return _with_register(request, stores[first])
    return None


def _with_register(request, store):
    """Override the store's default register with the one the operator picked at login
    (session `register_id`) — so a checkout writes to their chosen terminal. Returns a
    copy (never mutates the cached Store)."""
    rid = request.session.get("register_id")
    try:
        if rid and str(rid).isdigit() and int(rid) != int(getattr(store, "register_id", 0) or 0):
            return dataclasses.replace(store, register_id=int(rid))
    except (TypeError, ValueError):
        pass
    return store


def _client(store):
    return PosRegisterClient(store)


def _rest_client(store):
    key = getattr(store, "api_key", "") or ""
    return PosReadClient(key) if key and "pytest" not in sys.modules else None


def _backoffice_client(store):
    if "pytest" in sys.modules or not store or not (store.base_url and store.username and store.password):
        return None
    return BackofficeCustomerClient(store)


# ── roles + shift ─────────────────────────────────────────────────────────────
def _role(request):
    return request.session.get("role") or "budtender"


def _is_door(request):
    return _role(request) == "door"


def _require_not_door(request):
    """Door staff can browse but never touch the cart / checkout / claim."""
    if _is_door(request):
        raise PermissionDenied("door role cannot check out")


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


def _current_shift(request):
    sid = request.session.get("staff_session_id")
    if not sid:
        return None
    try:
        return StaffSession.objects.filter(id=sid, logout_at__isnull=True).first()
    except Exception:
        return None


def _all_registers():
    """Live Dutchie registers per store, flattened + cached (login page reads this)."""
    key = "pos:registers:v1"
    hit = cache.get(key)
    if hit is not None:
        return hit
    out = []
    for name, store in _stores().items():
        try:
            for r in PosRegisterClient(store).get_registers():
                out.append({"store": name, "id": r.get("id"),
                            "name": r.get("TerminalName") or f"Register {r.get('id')}",
                            "room": r.get("RoomNo") or ""})
        except Exception as exc:
            logger.warning("get_registers(%s) failed: %s", name, exc)
    cache.set(key, out, 3600)
    return out


def _register_name(store, register_id):
    for r in _all_registers():
        if r["store"] == store and str(r["id"]) == str(register_id):
            return r["name"]
    return ""


def _login_pickers():
    return {"stores": list(_stores().keys()), "registers": _all_registers()}


def _set_session_customer(request, acct_id, name, phone):
    """The session-customer state that scan/profile/claim all set (single source)."""
    request.session["acct_id"] = acct_id
    request.session["acct_name"] = name or ""
    request.session["acct_phone"] = phone or ""
    request.session.setdefault("cart", [])
    allowed = request.session.get("guests") or {}
    allowed[str(acct_id)] = {"name": name or "", "phone": phone or ""}
    request.session["guests"] = allowed


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


# â”€â”€ auth (budtender-facing login, mobile) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@rate_limit("login", limit=10, window=300)
def login_view(request):
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            role = request.POST.get("role")
            role = role if role in ("budtender", "door") else "budtender"
            if request.user.is_superuser:
                role = "admin"                # privilege is server-side, never the client's word
            stores = _stores()
            store = request.POST.get("location") or ""
            if store not in stores:
                store = next(iter(stores), "")
            register_id = str(request.POST.get("register") or "")
            request.session["role"] = role
            request.session["store"] = store
            request.session["register_id"] = register_id
            try:
                ss = StaffSession.objects.create(
                    user=request.user, username=request.user.username, role=role,
                    store=store, register_id=register_id,
                    register_name=_register_name(store, register_id), ip=_client_ip(request))
                request.session["staff_session_id"] = ss.id
            except Exception as exc:
                logger.warning("StaffSession create failed: %s", exc)
            tracking.track(request, "login")
            return redirect("door" if role == "door" else "screen")
    else:
        form = AuthenticationForm(request)
    return render(request, "pos/login.html", {"form": form, **_login_pickers()})


def logout_view(request):
    ss = _current_shift(request)
    if ss is not None:
        try:
            ss.logout_at = timezone.now()
            ss.save(update_fields=["logout_at"])
        except Exception as exc:
            logger.warning("StaffSession close failed: %s", exc)
    tracking.track(request, "logout")   # standalone shift event (allowed with no open visit)
    auth_logout(request)
    return redirect("login")


# â”€â”€ begin session (start gate: scan ID + phone -> 21 check -> find/create) â”€â”€â”€â”€â”€
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
    # A customer is now selected, so go straight to the menu rather than back to the
    # station — the station's whole job was picking this person.
    return redirect("shop")


def _normalize_name(value):
    return " ".join(str(value or "").lower().split())


def _normalize_phone_match(value):
    digits = "".join(c for c in str(value or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _guest_sort_key(row, *, phone="", name=""):
    phone_digits = _normalize_phone_match(phone)
    name_key = _normalize_name(name)
    row_phone = _normalize_phone_match(row.get("phone"))
    row_name = _normalize_name(row.get("name"))
    return (
        0 if phone_digits and row_phone == phone_digits else 1,
        0 if name_key and row_name == name_key else 1,
        int(row.get("acct_id") or 0),
    )


def _sort_guests(guests, *, phone="", name=""):
    return sorted(guests, key=lambda row: _guest_sort_key(row, phone=phone, name=name))


def _pick_guest(guests, *, phone="", name=""):
    """Pick an exact identity match from a fuzzy POS search response."""
    return _sort_guests(guests, phone=phone, name=name)[0] if guests else None


def _apply_dutchie_customer(scan, row):
    """Fill missing scan fields from the authoritative Dutchie customer row."""
    if not isinstance(row, dict):
        return
    first = row.get("firstName") or row.get("FirstName") or ""
    last = row.get("lastName") or row.get("LastName") or ""
    values = {
        "first_name": first, "middle_name": row.get("middleName") or row.get("MiddleName") or "", "last_name": last,
        "phone": row.get("phone") or row.get("cellPhone") or row.get("CellPhone") or row.get("Phone") or "",
        "email": row.get("emailAddress") or row.get("EmailAddress") or row.get("email") or "",
        "address": row.get("address1") or row.get("Address1") or row.get("street") or "",
        "address2": row.get("address2") or row.get("Address2") or row.get("street2") or "",
        "city": row.get("city") or row.get("City") or "", "state": row.get("state") or row.get("State") or "",
        "postal_code": row.get("postalCode") or row.get("PostalCode") or row.get("postal_code") or "",
        "mjstateidno": row.get("mmjidNumber") or row.get("MJStateIDNo") or "",
        "id_number": row.get("driversLicenseID") or row.get("DriversLicense") or "",
        "id_expiration": str(row.get("DriversLicenseExpiration") or "")[:10],
        "gender": row.get("gender") or row.get("Gender") or "",
        "birth_date": str(row.get("dateOfBirth") or row.get("PatientDOB") or row.get("DOB") or "")[:10],
    }
    for key, value in values.items():
        if value and not scan.get(key):
            scan[key] = value
    if not scan.get("accts_name") and (first or last):
        scan["accts_name"] = f"{first} {last}".strip()
    if row.get("Code"):
        scan.setdefault("dutchie_code", row["Code"])
    if row.get("CustomerTypeId") is not None:
        scan.setdefault("customer_type_id", row["CustomerTypeId"])


def _sync_customer_to_dutchie(store, scan, acct_id):
    """Write then re-read the canonical Dutchie row when the API is available."""
    if not acct_id:
        return None
    backoffice = _backoffice_client(store)
    if backoffice is not None:
        try:
            row = backoffice.find_customer(acct_id, scan.get("accts_name") or "")
            if row:
                backoffice.update_customer(row, scan)
                refreshed = backoffice.find_customer(acct_id, scan.get("accts_name") or "") or row
                _apply_dutchie_customer(scan, refreshed)
                return refreshed
        except Exception as exc:
            logger.warning("Dutchie Backoffice customer sync unavailable: %s", exc)
    client = _rest_client(store)
    if client is None:
        return None
    try:
        client.save_customer(
            customer_id=int(acct_id), first_name=scan.get("first_name", ""),
            last_name=scan.get("last_name", ""), address=scan.get("address", ""),
            address2=scan.get("address2", ""), city=scan.get("city", ""),
            state=scan.get("state", ""), postal_code=scan.get("postal_code", ""),
            phone=scan.get("phone", ""), email=scan.get("email", ""),
            birth_date=scan.get("birth_date", ""),
            mjstateidno=scan.get("mjstateidno", ""), id_number=scan.get("id_number", ""),
        )
        row = client.customer_lookup(
            phone=scan.get("phone", ""), email=scan.get("email", ""),
            first_name=scan.get("first_name", ""), last_name=scan.get("last_name", ""),
            birth_date=scan.get("birth_date", ""), mjstateidno=scan.get("mjstateidno", ""))
        if row:
            _apply_dutchie_customer(scan, row)
        return row
    except Exception as exc:
        logger.warning("Dutchie customer profile sync unavailable: %s", exc)
    return None


def _create_guest_from_scan(client, scan):
    return client.create_guest(
        first_name=scan["first_name"], last_name=scan.get("last_name", ""),
        dob=scan["birth_date"], phone=scan.get("phone", ""),
        email=scan.get("email", ""), mj_state_id=scan.get("mjstateidno", ""),
        dl_id=scan.get("id_number", ""), address=scan.get("address", ""),
        address2=scan.get("address2", ""), city=scan.get("city", ""),
        state=scan.get("state", ""), postal_code=scan.get("postal_code", ""))


def _resolve_or_create(client, scan, phone, rest_client=None):
    """Look up by strong identity first, then POS phone/name; create if absent.
    Returns (acct_id, name, resolved_phone, how)."""
    name = (scan.get("accts_name") or "").strip()
    if rest_client is not None:
        try:
            row = rest_client.customer_lookup(
                phone=phone or scan.get("phone", ""), email=scan.get("email", ""),
                first_name=scan.get("first_name", ""), last_name=scan.get("last_name", ""),
                birth_date=scan.get("birth_date", ""), mjstateidno=scan.get("mjstateidno", ""))
        except Exception as exc:
            logger.warning("Dutchie REST customer lookup unavailable: %s", exc)
            row = None
        if row:
            _apply_dutchie_customer(scan, row)
            acct = (row.get("customerId") or row.get("CustomerId") or row.get("Guest_id")
                    or row.get("AcctId") or row.get("id"))
            if acct:
                return (acct, scan.get("accts_name") or name,
                        scan.get("phone") or phone, "public")
    if phone:
        g = _parse_guests(client.guest_search(phone))
        match = _pick_guest(g, phone=phone, name=name)
        if match:
            return match["acct_id"], match["name"], match.get("phone") or phone, "phone"
    if name:
        g = _parse_guests(client.guest_search(name))
        match = _pick_guest(g, name=name)
        if match:
            return match["acct_id"], match["name"], match.get("phone") or phone, "name"
    if scan.get("first_name") and scan.get("birth_date"):
        scan = {**scan, "phone": phone or scan.get("phone", "")}
        gid = _create_guest_from_scan(client, scan)
        if gid:
            disp = scan.get("accts_name") or f"{scan['first_name']} {scan.get('last_name', '')}".strip()
            return gid, disp, phone or scan.get("phone", ""), "created"
    return None, None, phone or scan.get("phone", ""), "none"


def _resolve_scanned_customer(client, scan, phone, rest_client=None, backoffice_client=None):
    """Resolve strong scan identity, otherwise return every fuzzy name candidate."""
    name = (scan.get("accts_name") or "").strip()
    if rest_client is not None:
        try:
            row = rest_client.customer_lookup(
                phone=phone or scan.get("phone", ""), email=scan.get("email", ""),
                first_name=scan.get("first_name", ""), last_name=scan.get("last_name", ""),
                birth_date=scan.get("birth_date", ""), mjstateidno=scan.get("mjstateidno", ""))
        except Exception as exc:
            logger.warning("Dutchie REST customer lookup unavailable: %s", exc)
            row = None
        if row:
            _apply_dutchie_customer(scan, row)
            acct = (row.get("customerId") or row.get("CustomerId") or row.get("Guest_id")
                    or row.get("AcctId") or row.get("id"))
            if acct:
                return acct, scan.get("accts_name") or name, scan.get("phone") or phone, "public", []

    if phone:
        guests = _parse_guests(client.guest_search(phone))
        match = _pick_guest(guests, phone=phone, name=name)
        if match and _normalize_phone_match(match.get("phone")) == _normalize_phone_match(phone):
            return match["acct_id"], match["name"], match.get("phone") or phone, "phone", []

    if name:
        guests = _merge_guests(_parse_guests(client.guest_search(name)),
                               _backoffice_guests(backoffice_client, name))
        guests = _sort_guests(guests, name=name)[:MAX_LIST]
        for guest in guests:
            guest["possible_name_match"] = True
        return None, None, phone or scan.get("phone", ""), "name_matches", guests
    return None, None, phone or scan.get("phone", ""), "none", []


def _run_scan(request):
    """Run ID scan via posted payload first, then fallback to uploaded images."""
    if request.POST.get("id_payload"):
        from idscan.pipeline import run_id_scan_payload
        return run_id_scan_payload(request.POST["id_payload"])
    from pos_core.uploads import collect_id_images
    files = request.FILES.getlist("images")
    if not files:
        return None
    try:
        images = collect_id_images(files)
    except Exception:
        raise
    from idscan.pipeline import run_id_scan
    return run_id_scan(images)


def _contact_from_request(scan, request, fallback_phone=""):
    phone = _normalize_phone_match(request.POST.get("phone") or scan.get("phone") or fallback_phone)
    if len(phone) != 10:
        return "Phone number is required."
    scan["phone"] = phone
    email = (request.POST.get("email") or scan.get("email") or "").strip()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            return "Enter a valid email address or leave it blank."
        scan["email"] = email
    for field in ("address", "address2", "city", "state", "postal_code"):
        value = (request.POST.get(field) or "").strip()
        if value:
            scan[field] = value
    return ""


def _queue_customer(request, *, acct_id, name, phone, how):
    """Queue one canonical customer once; a later budtender claim starts their cart."""
    store_name = request.session.get("store") or ""
    if not store_name:
        return None
    queued = ShopVisit.objects.filter(store=store_name, status="queued", ended_at__isnull=True)
    if acct_id:
        existing = queued.filter(acct_id=acct_id).first()
    else:
        existing = queued.filter(phone=phone).first() if phone else None
    if existing:
        return existing
    visit = ShopVisit.objects.create(
        store=store_name, status="queued", how_started=how, acct_id=acct_id or None,
        acct_name=name or "Guest", phone=phone or "",
        staff_session_id=request.session.get("staff_session_id"))
    ShopEvent.objects.create(visit=visit, kind="queued", budtender=request.user.username,
                             acct_id=acct_id or None, detail=(name or phone)[:200])
    return visit


def _pending_profile_template(request):
    return "pos/_door_profile.html" if request.POST.get("queue") == "1" else "pos/_profile.html"


@login_required
@rate_limit("start", limit=30, window=60)
@require_http_methods(["POST"])
def start(request):
    store = _active_store(request)
    phone = "".join(c for c in (request.POST.get("phone") or "") if c.isdigit())
    ctx = {"stores": list(_stores().keys()), "active": request.session.get("store"), "phone": phone}

    # "Continue as guest" â€” quick anonymous Dutchie guest, no profile needed.
    if request.POST.get("guest"):
        if not store:
            ctx["error"] = "no store configured"
            return render(request, "pos/begin.html", ctx)
        try:
            gid = _client(store).create_guest(first_name="Guest", last_name="", dob="", phone="")
        except Exception as exc:
            logger.warning("guest start failed: %s", exc)
            ctx["error"] = "Could not start a guest session â€” try again."
            return render(request, "pos/begin.html", ctx)
        if not gid:
            ctx["error"] = "could not start a guest session"
            return render(request, "pos/begin.html", ctx)
        return _start_session(request, gid, "Guest", "", how="guest")

    scan = _run_scan(request) or {}
    if scan:
        if scan.get("error"):
            ctx["error"] = f"scan failed: {scan['error']}"
            return render(request, "pos/begin.html", ctx)
        ctx["scan"] = scan
        if scan.get("over_21") is False:
            ctx["under21"] = True
            return render(request, "pos/begin.html", ctx)
    if not store:
        ctx["error"] = "no store configured"
        return render(request, "pos/begin.html", ctx)
    if not (phone or scan):
        ctx["error"] = "Scan an ID or enter a phone number to begin."
        return render(request, "pos/begin.html", ctx)

    phone = phone or "".join(c for c in (scan.get("phone") or "") if c.isdigit())
    matches = []
    try:
        if scan:
            acct_id, name, resolved_phone, how, matches = _resolve_scanned_customer(
                _client(store), scan, phone, _rest_client(store), _backoffice_client(store))
        else:
            acct_id, name, resolved_phone, how = _resolve_or_create(
                _client(store), scan, phone, _rest_client(store))
    except Exception as exc:
        logger.warning("start lookup failed: %s", exc)
        ctx["error"] = "Lookup failed â€” try again."
        return render(request, "pos/begin.html", ctx)
    phone = resolved_phone or phone
    if scan:
        if acct_id:
            matches = [{"acct_id": acct_id, "name": name or scan.get("accts_name", ""), "phone": phone}]
        cached = upsert_customer({**scan, "phone": phone})
        request.session["pending_customer_id"] = cached.pk
        allowed = request.session.get("guests") or {}
        for guest in matches:
            allowed[str(guest["acct_id"])] = {
                "name": guest.get("name", ""), "phone": guest.get("phone", "")}
        request.session["guests"] = allowed
        ctx.update({"customer_matches": matches, "can_create_customer": not acct_id,
                    "scan": scan, "phone": phone})
        return render(request, "pos/begin.html", ctx)
    if not acct_id:
        # Persist structured scan data even when Dutchie account is not found.
        ctx["no_account"] = True
        return render(request, "pos/begin.html", ctx)
    how = "scan" if scan else how
    return _start_session(request, acct_id, name, phone, how=how,
                          scan_over21=scan.get("over_21") if scan else None)


@login_required
@require_http_methods(["GET"])
def product_lab(request, product_id):
    """Lab panel for ONE product, fetched when a budtender asks for it.

    lab_result is one Dutchie call per BATCH and batches barely repeat here (4,082
    for 4,743 products), so this is never called for a grid — only for the card
    someone opened. Terpenes and COA urls came back empty on 64/64 sampled batches,
    so in practice this renders THCA + Total + the four cannabinoids that exist.
    """
    _require_not_door(request)
    store = _active_store(request)
    p = catalog.find_item(store.name, product_id=product_id) if store else None
    result = dutchie_lab.lab_result(store.name, p.get("BatchId")) if p else None
    return render(request, "pos/_lab.html",
                  {"lab": result, "potency": public_potency(result)})


# â”€â”€ POS screen (requires an active session) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _advance_to_shop(resp):
    """Send an htmx response on to the menu.

    Customer selection happens on the STATION screen, which has no menu on it — so
    every path that resolves a customer (claim, scan, guest, load order) has to move
    the tablet to /pos/shop/. HX-Redirect makes htmx do a real client-side navigation
    rather than swapping a fragment into a page that no longer shows it.
    """
    resp["HX-Redirect"] = reverse("shop")
    return resp


@login_required
def screen(request):
    """The STATION — queue, orders waiting, scan, lookup. No menu.

    Split out from the old single page because this runs on a tablet at the counter:
    the budtender's first job is to see who is waiting and check someone in, and a
    4,700-product grid underneath that is noise until a customer exists. The menu
    lives at `shop` and is only reachable once one does.
    """
    role = _role(request)
    store_name = request.session.get("store") or ""
    return render(request, "pos/station.html", {
        "stores": list(_stores().keys()),
        "active": request.session.get("store"),
        "cart": request.session.get("cart", []),
        "acct_id": request.session.get("acct_id"),
        "acct_name": request.session.get("acct_name"),
        "role": role,
        "queue": list(_store_queue(store_name)) if role != "door" else [],
        "phone_carts": _phone_cart_queue(store_name) if role != "door" else [],
    })


@login_required
def shop(request):
    """The MENU, for a customer who is already checked in.

    Gated on acct_id: landing here with nobody selected means the session was
    restarted or the tablet was reloaded mid-shift, and the honest answer is to send
    them back to the station rather than show a menu that cannot be checked out.
    """
    role = _role(request)
    if role == "door":
        return redirect("door")
    if not request.session.get("acct_id"):
        return redirect("screen")
    return render(request, "pos/shop.html", {
        "stores": list(_stores().keys()),
        "active": request.session.get("store"),
        "cart": request.session.get("cart", []),
        "acct_id": request.session.get("acct_id"),
        "acct_name": request.session.get("acct_name"),
        "initial_cat": request.GET.get("cat", ""),
        "role": role,
    })


@login_required
@rate_limit("scan", limit=20, window=60)
@require_http_methods(["POST"])
def scan(request):
    store = _active_store(request)
    ctx = {"store": store}
    try:
        scan_result = _run_scan(request)
    except Exception as exc:
        tracking.track(request, "scan_failed", detail=f"upload: {exc}"[:120])
        ctx["error"] = f"upload rejected: {exc}"
        return render(request, "pos/_profile.html", ctx)
    scan_result = scan_result or {}
    if scan_result.get("error"):
        tracking.track(request, "scan_failed", detail=str(scan_result["error"])[:120])
        ctx["error"] = f"scan failed: {scan_result['error']}"
        return render(request, "pos/_profile.html", ctx)
    if scan_result.get("over_21") is False:
        tracking.track(request, "scan_failed", detail="under21")
        ctx["warn"] = "UNDER 21 â€” cannot create a POS session."
        ctx["scan"] = scan_result
        ctx["acct_id"] = None
        return render(request, "pos/_profile.html", ctx)

    acct_id = None
    resolved_name = scan_result.get("accts_name", "")
    matches = []
    if store:
        try:
            scan_phone = "".join(c for c in (scan_result.get("phone") or "") if c.isdigit())
            acct_id, resolved_name, resolved_phone, _, matches = _resolve_scanned_customer(
                _client(store), scan_result, scan_phone, _rest_client(store), _backoffice_client(store))
            if resolved_phone:
                scan_result["phone"] = resolved_phone
            if resolved_name:
                scan_result["accts_name"] = resolved_name
        except Exception as exc:
            logger.warning("scan guest lookup unavailable: %s", exc)
            ctx["warn"] = "Customer lookup unavailable."
    if acct_id:
        matches = [{"acct_id": acct_id, "name": resolved_name or scan_result.get("accts_name", ""),
                    "phone": scan_result.get("phone", "")}]
    cached = upsert_customer(scan_result)
    request.session["pending_customer_id"] = cached.pk
    request.session["acct_phone"] = scan_result.get("phone") or ""
    allowed = request.session.get("guests") or {}
    for guest in matches:
        allowed[str(guest["acct_id"])] = {
            "name": guest.get("name", ""), "phone": guest.get("phone", "")}
    request.session["guests"] = allowed
    ctx.update({"scan": scan_result, "acct_id": None,
                "customer_matches": matches,
                "can_create_customer": not acct_id,
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
        guests = _merge_guests(_parse_guests(_client(store).guest_search(q)),
                               _backoffice_guests(_backoffice_client(store), q))
    except Exception as exc:
        logger.warning("lookup failed: %s", exc)
        tracking.track(request, "lookup_failed", detail=str(exc)[:120])
        ctx["error"] = "Lookup failed â€” try again."
        return render(request, "pos/_guests.html", ctx)
    if request.POST.get("name"):
        for guest in guests:
            guest["possible_name_match"] = True
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
    """POST-only (was a state-mutating GET â€” CSRF retarget). The customer's phone/
    name are taken from the SESSION allow-map populated by a prior lookup/scan, never
    from the request â€” so this can't be used to pull arbitrary customers' PII (IDOR)."""
    acct = request.POST.get("acct")
    allowed = (request.session.get("guests") or {}).get(str(acct))
    if not allowed:
        return render(request, "pos/_profile.html",
                      {"error": "Select a customer from a lookup first."})
    name, phone = allowed.get("name", ""), allowed.get("phone", "")
    scan = {}
    pending_id = request.session.get("pending_customer_id")
    if pending_id:
        pending = Customer.objects.filter(pk=pending_id).first()
        if pending and isinstance(pending.raw_scan, dict):
            scan = {**pending.raw_scan, "accts_name": name, "phone": phone}
            error = _contact_from_request(scan, request, phone)
            if error:
                return render(request, "pos/_profile.html", {"error": error, "scan": scan,
                                                              "can_create_customer": True})
            phone = scan["phone"]
    request.session["acct_id"] = acct
    request.session["acct_name"] = name
    request.session["acct_phone"] = phone
    tracking.start_visit(request, acct_id=acct, name=name, phone=phone, how="lookup")
    _sync_customer_to_dutchie(_active_store(request), scan or {"accts_name": name, "phone": phone}, acct)
    upsert_customer(scan or {"accts_name": name, "phone": phone},
                    dutchie_acct_id=int(acct) if str(acct).isdigit() else None)
    request.session.pop("pending_customer_id", None)
    resp = render(request, "pos/_profile.html", {
        "acct_id": acct, "scan": scan or {"accts_name": name, "phone": phone},
        "history": load_customer_history(acct_id=acct, phone=phone, name=name),
    })
    resp["HX-Trigger"] = "customerChanged"  # re-rank the menu For-You
    return _advance_to_shop(resp)


@login_required
@rate_limit("start", limit=30, window=60)
@require_http_methods(["POST"])
def create_customer(request):
    """Create a new Dutchie customer from the most recent ID scan."""
    store = _active_store(request)
    pending_id = request.session.get("pending_customer_id")
    pending = Customer.objects.filter(pk=pending_id).first() if pending_id else None
    scan = dict(pending.raw_scan or {}) if pending else {}
    ctx = {"scan": scan, "can_create_customer": bool(pending)}
    template = _pending_profile_template(request)
    if not store or not pending:
        ctx["error"] = "Scan the ID again before creating a customer."
        return render(request, template, ctx)
    error = _contact_from_request(scan, request)
    if error:
        ctx["error"] = error
        return render(request, template, ctx)
    if scan.get("over_21") is False:
        ctx["error"] = "This customer is under 21 and cannot be created."
        return render(request, template, ctx)
    if not scan.get("first_name") or not scan.get("birth_date"):
        ctx["error"] = "The ID scan did not provide the required name and birth date."
        return render(request, template, ctx)
    try:
        acct_id = _create_guest_from_scan(_client(store), scan)
    except Exception as exc:
        logger.warning("customer create failed: %s", exc)
        ctx["error"] = "Could not create the Dutchie customer."
        return render(request, template, ctx)
    if not acct_id:
        ctx["error"] = "Dutchie did not return a customer account."
        return render(request, template, ctx)
    _sync_customer_to_dutchie(store, scan, acct_id)
    upsert_customer(scan, dutchie_acct_id=acct_id)
    request.session.pop("pending_customer_id", None)
    name = scan.get("accts_name") or f"{scan['first_name']} {scan.get('last_name', '')}".strip()
    if request.POST.get("queue") == "1":
        queued = _queue_customer(request, acct_id=acct_id, name=name, phone=scan["phone"], how="created")
        if queued is None:
            return render(request, "pos/_door_result.html", {"error": "No store configured for the queue."})
        return render(request, "pos/_door_result.html", {"queued": queued, "name": name, "phone": scan["phone"]})
    return _start_session(request, acct_id, name, scan["phone"], how="created",
                          scan_over21=scan.get("over_21"))


@login_required
@rate_limit("start", limit=30, window=60)
@require_http_methods(["POST"])
def start_existing(request):
    """Continue with a name-match selected after an ID scan."""
    acct = request.POST.get("acct")
    allowed = (request.session.get("guests") or {}).get(str(acct))
    pending_id = request.session.get("pending_customer_id")
    pending = Customer.objects.filter(pk=pending_id).first() if pending_id else None
    if not allowed or not pending:
        return render(request, "pos/_door_profile.html" if request.POST.get("queue") == "1" else "pos/begin.html",
                      {"error": "Scan the ID again before selecting a customer."})
    scan = {**(pending.raw_scan or {}), "accts_name": allowed.get("name", ""),
            "phone": allowed.get("phone", "")}
    error = _contact_from_request(scan, request, allowed.get("phone", ""))
    if error:
        return render(request, "pos/_door_profile.html" if request.POST.get("queue") == "1" else "pos/begin.html",
                      {"error": error, "scan": scan, "can_create_customer": True})
    _sync_customer_to_dutchie(_active_store(request), scan, acct)
    upsert_customer(scan, dutchie_acct_id=int(acct) if str(acct).isdigit() else None)
    request.session.pop("pending_customer_id", None)
    if request.POST.get("queue") == "1":
        queued = _queue_customer(request, acct_id=acct, name=scan["accts_name"], phone=scan["phone"], how="scan")
        if queued is None:
            return render(request, "pos/_door_result.html", {"error": "No store configured for the queue."})
        return render(request, "pos/_door_result.html", {
            "queued": queued, "name": scan["accts_name"], "phone": scan["phone"]})
    return _start_session(request, acct, scan["accts_name"], scan["phone"], how="scan",
                          scan_over21=scan.get("over_21"))


# -- door role: scan people into the shared per-store queue --------------------
@login_required
def door(request):
    """Door landing - scan an ID / enter a phone to add the person to the live queue.
    No cart, no checkout (role-gated). Location is locked to the shift's store."""
    return render(request, "pos/door.html", {
        "store": request.session.get("store"), "role": _role(request),
    })


@login_required
@rate_limit("start", limit=60, window=60)
@require_http_methods(["POST"])
def door_scan(request):
    """Door check-in: preview scanned data, then queue a refreshed canonical customer."""
    store = _active_store(request)
    phone = _normalize_phone_match(request.POST.get("phone") or "")
    name = (request.POST.get("name") or "").strip()
    ctx = {}
    has_scan = bool(request.POST.get("id_payload") or request.FILES.getlist("images"))
    if has_scan:
        try:
            scan = _run_scan(request) or {}
        except Exception as exc:
            ctx["error"] = f"upload rejected: {exc}"
            return render(request, "pos/_door_result.html", ctx)
        if scan.get("error"):
            ctx["error"] = f"scan failed: {scan['error']}"
            return render(request, "pos/_door_result.html", ctx)
        if scan.get("over_21") is False:                     # HARD age flag - do not queue
            return render(request, "pos/_door_result.html",
                          {"under21": True, "name": scan.get("accts_name")})
        name = name or (scan.get("accts_name") or "").strip()
        phone = phone or _normalize_phone_match(scan.get("phone") or "")
        matches = []
        acct_id = None
        if store:
            try:
                acct_id, resolved_name, resolved_phone, _, matches = _resolve_scanned_customer(
                    _client(store), scan, phone, _rest_client(store), _backoffice_client(store))
                if acct_id:
                    matches = [{"acct_id": acct_id, "name": resolved_name or name,
                                "phone": resolved_phone or phone}]
                phone = resolved_phone or phone
            except Exception as exc:
                logger.warning("door customer lookup failed: %s", exc)
                ctx["error"] = "Customer lookup unavailable. Try again."
                return render(request, "pos/_door_result.html", ctx)
        cached = upsert_customer({**scan, "phone": phone})
        request.session["pending_customer_id"] = cached.pk
        allowed = request.session.get("guests") or {}
        for guest in matches:
            allowed[str(guest["acct_id"])] = {
                "name": guest.get("name", ""), "phone": guest.get("phone", "")}
        request.session["guests"] = allowed
        return render(request, "pos/_door_profile.html", {
            "scan": scan, "customer_matches": matches, "can_create_customer": not acct_id,
        })
    if not (name or phone):
        ctx["error"] = "Scan an ID or enter a phone/name to add to the queue."
        return render(request, "pos/_door_result.html", ctx)
    acct_id = None
    if store:
        try:
            parts = name.split()
            acct_id, resolved_name, resolved_phone, _ = _resolve_or_create(
                _client(store), {"accts_name": name, "first_name": parts[0] if parts else "",
                                 "last_name": " ".join(parts[1:])}, phone, _rest_client(store))
            name, phone = resolved_name or name, resolved_phone or phone
        except Exception as exc:
            logger.warning("door lookup failed: %s", exc)
    v = _queue_customer(request, acct_id=acct_id, name=name, phone=phone, how="door")
    if v is None:
        ctx["error"] = "No store configured for the queue."
        return render(request, "pos/_door_result.html", ctx)
    ctx.update({"queued": v, "name": name or "Guest", "phone": phone})
    return render(request, "pos/_door_result.html", ctx)


# -- budtender role: the live queue + claim ------------------------------------
def _store_queue(store_name):
    return (ShopVisit.objects.filter(store=store_name, status="queued", ended_at__isnull=True)
            .order_by("started_at"))


def _phone_cart_queue(store_name):
    """Claimable drafts for this store.

    `store_name` is a POS store key (yakima|mtvernon|pullman) but PhoneCartDraft is
    keyed by happytime location_slug (yakima|mount-vernon|pullman) — so this MUST
    translate, or Mount Vernon drafts are invisible to the Mount Vernon register.
    Open carts are excluded: an `open` row is a shopper still browsing, and loading
    a cart out from under them creates a phantom order nobody placed.
    """
    if not store_name:
        return []
    return list(
        PhoneCartDraft.objects.filter(
            location_slug=dutchie_stores.location_slug(store_name),
            status=PhoneCartDraft.Status.RELEASED,
        ).order_by("-released_at", "-updated_at")[:50]
    )


@login_required
@rate_limit("sessions", limit=600, window=60)
@require_http_methods(["GET"])
def queue_panel(request):
    """Live per-store queue partial - polled every 5s by the budtender screen."""
    _require_not_door(request)
    store_name = request.session.get("store") or ""
    return render(request, "pos/_queue_panel.html",
                  {"queue": list(_store_queue(store_name)), "phone_carts": _phone_cart_queue(store_name)})


@login_required
@rate_limit("start", limit=60, window=60)
@require_http_methods(["POST"])
def claim(request, visit_id):
    """Claim a queued customer: mark claimed (records the wait), resolve the Dutchie
    guest so the budtender can shop/checkout, and adopt this visit as the session's open
    visit - dropping into the existing shop flow via `customerChanged`."""
    _require_not_door(request)
    store = _active_store(request)
    v = ShopVisit.objects.filter(pk=visit_id, status="queued", ended_at__isnull=True).first()
    if v is None:
        return render(request, "pos/_profile.html", {"error": "That customer was already taken."})
    acct_id = v.acct_id
    profile_scan = {"accts_name": v.acct_name or "", "phone": v.phone or ""}
    if store and not acct_id:                               # door only captured identity - resolve now
        try:
            parts = (v.acct_name or "").split()
            profile_scan.update({"first_name": parts[0] if parts else "",
                                 "last_name": " ".join(parts[1:])})
            acct_id, resolved_name, resolved_phone, _ = _resolve_or_create(
                _client(store), profile_scan, v.phone or "", _rest_client(store))
            if resolved_name:
                profile_scan["accts_name"] = resolved_name
            if resolved_phone:
                profile_scan["phone"] = resolved_phone
        except Exception as exc:
            logger.warning("claim guest lookup failed: %s", exc)
    if not acct_id and store:                               # no match -> shop as a guest so checkout works
        try:
            acct_id = _client(store).create_guest(
                first_name=(v.acct_name or "Guest").split(" ")[0], last_name="", dob="", phone=v.phone or "")
        except Exception as exc:
            logger.warning("claim guest create failed: %s", exc)
    v.status = "claimed"
    v.claimed_at = timezone.now()
    v.budtender = v.claimed_by = request.user.username
    v.acct_id = acct_id or v.acct_id
    v.acct_name = profile_scan.get("accts_name") or v.acct_name
    v.phone = profile_scan.get("phone") or v.phone
    v.staff_session_id = request.session.get("staff_session_id") or v.staff_session_id
    v.save(update_fields=["status", "claimed_at", "budtender", "claimed_by", "acct_id",
                          "acct_name", "phone", "staff_session"])
    _set_session_customer(request, v.acct_id, v.acct_name, v.phone)
    request.session["visit_id"] = v.id                     # adopt as the open visit
    _sync_customer_to_dutchie(store, profile_scan, v.acct_id)
    upsert_customer(profile_scan,
                    dutchie_acct_id=int(v.acct_id) if str(v.acct_id or "").isdigit() else None)
    ShopEvent.objects.create(visit=v, kind="claimed", budtender=request.user.username,
                             acct_id=v.acct_id, detail=v.wait_display)

    # If this person already ordered online, their cart is waiting — load it here so
    # the budtender doesn't have to spot the separate "Orders waiting" row and match
    # it up by hand with the customer in front of them.
    draft = _waiting_draft_for(store.name if store else "", acct_id=v.acct_id, phone=v.phone)
    order_note = ""
    if draft:
        loaded, skipped = _load_draft_lines(request, draft, store)
        draft.status = PhoneCartDraft.Status.CLAIMED
        draft.claimed_at = timezone.now()
        audit = draft.audit or []
        audit.append({"at": timezone.now().isoformat(), "action": "pos_claim_via_queue",
                      "loaded_lines": loaded, "skipped": skipped[:20], "visit": v.id})
        draft.audit = audit[-100:]
        draft.save(update_fields=["status", "claimed_at", "audit", "updated_at"])
        order_note = f"Loaded their online order — {loaded} item{'' if loaded == 1 else 's'}."
        if skipped:
            order_note += " Needs manual review: " + ", ".join(skipped[:3])

    resp = render(request, "pos/_profile.html", {
        "acct_id": v.acct_id, "scan": {"accts_name": v.acct_name, "phone": v.phone},
        "history": load_customer_history(acct_id=v.acct_id, phone=v.phone, name=v.acct_name),
        "order_note": order_note,
    })
    resp["HX-Trigger"] = "customerChanged"
    return _advance_to_shop(resp)


@login_required
@rate_limit("start", limit=30, window=60)
@require_http_methods(["POST"])
def guest_start(request):
    """Continue-as-guest from the budtender screen (mirrors the begin-gate guest path)."""
    _require_not_door(request)
    store = _active_store(request)
    if not store:
        return render(request, "pos/_profile.html", {"error": "no store configured"})
    try:
        gid = _client(store).create_guest(first_name="Guest", last_name="", dob="", phone="")
    except Exception as exc:
        logger.warning("guest start failed: %s", exc)
        gid = None
    if not gid:
        return render(request, "pos/_profile.html", {"error": "Could not start a guest session."})
    _set_session_customer(request, gid, "Guest", "")
    tracking.start_visit(request, acct_id=gid, name="Guest", phone="", how="guest")
    resp = render(request, "pos/_profile.html",
                  {"acct_id": gid, "scan": {"accts_name": "Guest", "phone": ""}})
    resp["HX-Trigger"] = "customerChanged"
    return _advance_to_shop(resp)


# -- customer profile (2 pages: preview + full transaction history) ------------
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


def _find_product_in_inventory(inv, row):
    """Try to resolve a purchas history row into current inventory for direct open-linking."""
    if not row:
        return None
    pid = row.get("product_id") or row.get("product_id") or row.get("sku")
    if pid:
        for p in inv:
            if str(p.get("product_id")) == str(pid):
                return p
    name = (row.get("product") or "").strip().lower()
    brand = (row.get("brand") or "").strip().lower()
    for p in inv:
        if name and name == (p.get("name") or "").strip().lower():
            if not brand or brand == (p.get("brand") or "").strip().lower():
                return p
        if brand and brand == (p.get("brand") or "").strip().lower() and (hsku := row.get("sku")):
            if str(p.get("product_id")) == str(hsku):
                return p
    return None


def _product_package_id(p):
    for key in ("package_id", "SerialNo", "BatchId", "ProductId", "product_id"):
        val = p.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _product_received_date(p):
    return p.get("received_date") or p.get("receivedDate") or p.get("received_at") or ""


def _inventory_by_identity(inv):
    by_pid = {}
    by_name = {}
    for p in inv or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("product_id") or p.get("ProductId") or "").strip()
        if pid:
            by_pid.setdefault(pid, []).append(p)
        name = (p.get("name") or "").strip().lower()
        brand = (p.get("brand") or "").strip().lower()
        key = (name, brand)
        if name:
            by_name.setdefault(key, []).append(p)
    return by_pid, by_name


def _resolve_inventory_matches(inv, row):
    by_pid, by_name = _inventory_by_identity(inv)
    row_pid = str(row.get("product_id") or row.get("sku") or "").strip()
    if row_pid and row_pid in by_pid:
        return by_pid[row_pid]
    name = (row.get("product_name") or row.get("product") or row.get("name") or "").strip().lower()
    brand = (row.get("brand") or "").strip().lower()
    if name:
        matches = by_name.get((name, brand)) or by_name.get((name, ""))
        if matches:
            return matches
    out = []
    for p in inv or []:
        if not isinstance(p, dict):
            continue
        if row.get("category") and str(p.get("cat_key") or p.get("category") or "").strip().lower() != str(row.get("category")).strip().lower():
            continue
        if row.get("subcategory") and str(p.get("subcategory") or "").strip().lower() != str(row.get("subcategory")).strip().lower():
            continue
        out.append(p)
    return out


def _backoffice_guests(client, name: str) -> list[dict]:
    if client is None or not name or len(_normalize_phone_match(name)) >= 7:
        return []
    rows = client.search_customers(name)
    return [{
        "acct_id": row.get("Id"),
        "name": row.get("Name") or " ".join(filter(None, [row.get("FirstName"), row.get("LastName")])),
        "phone": row.get("CellPhone") or row.get("Phone") or "",
        "patient_type": row.get("CustomerType") or "",
        "is_medical": bool(row.get("MJStateIDNo")),
        "pt_label": row.get("CustomerType") or "",
        "last": str(row.get("LastTransaction") or "")[:10],
        "dutchie_customer": row,
    } for row in rows if row.get("Id") is not None]


def _merge_guests(*groups):
    merged = {}
    for group in groups:
        for guest in group:
            key = str(guest.get("acct_id") or "")
            if key:
                merged.setdefault(key, guest)
    return list(merged.values())


def _package_summary(inv_rows):
    packages = []
    for p in inv_rows or []:
        if not isinstance(p, dict):
            continue
        packages.append({
            "package_id": _product_package_id(p),
            "received_date": _product_received_date(p),
            "price": p.get("price"),
            "bucket": p.get("bucket", ""),
            "in_stock": _affw(p.get("qty")) > 0,
        })
    packages.sort(key=lambda r: str(r.get("received_date") or ""), reverse=True)
    return packages[:12]


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
    if isinstance(profile, dict):
        profile.setdefault("orders", profile.get("total_orders", 0))
    # purchase_history comes from an uncontrolled remote DB; keep only dict rows so a
    # stray null/string element degrades instead of 500-ing (same guard as ranking/suggest).
    # COPY each row â€” `profile` is now cached/shared, so the full-page `h["spend"]=` below
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
        sel_cat = (request.GET.get("cat") or "").strip().lower()
        sel_sub = (request.GET.get("subcat") or "").strip().lower()
        pref_tree = build_category_pref_tree(hist)
        selected_cat_label = ""
        selected_sub_label = ""
        selected_subcats = []
        table_rows = []

        def _norm(v):
            return str(v or "").strip().lower()

        for node in pref_tree:
            cat_key = _norm(node.get("category"))
            if not selected_cat_label and (not sel_cat or cat_key == sel_cat):
                selected_cat_label = node.get("category") or ""
            if sel_cat and cat_key != sel_cat:
                continue
            subcats = []
            for sub_node in node.get("subcategories", []):
                sub_key = _norm(sub_node.get("subcategory"))
                subcats.append(sub_node)
                if not selected_sub_label and (not sel_sub or sub_key == sel_sub):
                    selected_sub_label = sub_node.get("subcategory") or ""
                if sel_sub and sub_key != sel_sub:
                    continue
                selected_subcats.append(sub_node)
                for row in sub_node.get("products") or []:
                    if not isinstance(row, dict):
                        continue
                    r = dict(row)
                    matches = _resolve_inventory_matches(inv, r)
                    r["spend"] = round(_affw(r.get("last_price")) * _affw(r.get("qty")), 2)
                    r["in_stock"] = bool(matches)
                    r["current_price"] = (matches[0].get("price") if matches else None)
                    r["current_bucket"] = (matches[0].get("bucket") if matches else "")
                    r["package_ids"] = sorted({m.get("package_id") or m.get("SerialNo") or m.get("BatchId") or m.get("ProductId")
                                               for m in matches if m.get("package_id") or m.get("SerialNo") or m.get("BatchId") or m.get("ProductId")},
                                              key=lambda x: str(x))
                    r["menu_link"] = ""
                    if matches:
                        first = matches[0]
                        pid = first.get("ProductId") or first.get("product_id")
                        if pid:
                            r["menu_link"] = reverse("product", args=[pid])
                    if not r["menu_link"]:
                        q = (r.get("product_name") or r.get("product") or r.get("name") or "").strip()
                        params = [f"q={q}"]
                        if r.get("category"):
                            params.append(f"cat={str(r.get('category')).strip().lower()}")
                        if r.get("subcategory"):
                            params.append(f"subcat={str(r.get('subcategory')).strip().lower()}")
                        r["menu_link"] = reverse("shop") + ("?" + "&".join(params) if params else "")
                    r["similar_rows"] = []
                    if not r["in_stock"]:
                        cands = []
                        for item in inv:
                            if not isinstance(item, dict):
                                continue
                            if _norm(item.get("cat_key") or item.get("category")) != _norm(r.get("category")):
                                continue
                            if _norm(item.get("subcategory")) != _norm(r.get("subcategory")):
                                continue
                            sim = product_similarity(r, item)
                            cands.append((sim.get("score", 0.0), item, sim.get("reasons") or []))
                        cands.sort(key=lambda x: x[0], reverse=True)
                        r["similar_rows"] = [{
                            "name": c[1].get("name"),
                            "price": c[1].get("price"),
                            "bucket": c[1].get("bucket", ""),
                            "reasons": c[2],
                            "product_id": c[1].get("ProductId") or c[1].get("product_id"),
                        } for c in cands[:5]]
                    table_rows.append(r)
            if sel_cat and cat_key == sel_cat and not selected_sub_label and subcats:
                selected_sub_label = subcats[0].get("subcategory") or ""
        if not selected_cat_label and pref_tree:
            selected_cat_label = pref_tree[0].get("category") or ""
        if not selected_sub_label and selected_subcats:
            selected_sub_label = selected_subcats[0].get("subcategory") or ""
        table_rows = sorted(table_rows, key=lambda h: str(h.get("last_bought_at") or ""), reverse=True)
        ctx.update({
            "pref_tree": pref_tree,
            "selected_cat": sel_cat,
            "selected_subcat": sel_sub,
            "selected_cat_label": selected_cat_label,
            "selected_subcat_label": selected_sub_label,
            "pref_subcats": selected_subcats,
            "pref_rows": table_rows,
            "history": sorted(hist, key=lambda h: str(h.get("last_bought_at") or ""), reverse=True),
        })
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
        "brand_q": g.get("brand_q", ""),
        "subcat": g.get("subcat", ""),
        "strain_type": g.get("strain_type", ""), "effect": g.get("effect", ""),
        "strain": g.get("strain", ""),
        "sort": g.get("sort", "foryou"),
        "price_min": _int("price_min"), "price_max": _int("price_max"),
        "thc_min": _int("thc_min"), "doh_only": g.get("doh_only") == "1",
        "page": max(1, _int("page") or 1),
    }


@login_required
@require_http_methods(["GET"])
def product_lab(request, product_id):
    """Lab panel for ONE product, fetched only when a budtender asks for it.

    `lab_result` is one Dutchie call per BATCH, and batches barely repeat here —
    4,082 of them across 4,743 products — so this must never be called for a whole
    grid. Terpenes and COA urls came back empty on 64/64 sampled batches, so in
    practice this renders THCA + Total + the four cannabinoids that do exist.
    """
    _require_not_door(request)
    store = _active_store(request)
    row = catalog.find_item(store.name, product_id=product_id) if store else None
    result = dutchie_lab.lab_result(store.name, row.get("BatchId")) if row else None
    return render(request, "pos/_lab.html",
                  {"lab": result, "potency": public_potency(result)})


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
    # Cached persisted taste (fast â€” no DB hit per filter change) blended with THIS visit's
    # live behavior, so every customer (new / guest / DB-down) gets a personalized feed.
    profile = load_profile_full_cached(phone) if phone else None
    eff = ranking.blend_session_taste(profile, request.session.get("taste"))
    f = _filters(request)
    try:
        items = catalog.get_inventory(store.name)
    except Exception as exc:
        logger.warning("menu load failed: %s", exc)
        ctx["error"] = "Menu unavailable â€” refresh in a moment."
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
    # (unfiltered) view â€” once they tab/search, just show the filtered grid.
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
        # _menu.html) â€” don't run the 3-pass suggest scan on filtered/keystroke views.
        suggestions=catalog.suggestions(store.name, eff, 6) if (eff and default_view and not carousels) else [],
        carousels=carousels, cart_pairs=cart_pairs,
        persona=persona.summarize(profile),
    )
    _track_browse(request, f, total, ctx["suggestions"])
    return render(request, "pos/_menu.html", ctx)


def _carousels(ranked, profile, n_cats=5, per=15):
    """Per-category rails built by GROUPING the already-ranked feed (one rank, no re-query) â€”
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
    """The priciest line in the cart â€” the cross-sell anchor (resolved to a full product)."""
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
        tracking.track(
            request,
            "suggestions_shown",
            dedupe_key=",".join(sorted(ids)),
            detail=f"{len(ids)} suggested",
            ids=ids,
            recommendation_types=[s.get("recommendation_type") or s.get("type") for s in suggestions],
            buckets=[s.get("bucket", "") for s in suggestions],
            categories=[s.get("category") or s.get("cat_key") for s in suggestions],
            subcategories=[s.get("subcategory", "") for s in suggestions],
        )


@login_required
@rate_limit("product", limit=240, window=60)
@require_http_methods(["GET"])
def product(request, product_id):
    """Full product detail page - lab data, terpene + effect explanations (Dutchie/
    happytimeweed style). Reads the trusted cached inventory row by ProductId."""
    if not request.session.get("acct_id"):
        return redirect("begin")
    store = _active_store(request)
    p = catalog.find_item(store.name, product_id=product_id) if store else None
    if not p:
        return render(request, "pos/product.html",
                      {"missing": True, "acct_name": request.session.get("acct_name")})
    try:
        inv = catalog.get_inventory(store.name) if store else []
    except Exception:
        inv = []
    matches = _resolve_inventory_matches(inv, p) if inv else []
    p["package_ids"] = sorted({
        str(m.get("package_id") or m.get("SerialNo") or m.get("BatchId") or m.get("ProductId") or "")
        for m in matches
        if m.get("package_id") or m.get("SerialNo") or m.get("BatchId") or m.get("ProductId")
    })
    if matches and not p.get("received_date"):
        p["received_date"] = max((m.get("received_date") or "") for m in matches)
    effects = [(e, education.effect_info(e)) for e in (p.get("effects") or [])]
    terp_aroma_effect = education.terpene_info(p.get("terpene"))
    tracking.track(request, "product_view", product=p, dedupe_key=p.get("product_id"))
    tracking.accrue_taste(request, p, weight=1)   # live personalization signal
    similar = [s for s in catalog.query(inv, None,
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
    _require_not_door(request)
    store = _active_store(request)
    cart = request.session.get("cart", [])
    try:
        cnt = max(1, min(99, int(request.POST.get("Cnt") or 1)))
    except (TypeError, ValueError):
        cnt = 1
    p = catalog.find_item(store.name, product_id=request.POST.get("ProductId")) if store else None
    if not p:
        ctx = _cart_ctx(cart)
        ctx["add_error"] = "Item unavailable â€” refresh the menu."
        return render(request, "pos/_cart.html", ctx)
    item = {k: p.get(k) for k in _TRUSTED_ITEM_KEYS}
    item["Discount"] = 0.0
    # Live price-check at add: the browse cache can be ~8 min stale, so confirm the
    # current price + auto-discount + availability straight from Dutchie for THIS serial.
    # Best-effort â€” any failure falls back to the cached price so a hiccup never blocks a
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
    # `item` is the trimmed trusted cart line (no brand/category) â€” pass them from the full
    # product `p`, and flag when this add came from a suggestion the customer was just shown.
    tracking.track(request, "item_add", product=item, price=item.get("UnitPrice"),
                   discount=item.get("Discount"), qty=cnt,
                   brand=p.get("brand"), category=p.get("cat_key") or p.get("category"),
                   subcategory=p.get("subcategory"), bucket=p.get("bucket"),
                   from_suggestion=str(item.get("ProductId")) in (request.session.get("_lastsugg") or []),
                   source_recommendation_type=p.get("recommendation_type") or p.get("type") or "")
    tracking.accrue_taste(request, p, weight=3)   # an add is a stronger signal than a view
    return render(request, "pos/_cart.html", _cart_ctx(cart))


def _cart_ctx(cart):
    total = sum(float(it.get("UnitPrice") or 0) * int(it.get("Cnt") or 1) for it in cart)
    return {"cart": cart, "cart_total": total}


@login_required
@require_http_methods(["POST"])
def cart_remove(request):
    _require_not_door(request)
    idx = int(request.POST.get("idx", -1))
    cart = request.session.get("cart", [])
    if 0 <= idx < len(cart):
        tracking.track(request, "item_remove", product=cart.pop(idx))
    request.session["cart"] = cart
    return render(request, "pos/_cart.html", _cart_ctx(cart))


def _waiting_draft_for(store_name, *, acct_id=None, phone=None):
    """A released online order belonging to this person at this store, or None.

    Matched on Dutchie account id first, then on the phone's last 10 digits — the
    same identity `bundles.customers` resolves an order by. Without this, someone
    who ordered online and then walked in was claimed off the door queue with an
    EMPTY cart, and the budtender had to notice the separate "Orders waiting" row
    and load it by hand — with the customer standing there.
    """
    if not store_name:
        return None
    qs = PhoneCartDraft.objects.filter(
        location_slug=dutchie_stores.location_slug(store_name),
        status=PhoneCartDraft.Status.RELEASED,
    )
    digits = re.sub(r"[^0-9]", "", str(phone or ""))[-10:]
    match = None
    if acct_id:
        match = qs.filter(dutchie_acct_id=str(acct_id)).order_by("-released_at").first()
    if match is None and len(digits) == 10:
        match = qs.filter(contact_phone__endswith=digits).order_by("-released_at").first()
    return match


def _load_draft_lines(request, draft, store):
    """Draft lines -> session cart. Returns (loaded, skipped_names).

    Extracted from `phone_cart_claim` so claiming a queued CUSTOMER can populate the
    cart identically to claiming the order directly — one code path, so the two can
    never drift into pricing the same order differently.
    """
    cart = request.session.get("cart", [])
    skipped, loaded = [], 0
    for line in draft.lines or []:
        if not isinstance(line, dict):
            continue
        product_id = line.get("product_id")
        p = catalog.find_item(store.name, product_id=product_id) if store and product_id else None
        if not p:
            skipped.append(line.get("name") or line.get("sku") or "item")
            continue
        item = {k: p.get(k) for k in _TRUSTED_ITEM_KEYS}
        item["Discount"] = 0.0
        try:
            item["Cnt"] = max(1, min(99, int(line.get("quantity") or 1)))
        except (TypeError, ValueError):
            item["Cnt"] = 1
        # Same live per-serial confirmation `cart_add` does for a walk-in. Without it
        # a claimed online order was priced off the browse cache while an identical
        # walk-in add got a live check — two prices for one product in one POS.
        serial = p.get("SerialNo")
        if store and serial:
            try:
                live = PosRegisterClient.parse_price_check(_client(store).price_check(serial))
                if live["price"] is not None:
                    item["UnitPrice"] = live["price"]
            except Exception as exc:
                logger.warning("price_check failed for %s (using cached price): %s", serial, exc)
        cart.append(item)
        loaded += 1
        tracking.track(request, "item_add", product=item, price=item.get("UnitPrice"),
                       qty=item["Cnt"], brand=p.get("brand"),
                       category=p.get("cat_key") or p.get("category"),
                       subcategory=p.get("subcategory"), bucket=p.get("bucket"),
                       source_recommendation_type="phone_cart")
    request.session["cart"] = cart
    return loaded, skipped


@login_required
@require_http_methods(["POST"])
def phone_cart_claim(request):
    """Load a released phone-cart draft into the normal POS session cart.

    This intentionally stops before checkout. Staff still verifies the customer and uses
    the existing `cart_submit` path, which remains the only Dutchie order writer.
    """
    _require_not_door(request)
    token = str(request.POST.get("draft_token") or "").strip()
    draft = PhoneCartDraft.objects.filter(draft_token=token).first()
    if not draft:
        ctx = _cart_ctx(request.session.get("cart", []))
        ctx["add_error"] = "Phone cart not found."
        return render(request, "pos/_cart.html", ctx)
    if draft.status not in {PhoneCartDraft.Status.RELEASED, PhoneCartDraft.Status.OPEN}:
        ctx = _cart_ctx(request.session.get("cart", []))
        ctx["add_error"] = f"Phone cart is {draft.status}."
        return render(request, "pos/_cart.html", ctx)

    # Translate to the POS store key. Assigning the raw location_slug meant
    # "mount-vernon" was not in load_stores(), so _active_store silently fell back
    # to the FIRST store — a Mount Vernon draft would load against Yakima stock.
    request.session["store"] = dutchie_stores.store_key(draft.location_slug)
    store = _active_store(request)
    loaded, skipped = _load_draft_lines(request, draft, store)
    cart = request.session.get("cart", [])
    draft.status = PhoneCartDraft.Status.CLAIMED
    draft.claimed_at = timezone.now()
    # Attach the customer. cart_submit refuses to run without an AcctId, so a
    # claimed order with nobody selected is a dead end at the register — the
    # budtender would have to re-find them by hand with the shopper standing there.
    customer_note = ""
    if draft.contact_phone or draft.dutchie_acct_id:
        acct_id, cust_name, how = bundle_customers.ensure_customer(draft)
        if acct_id:
            _set_session_customer(request, acct_id, cust_name or draft.pickup_name,
                                  draft.contact_phone)
            if how == "created":
                customer_note = f"Created a new account for {cust_name or draft.pickup_name}."
                draft.dutchie_acct_id = str(acct_id)
                draft.customer_status = PhoneCartDraft.Customer.MATCHED
        else:
            customer_note = (f"No account found for {draft.contact_phone or 'this order'} "
                             "and it couldn't be created — please look the customer up.")

    audit = draft.audit or []
    audit.append({
        "at": timezone.now().isoformat(),
        "action": "pos_claim",
        "loaded_lines": loaded,
        "skipped": skipped[:20],
        "customer": draft.dutchie_acct_id or "unresolved",
    })
    draft.audit = audit[-100:]
    draft.save(update_fields=["status", "claimed_at", "audit", "dutchie_acct_id",
                              "customer_status", "updated_at"])
    ctx = _cart_ctx(cart)
    notes = [n for n in (customer_note,) if n]
    if skipped:
        notes.append("Some items need manual review: " + ", ".join(skipped[:3]))
    if notes:
        ctx["add_error"] = " ".join(notes)
    return _advance_to_shop(render(request, "pos/_cart.html", ctx))


@login_required
@require_http_methods(["POST"])
def cart_submit(request):
    _require_not_door(request)
    store = _active_store(request)
    cart = request.session.get("cart", [])
    acct_id = request.session.get("acct_id")
    shift_id = request.session.get("staff_session_id")
    ctx = {"store": store}
    if not (store and acct_id and cart):
        ctx["error"] = "need a store, a selected customer (AcctId), and at least one item"
        return render(request, "pos/_submit_result.html", ctx)
    try:
        result = _client(store).submit_cart(int(acct_id), cart)
        record_write(store.name, "submit", ok=True, acct_id=int(acct_id),
                     shipment_id=result["shipment_id"],
                     summary=f"{len(cart)} items -> Ready for pickup",
                     username=getattr(request.user, "username", ""), staff_session_id=shift_id)
        total = _cart_ctx(cart)["cart_total"]
        tracking.track(request, "checkout", detail=f"{len(cart)} items",
                       shipment_id=result["shipment_id"], total=total)
        tracking.end_visit(request, "checked_out", shipment_id=result["shipment_id"],
                           cart_total=total)
        # Checkout done â†’ clear the session and bounce to the start page for the
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
                     summary=str(exc)[:200], username=getattr(request.user, "username", ""),
                     staff_session_id=shift_id)
        logger.warning("cart submit failed: %s", exc)
        tracking.track(request, "checkout_failed", detail=str(exc)[:120])
        ctx["error"] = "Submit failed â€” please try again."
    return render(request, "pos/_submit_result.html", ctx)


# â”€â”€ session-activity dashboard (operator-facing, read-only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_DATE_WINDOWS = {"today": 1, "7d": 7, "30d": 30, "all": None}

_EVENT_META = {
    "login": ("ðŸ”‘", "Logged in"), "visit_start": ("ðŸŸ¢", "Visit started"),
    "id_scan": ("ðŸªª", "ID scanned"), "customer_search": ("ðŸ”", "Customer search"),
    "customer_selected": ("ðŸ‘¤", "Customer selected"), "profile_view": ("ðŸ“‡", "Viewed profile"),
    "profile_full_view": ("ðŸ“‹", "Viewed full profile"), "menu_browse": ("ðŸ§­", "Browsed menu"),
    "search": ("ðŸ”Ž", "Searched"), "category": ("ðŸ—‚ï¸", "Category"),
    "product_view": ("ðŸ‘ï¸", "Viewed product"), "suggestions_shown": ("âœ¨", "Suggestions shown"),
    "item_add": ("âž•", "Added to cart"), "item_remove": ("âž–", "Removed from cart"),
    "checkout": ("âœ…", "Checked out"), "abandon": ("ðŸšª", "Abandoned"),
    "scan_failed": ("âš ï¸", "Scan failed"), "lookup_failed": ("âš ï¸", "Lookup failed"),
    "checkout_failed": ("âŒ", "Checkout failed"),
    "admin_close": ("ðŸ”’", "Admin closed"), "admin_delete": ("ðŸ—‘ï¸", "Admin deleted"),
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
    # Annotate the last event kind in ONE bounded subquery â€” never `.events.last` in the
    # template (that clones the qs per row = N+1, and this panel polls every 5s).
    last_kind = ShopEvent.objects.filter(visit=OuterRef("pk")).order_by("-at").values("kind")[:1]
    return (ShopVisit.objects.filter(ended_at__isnull=True).exclude(status="queued")
            .annotate(last_kind=Subquery(last_kind)).order_by("-started_at"))


def _require_sessions_admin(request):
    if not request.user.is_staff:
        raise PermissionDenied


def _require_admin(request):
    if not request.user.is_superuser:
        raise PermissionDenied


@login_required
@rate_limit("sessions", limit=120, window=60)
@require_http_methods(["GET"])
def shifts(request):
    """Admin: staff shift log - who worked when (Pacific), on which register, and what
    each shift sold. TIME_ZONE renders Pacific; the |date filter is enough."""
    _require_admin(request)
    rows = StaffSession.objects.select_related("user").order_by("-login_at")[:200]
    return render(request, "pos/shifts_list.html", {"shifts": rows})


@login_required
@rate_limit("sessions", limit=120, window=60)
@require_http_methods(["GET"])
def shift_detail(request, shift_id):
    _require_admin(request)
    s = get_object_or_404(StaffSession, pk=shift_id)
    return render(request, "pos/shift_detail.html", {"s": s, "visits": s.visits.order_by("-started_at")})


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
    """Live partial â€” polled by the dashboard every few seconds."""
    _require_sessions_admin(request)
    return render(request, "pos/_active_panel.html", {"active": _active_visits()})


@login_required
@rate_limit("sessions", limit=120, window=60)
@require_http_methods(["GET"])
def session_detail(request, visit_id):
    _require_sessions_admin(request)
    v = get_object_or_404(ShopVisit, pk=visit_id)
    events = [{"e": e, "icon": _EVENT_META.get(e.kind, ("â€¢", e.kind))[0],
               "label": _EVENT_META.get(e.kind, ("â€¢", e.kind))[1]} for e in v.events.all()]
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
    # grows â€” retention is indefinite). Cap the scan at <=365d regardless of window.
    floor = timezone.now() - timezone.timedelta(days=_DATE_WINDOWS[f["win"]] or 365)
    ev = ShopEvent.objects.filter(visit__in=qs, at__gte=floor)
    top_lookup = list(ev.filter(kind="product_view").exclude(product_name="")
                      .values("product_id", "product_name").annotate(n=Count("id")).order_by("-n")[:20])
    top_search = list(ev.filter(kind="search").exclude(detail="")
                      .values("detail").annotate(n=Count("id")).order_by("-n")[:20])
    by_event = list(ev.values("kind").annotate(n=Count("id")).order_by("-n")[:12])
    # Top suggested: ids live in each suggestions_shown event's meta list â€” tally in Python
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
