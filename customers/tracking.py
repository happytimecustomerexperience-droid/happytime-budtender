"""Activity tracking — visit lifecycle + per-event logging.

DEGRADE-SAFE by contract: every public function swallows its own errors so tracking can
NEVER break a page or slow the sale path. A visit is the unit (one customer, one budtender,
one store); events hang off the open visit resolved from the session. No new PII is stored.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from .models import ShopEvent, ShopVisit

logger = logging.getLogger(__name__)

_VISIT = "visit_id"                       # session key holding the open visit id
_TMP = ("_seen", "_lastbrowse", "_lastsearch", "_lastsugg", "taste")  # per-visit scratch, reset on start/end
_TASTE_CAP = 16                           # keep the session taste dict tiny


def accrue_taste(request, product, weight=1):
    """Bump this visit's live taste from a viewed/added product (category/brand/strain_type
    — the exact keys ranking.blend_session_taste reads). Best-effort; never raises."""
    try:
        if not product:
            return
        t = request.session.get("taste") or {}

        def _bump(field, val):
            val = (val or "").strip() if isinstance(val, str) else val
            if not val:
                return
            d = t.setdefault(field, {})
            d[val] = (d.get(val) or 0) + weight
            if len(d) > _TASTE_CAP:
                t[field] = dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:_TASTE_CAP])

        for field in ("category", "brand", "strain_type"):
            _bump(field, product.get(field))
        for fl in (product.get("flavors") or []):
            _bump("flavor", fl)
        request.session["taste"] = t
        request.session.modified = True
    except Exception as exc:
        logger.warning("accrue_taste failed: %s", exc)


def _username(request):
    return getattr(getattr(request, "user", None), "username", "") or ""


def _open_visit(request):
    vid = request.session.get(_VISIT)
    if not vid:
        return None
    try:
        return ShopVisit.objects.filter(id=vid, ended_at__isnull=True).first()
    except Exception:
        return None


def start_visit(request, *, acct_id, name="", phone="", how="lookup", store="", **meta):
    """Begin a visit for the selected customer. Idempotent: reuse the open visit when the
    same acct is active; otherwise close the prior open one (abandoned) and start fresh."""
    try:
        acct = int(acct_id) if acct_id is not None and str(acct_id).isdigit() else None
        cur = _open_visit(request)
        if cur is not None:
            if cur.acct_id == acct:
                return cur                                # same customer -> reuse
            _close(cur, "abandoned")                      # switching customer -> close old
        # Clear per-visit scratch (incl. `taste`) BEFORE create(): if anything below raises,
        # the prior shopper's taste must not survive into the next customer's session.
        for k in _TMP:
            request.session.pop(k, None)
        request.session["_seen"] = []
        store = store or request.session.get("store") or ""
        v = ShopVisit.objects.create(
            store=str(store), budtender=_username(request), acct_id=acct,
            acct_name=name or "", phone=phone or "", how_started=how or "")
        request.session[_VISIT] = v.id
        _log(v, request, "visit_start", detail=how, meta=meta)
        # Fire the secondary "how did this visit start" event from ONE place so every
        # entry point (begin gate, on-screen scan, lookup select) records it uniformly.
        if how == "scan":
            _log(v, request, "id_scan", meta={"over_21": meta.get("scan_over21")})
        elif how in ("lookup", "phone", "name"):
            _log(v, request, "customer_selected", detail=(name or "")[:200])
        return v
    except Exception as exc:
        logger.warning("start_visit failed: %s", exc)
        return None


def end_visit(request, outcome, **summary):
    """Close the open visit (outcome = checked_out | abandoned) and clear scratch state."""
    try:
        v = _open_visit(request)
        if v is not None:
            _close(v, outcome, **summary)
    except Exception as exc:
        logger.warning("end_visit failed: %s", exc)
    finally:
        request.session.pop(_VISIT, None)
        for k in _TMP:
            request.session.pop(k, None)


def track(request, kind, *, product=None, detail="", dedupe_key=None, brand="", category="", **meta):
    """Append an event to the current open visit (no-op if none, except 'login' which is
    a standalone budtender event). `dedupe_key` collapses repeats within one visit.
    `brand`/`category` are first-class dims for the product analytics — auto-filled from
    `product` when present, or passed explicitly (item_add sends a trimmed cart line that
    lacks them, so cart_add passes the full product's brand/category)."""
    try:
        v = _open_visit(request)
        if v is None and kind != "login":
            return
        if dedupe_key is not None:
            seen = request.session.get("_seen") or []
            tag = f"{kind}:{dedupe_key}"
            if tag in seen:
                return
            request.session["_seen"] = (seen + [tag])[-500:]
        pid = pname = ""
        if product:
            pid = str(product.get("product_id") or product.get("ProductId") or "")
            pname = str(product.get("name") or product.get("ProductDesc") or "")[:255]
            brand = brand or str(product.get("brand") or "")
            category = category or str(product.get("cat_key") or product.get("category") or "")
        _log(v, request, kind, detail=detail, product_id=pid, product_name=pname,
             brand=brand, category=category, meta=meta)
        if v is not None:
            _bump(v, kind)
    except Exception as exc:
        logger.warning("track(%s) failed: %s", kind, exc)


# ── internals ────────────────────────────────────────────────────────────────
def _close(v, outcome, *, shipment_id=None, cart_total=None):
    v.ended_at = timezone.now()
    v.outcome = outcome
    if shipment_id is not None:
        v.order_shipment_id = shipment_id
    if cart_total is not None:
        v.cart_total = cart_total
    v.save(update_fields=["ended_at", "outcome", "order_shipment_id", "cart_total"])


def _log(visit, request, kind, *, detail="", product_id="", product_name="",
         brand="", category="", meta=None):
    ShopEvent.objects.create(
        visit=visit, kind=kind, budtender=_username(request),
        acct_id=getattr(visit, "acct_id", None) if visit is not None else None,
        product_id=product_id or "", product_name=product_name or "",
        brand=(brand or "")[:120], category=(category or "")[:120],
        detail=(detail or "")[:200], meta=meta or {})


def _bump(visit, kind):
    fields = ["event_count"]
    visit.event_count = (visit.event_count or 0) + 1
    if kind == "product_view":
        visit.items_viewed = (visit.items_viewed or 0) + 1
        fields.append("items_viewed")
    elif kind == "item_add":
        visit.items_added = (visit.items_added or 0) + 1
        fields.append("items_added")
    visit.save(update_fields=fields)
