"""Read-only analytics over the POS's own behaviour log (ShopVisit / ShopEvent).

Pure query helpers — no request objects, no side effects. A view scopes a ShopVisit
queryset (by store / budtender / window) and hands it here; the SAME functions serve the
manager BI dashboards and a budtender's own self-service page (which just scopes to
``budtender=<their username>``). Preference aggregation over the external read-only profile
DB lives in ``customers.intelligence``; the pure summarisers here take plain profile dicts
so they are unit-testable without that DB.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from customers.models import ShopEvent

# ── behavioural (POS DB: ShopVisit / ShopEvent) ───────────────────────────────


def events_for(visits):
    """ShopEvents belonging to a scoped ShopVisit queryset."""
    return ShopEvent.objects.filter(visit__in=visits)


def visit_metrics(visits) -> dict:
    """Headline KPIs for a scoped visit queryset."""
    m = visits.aggregate(
        visits=Count("id"),
        active=Count("id", filter=Q(ended_at__isnull=True)),
        checkouts=Count("id", filter=Q(outcome="checked_out")),
        abandoned=Count("id", filter=Q(outcome="abandoned")),
        viewed=Sum("items_viewed"),
        added=Sum("items_added"),
        revenue=Sum("cart_total", filter=Q(outcome="checked_out")),
        multi=Count("id", filter=Q(outcome="checked_out", items_added__gt=1)),
        co_items=Sum("items_added", filter=Q(outcome="checked_out")),
    )
    v = m["visits"] or 0
    co = m["checkouts"] or 0
    m["checkout_rate"] = round(100 * co / v) if v else 0
    m["avg_basket"] = round(float(m["revenue"] or 0) / co, 2) if co else 0
    m["avg_items"] = round((m["co_items"] or 0) / co, 1) if co else 0
    m["attach_rate"] = round(100 * (m["multi"] or 0) / co) if co else 0  # % of sales with 2+ items
    m["revenue"] = round(float(m["revenue"] or 0), 2)
    return m


def funnel(visits) -> list[dict]:
    """Visit funnel: total → browsed → added-to-cart → checked-out, with drop-off."""
    a = visits.aggregate(
        total=Count("id"),
        browsed=Count("id", filter=Q(items_viewed__gt=0)),
        carted=Count("id", filter=Q(items_added__gt=0)),
        bought=Count("id", filter=Q(outcome="checked_out")),
    )
    total = a["total"] or 0
    out, prev = [], None
    for label, key in [("Visits", "total"), ("Browsed", "browsed"),
                       ("Added to cart", "carted"), ("Checked out", "bought")]:
        n = a[key] or 0
        out.append({"label": label, "n": n,
                    "pct": round(100 * n / total) if total else 0,
                    "step_pct": round(100 * n / prev) if prev else 100})
        prev = n
    return out


def top_products(events, kind="item_add", n=20) -> list[dict]:
    """Most-added (or -viewed) products in the scope."""
    return list(events.filter(kind=kind).exclude(product_name="")
                .values("product_id", "product_name")
                .annotate(n=Count("id")).order_by("-n")[:n])


def by_dimension(events, field, n=15) -> list[dict]:
    """Views + adds grouped by ``brand`` or ``category`` with a view→add rate."""
    if field not in ("brand", "category"):
        raise ValueError("field must be 'brand' or 'category'")
    rows = list(events.filter(kind__in=["product_view", "item_add"]).exclude(**{field: ""})
                .values(field)
                .annotate(views=Count("id", filter=Q(kind="product_view")),
                          adds=Count("id", filter=Q(kind="item_add")))
                .order_by("-adds", "-views")[:n])
    mx = max((r["adds"] or 0) for r in rows) if rows else 0
    for r in rows:
        r["name"] = r.pop(field)
        r["add_rate"] = round(100 * r["adds"] / r["views"]) if r["views"] else 0
        r["pct"] = round(100 * (r["adds"] or 0) / mx) if mx else 0   # bar width vs the top row
    return rows


def budtender_performance(visits) -> list[dict]:
    """Per-budtender scorecard: volume, conversion, attach, basket, revenue."""
    rows = list(visits.exclude(budtender="").values("budtender").annotate(
        visits=Count("id"),
        checkouts=Count("id", filter=Q(outcome="checked_out")),
        revenue=Sum("cart_total", filter=Q(outcome="checked_out")),
        items=Sum("items_added", filter=Q(outcome="checked_out")),
        multi=Count("id", filter=Q(outcome="checked_out", items_added__gt=1)),
    ).order_by("-revenue"))
    for r in rows:
        co = r["checkouts"] or 0
        r["revenue"] = round(float(r["revenue"] or 0), 2)
        r["checkout_rate"] = round(100 * co / r["visits"]) if r["visits"] else 0
        r["avg_basket"] = round(r["revenue"] / co, 2) if co else 0
        r["attach_rate"] = round(100 * (r["multi"] or 0) / co) if co else 0
    return rows


def daily_trend(visits, days=30) -> list[dict]:
    """Continuous daily series (gap-filled) of visits / checkouts / revenue for sparklines."""
    days = max(1, min(days or 30, 180))
    start = (timezone.now() - timezone.timedelta(days=days - 1)).date()
    raw = {r["d"]: r for r in visits.filter(started_at__date__gte=start)
           .annotate(d=TruncDate("started_at")).values("d")
           .annotate(visits=Count("id"),
                     checkouts=Count("id", filter=Q(outcome="checked_out")),
                     revenue=Sum("cart_total", filter=Q(outcome="checked_out")))}
    out = []
    for i in range(days):
        d = start + timezone.timedelta(days=i)
        r = raw.get(d)
        out.append({"date": d.isoformat(),
                    "visits": (r["visits"] if r else 0) or 0,
                    "checkouts": (r["checkouts"] if r else 0) or 0,
                    "revenue": round(float((r["revenue"] if r else 0) or 0), 2)})
    return out


def suggestion_conversion(events) -> dict:
    """How many cart-adds came from a suggestion the customer was just shown."""
    adds = events.filter(kind="item_add")
    total = adds.count()
    from_sugg = adds.filter(meta__from_suggestion=True).count()
    return {"total_adds": total, "from_suggestion": from_sugg,
            "rate": round(100 * from_sugg / total) if total else 0}


# ── preference (external read-only profile DB) — pure summarisers over dicts ───


def top_affinities(profiles, dim, n=10) -> list[dict]:
    """Sum a ``{name: weight}`` affinity dim across many profile dicts → top-n rows.
    ``pct`` is the bar width relative to the top row."""
    agg: dict[str, float] = defaultdict(float)
    for p in profiles or []:
        for k, v in ((p or {}).get(dim) or {}).items():
            try:
                agg[str(k)] += float(v)
            except (TypeError, ValueError):
                continue
    ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:n]
    mx = ranked[0][1] if ranked else 0
    return [{"name": k, "weight": round(w, 2), "pct": round(100 * w / mx) if mx else 0}
            for k, w in ranked]


def segment_counts(profiles) -> list[tuple[str, int]]:
    """Persona-segment distribution across profiles (for the customer-mix donut)."""
    from . import persona
    c = Counter()
    for p in profiles or []:
        orders = int((p or {}).get("orders") or 0)
        c[persona.segment(orders, _recency(p))] += 1
    order = ["Champion", "Loyal", "Regular", "New", "Lapsed"]
    return [(s, c[s]) for s in order if c[s]] + [(s, n) for s, n in c.items() if s not in order]


def price_tier_counts(profiles) -> list[tuple[str, int]]:
    labels = {"value": "Budget", "mid": "Mid-tier", "top": "Premium"}
    c = Counter(labels.get((p or {}).get("price_tier"), "Unknown") for p in profiles or [])
    return [(lbl, c[lbl]) for lbl in ["Budget", "Mid-tier", "Premium", "Unknown"] if c[lbl]]


def thc_band_counts(profiles) -> list[dict]:
    """Distribution of preferred THC midpoint into bands (from thc_min/thc_max)."""
    bands = [("<15%", 0, 15), ("15–20%", 15, 20), ("20–25%", 20, 25),
             ("25–30%", 25, 30), ("30%+", 30, 999)]
    c = Counter()
    for p in profiles or []:
        lo, hi = (p or {}).get("thc_min"), (p or {}).get("thc_max")
        if lo is None or hi is None:
            continue
        try:
            mid = (float(lo) + float(hi)) / 2
        except (TypeError, ValueError):
            continue
        for label, a, b in bands:
            if a <= mid < b:
                c[label] += 1
                break
    mx = max(c.values()) if c else 0
    return [{"name": lbl, "n": c[lbl], "pct": round(100 * c[lbl] / mx) if mx else 0}
            for lbl, _, _ in bands if c[lbl]]


def _recency(profile):
    import datetime
    lp = (profile or {}).get("last_purchase")
    if not lp:
        return None
    try:
        d = datetime.date.fromisoformat(str(lp)[:10])
        return max(0, (datetime.date.today() - d).days)
    except (ValueError, TypeError):
        return None
