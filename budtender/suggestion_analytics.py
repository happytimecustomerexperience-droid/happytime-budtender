"""Staff analytics over suggestions and their outcomes (docs/contracts/suggestion-analytics-v1.md).

Backend token only (views in views.py). Everything is aggregated in SQL (annotate/Count with filters),
bounded (days <= 365, limit <= 100), with a fixed number of queries per response whatever the volume.
Rows carry the customer's id and (staff-visible) name, never a phone or a session token; snapshots go
out through ``suggestions.public_snapshot`` (allowlist), so no cost/margin can ride along.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Case, CharField, Count, F, IntegerField, Max, Q, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Concat, Lower, TruncDate
from django.utils import timezone

from . import suggestions
from .models import STORES, SuggestedProduct

TOP_N = 25
RECENT_N = 20

_STATUS_COUNTS = {
    "suggested": Count("id"),
    "pending": Count("id", filter=Q(outcome__status="pending")),
    "bought_exact": Count("id", filter=Q(outcome__status="bought_exact")),
    "bought_sibling": Count("id", filter=Q(outcome__status="bought_sibling")),
    "not_bought": Count("id", filter=Q(outcome__status="not_bought")),
    "unattributable": Count("id", filter=Q(outcome__status="unattributable")),
}
_SORTS = {
    "-shown_at": ("-shown_at", "-id"),
    "shown_at": ("shown_at", "id"),
    "-matched_at": (F("outcome__matched_at").desc(nulls_last=True), "-id"),
    "matched_at": (F("outcome__matched_at").asc(nulls_last=True), "id"),
    "status": ("outcome__status", "-shown_at", "-id"),
    "channel": ("channel", "-shown_at", "-id"),
    "store": ("location_slug", "-shown_at", "-id"),
}


def bounded_int(value, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return min(max(n, lo), hi)


def _text(value, cap: int = 128) -> str:
    return str(value).replace("\x00", "").strip()[:cap] if isinstance(value, (str, int)) else ""


def rate(num: int, den: int):
    return round(num / den, 4) if den else None


def _rates(c: dict) -> dict:
    bought = c["bought_exact"] + c["bought_sibling"]
    decided = bought + c["not_bought"]
    return {"bought_any": bought, "conversion_rate": rate(bought, decided),
            "exact_rate": rate(c["bought_exact"], decided), "sibling_rate": rate(c["bought_sibling"], decided)}


def filtered(data: dict, *, default_days: int = 30, max_days: int = 365):
    """(queryset, days) for the contract's filters: days, store, channel, kind, category, brand, plus the
    list-only status / match_kind / customer_id."""
    data = data if isinstance(data, dict) else {}
    days = bounded_int(data.get("days"), default=default_days, lo=1, hi=max_days)
    qs = SuggestedProduct.objects.filter(shown_at__gte=timezone.now() - timedelta(days=days))
    store = _text(data.get("store") or data.get("location"), 32).lower()
    if store in {s[0] for s in STORES}:
        qs = qs.filter(location_slug=store)
    channel = _text(data.get("channel"), 16).lower()
    if channel in suggestions.CHANNELS:
        qs = qs.filter(channel=channel)
    kind = _text(data.get("kind"), 12).lower()
    if kind in ("primary", "pairing"):
        qs = qs.filter(kind=kind)
    if category := _text(data.get("category"), 64):
        qs = qs.filter(snapshot__category=category)
    if brand := _text(data.get("brand"), 128):
        qs = qs.filter(snapshot__brand__iexact=brand)
    status = _text(data.get("status"), 16)
    if status in suggestions.STATUSES:
        qs = qs.filter(outcome__status=status)
    elif status == "bought_any":
        qs = qs.filter(outcome__status__in=suggestions.BOUGHT)
    match = _text(data.get("match_kind"), 16)
    if match in suggestions.MATCH_KINDS:
        qs = qs.filter(outcome__match_kind=match)
    if data.get("customer_id") not in (None, ""):
        qs = qs.filter(customer_id=bounded_int(data.get("customer_id"), default=0, lo=0, hi=2**31))
    return qs, days


def _group(qs, key_expr, *, label=None, order=("-suggested", "key")) -> list[dict]:
    out = []
    for g in qs.annotate(key=key_expr).values("key").annotate(**_STATUS_COUNTS).order_by(*order):
        key = g["key"]
        key = key.isoformat() if hasattr(key, "isoformat") else ("" if key is None else key)
        counts = {k: g[k] for k in _STATUS_COUNTS}
        out.append({"key": key, "label": label(key) if label else (str(key) or "(none)"), **counts,
                    "conversion_rate": _rates(counts)["conversion_rate"]})
    return out


def _product_key():
    """Stable product identity: the POS product id, else the sibling key + the lower-cased name, else the
    store + SKU (a partial backfill row)."""
    return Case(
        When(_pid__gt="", then=Concat(Value("pid:"), F("_pid"), output_field=CharField())),
        When(_pname__gt="", then=Concat(Value("key:"), F("sibling_key"), Value("|"), Lower(F("_pname")),
                                        output_field=CharField())),
        default=Concat(Value("sku:"), F("location_slug"), Value(":"), F("sku"), output_field=CharField()),
        output_field=CharField(),
    )


def _products(qs, *, having: Q | None = None, order=("-suggested", "pkey"), limit: int = TOP_N) -> list[dict]:
    groups = (qs.annotate(_pid=Cast(KT("snapshot__product_id"), CharField()), _pname=Cast(KT("snapshot__name"), CharField()))
              .annotate(pkey=_product_key()).values("pkey")
              .annotate(**_STATUS_COUNTS, customers=Count("customer", distinct=True),
                        last_suggested_at=Max("shown_at"), latest_id=Max("id")))
    if having is not None:
        groups = groups.filter(having)
    groups = list(groups.order_by(*order)[:limit])
    latest = {r["id"]: r for r in SuggestedProduct.objects.filter(id__in=[g["latest_id"] for g in groups])
              .values("id", "sku", "location_slug", "snapshot")}
    out = []
    for g in groups:
        row = latest.get(g["latest_id"]) or {}
        snap = suggestions.public_snapshot(row.get("snapshot"))
        counts = {k: g[k] for k in _STATUS_COUNTS}
        out.append({
            "key": g["pkey"], "sku": row.get("sku", ""), "store": row.get("location_slug", ""),
            "product_id": snap.get("product_id", ""), "name": snap.get("name") or row.get("sku", ""),
            "brand": snap.get("brand", ""), "category": snap.get("category", ""), "strain": snap.get("strain", ""),
            "strain_type": snap.get("strain_type", ""), "size_label": snap.get("size_label", ""),
            "price": snap.get("price"), "thc_percent": snap.get("thc_percent"),
            "snapshot_partial": bool(snap.get("snapshot_partial")),
            "times_suggested": counts["suggested"], "customers": g["customers"],
            **{k: v for k, v in counts.items() if k != "suggested"}, **_rates(counts),
            "last_suggested_at": g["last_suggested_at"].isoformat() if g["last_suggested_at"] else None,
        })
    return out


def row(sp) -> dict:
    """One suggestion as staff see it (list, customer profile, recent buyers)."""
    try:
        o = sp.outcome
    except Exception:  # noqa: BLE001 - RelatedObjectDoesNotExist: a legacy row not backfilled yet
        o = None
    cust = sp.customer
    matched_at = o.matched_at if o is not None else None
    return {
        "id": sp.pk,
        "suggested_at": sp.shown_at.isoformat() if sp.shown_at else None,
        "channel": sp.channel, "store": sp.location_slug, "kind": sp.kind, "sku": sp.sku,
        "customer": {"id": cust.pk, "name": cust.name or ""} if cust is not None else None,
        "identity_via": sp.identity_via,
        "snapshot": suggestions.public_snapshot(sp.snapshot),
        "status": o.status if o is not None else "untracked",
        "match_kind": o.match_kind if o is not None else "",
        "matched_name": o.matched_name if o is not None else "",
        "matched_sku": o.matched_sku if o is not None else "",
        "matched_amount": float(o.matched_amount) if o is not None and o.matched_amount is not None else None,
        "matched_at": matched_at.isoformat() if matched_at else None,
        "window_ends_at": o.window_ends_at.isoformat() if o is not None else None,
        "days_to_purchase": (round((matched_at - sp.shown_at).total_seconds() / 86400, 1)
                             if matched_at and sp.shown_at else None),
        "session": {"id": sp.session_id} if sp.session_id else None,
    }


def _rows(qs) -> list[dict]:
    return [row(sp) for sp in qs.select_related("customer", "outcome")]


def summary(data: dict) -> dict:
    qs, days = filtered({k: v for k, v in (data or {}).items() if k not in ("status", "match_kind", "customer_id")})
    t = qs.aggregate(**_STATUS_COUNTS, customers_known=Count("customer", distinct=True))
    counts = {k: t[k] for k in _STATUS_COUNTS}
    products = (qs.annotate(_pid=Cast(KT("snapshot__product_id"), CharField()), _pname=Cast(KT("snapshot__name"), CharField()))
                .annotate(pkey=_product_key()).values("pkey").distinct().count())
    bought = Q(bought_exact__gt=0) | Q(bought_sibling__gt=0)
    return {
        "window_days": days,
        "totals": {**counts, "products": products, "customers_known": t["customers_known"], **_rates(counts)},
        "by_channel": _group(qs, F("channel")),
        "by_store": _group(qs, F("location_slug")),
        "by_category": _group(qs, KT("snapshot__category")),
        "by_kind": _group(qs, F("kind")),
        "by_rank": _group(qs, Cast(KT("snapshot__rank"), IntegerField()),
                          label=lambda k: f"#{k}" if k != "" else "(none)", order=("key",)),
        "by_day": _group(qs, TruncDate("shown_at"), order=("key",)),
        "top_products": _products(qs),
        "never_bought": _products(qs, having=Q(not_bought__gt=0) & ~bought, order=("-not_bought", "pkey")),
        "recent_buyers": _rows(qs.filter(outcome__status__in=suggestions.BOUGHT)
                               .order_by(F("outcome__matched_at").desc(nulls_last=True), "-id")[:RECENT_N]),
    }


def listing(data: dict, *, default_days: int = 30, max_days: int = 365) -> dict:
    data = data if isinstance(data, dict) else {}
    qs, days = filtered(data, default_days=default_days, max_days=max_days)
    offset = bounded_int(data.get("offset"), default=0, lo=0, hi=1_000_000)
    limit = bounded_int(data.get("limit"), default=25, lo=1, hi=100)
    sort = data.get("sort") if data.get("sort") in _SORTS else "-shown_at"
    total = qs.count()
    page = _rows(qs.order_by(*_SORTS[sort])[offset:offset + limit])
    return {"ok": True, "window_days": days, "total": total, "offset": offset, "limit": limit, "sort": sort,
            "count": len(page), "rows": page}


def for_customer(profile, data: dict) -> dict:
    """One customer's suggestions (all their own rows; ``days`` up to ~10 years), with their totals."""
    data = {**(data if isinstance(data, dict) else {}), "customer_id": profile.pk}
    out = listing(data, default_days=3650, max_days=3650)
    qs, _ = filtered({k: v for k, v in data.items() if k not in ("status", "match_kind")},
                     default_days=3650, max_days=3650)
    t = qs.aggregate(**_STATUS_COUNTS)
    counts = {k: t[k] for k in _STATUS_COUNTS}
    return {**out, "customer": {"id": profile.pk, "name": profile.name or ""}, "totals": {**counts, **_rates(counts)}}
