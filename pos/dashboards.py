"""Manager BI dashboards (and, in P3, budtender self-service) over the behaviour log
+ the read-only profile book.

Thin views: scope a ShopVisit queryset, call the pure ``analytics`` engine, attach
inline-SVG ``charts``, render. Manager views are ``is_staff``-gated; the self-service
view (P3, ``my_stats``) reuses the same engine scoped to the acting budtender.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from pos_core.ratelimit import rate_limit
from customers import intelligence
from customers.models import ShopVisit

from . import analytics, charts

_WINDOWS = {"today": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}
_TEAL = "#5EE0D0"


def _staff(request):
    if not request.user.is_staff:
        raise PermissionDenied


def _scope(request, budtender=None):
    """Scope ShopVisit by store + window (and budtender). A non-None ``budtender`` forces
    self-scoping (self-service) and ignores any ``?budtender=`` override."""
    g = request.GET
    qs = ShopVisit.objects.all()
    store = g.get("store") or ""
    win = g.get("win") if g.get("win") in _WINDOWS else "30d"
    bt = budtender if budtender is not None else (g.get("budtender") or "")
    if store:
        qs = qs.filter(store=store)
    if bt:
        qs = qs.filter(budtender=bt)
    days = _WINDOWS[win]
    if days:
        qs = qs.filter(started_at__gte=timezone.now() - timezone.timedelta(days=days))
    return qs, {"store": store, "budtender": bt, "win": win}


def _filterbar(request, f):
    stores = sorted(s for s in ShopVisit.objects.values_list("store", flat=True).distinct() if s)
    budtenders = sorted(b for b in ShopVisit.objects.values_list("budtender", flat=True).distinct() if b)
    return {"f": f, "stores": stores, "budtenders": budtenders, "windows": list(_WINDOWS)}


@login_required
@rate_limit("insights", limit=120, window=60)
@require_http_methods(["GET"])
def overview(request):
    _staff(request)
    visits, f = _scope(request)
    events = analytics.events_for(visits)
    trend = analytics.daily_trend(visits, days=_WINDOWS.get(f["win"]) or 30)
    ctx = {
        "tab": "overview",
        "metrics": analytics.visit_metrics(visits),
        "funnel": analytics.funnel(visits),
        "sugg": analytics.suggestion_conversion(events),
        "spark_visits": charts.sparkline([d["visits"] for d in trend]),
        "spark_rev": charts.sparkline([d["revenue"] for d in trend], stroke=_TEAL),
        "top_brands": analytics.by_dimension(events, "brand", 8),
        "top_cats": analytics.by_dimension(events, "category", 8),
    }
    ctx.update(_filterbar(request, f))
    return render(request, "pos/insights/overview.html", ctx)


@login_required
@rate_limit("insights", limit=120, window=60)
@require_http_methods(["GET"])
def products(request):
    _staff(request)
    visits, f = _scope(request)
    events = analytics.events_for(visits)
    ctx = {
        "tab": "products",
        "funnel": analytics.funnel(visits),
        "cats": analytics.by_dimension(events, "category", 20),
        "brands": analytics.by_dimension(events, "brand", 20),
        "top_added": analytics.top_products(events, "item_add", 20),
        "top_viewed": analytics.top_products(events, "product_view", 20),
    }
    ctx.update(_filterbar(request, f))
    return render(request, "pos/insights/products.html", ctx)


@login_required
@rate_limit("insights", limit=120, window=60)
@require_http_methods(["GET"])
def budtenders(request):
    _staff(request)
    visits, f = _scope(request)
    ctx = {"tab": "budtenders", "team": analytics.visit_metrics(visits),
           "rows": analytics.budtender_performance(visits)}
    ctx.update(_filterbar(request, f))
    return render(request, "pos/insights/budtenders.html", ctx)


@login_required
@rate_limit("insights", limit=60, window=60)
@require_http_methods(["GET"])
def customers(request):
    """What the whole customer book likes (aggregate affinities). Reads the external
    read-only profile DB — degrades to an empty state when it isn't configured."""
    _staff(request)
    _, f = _scope(request)
    profiles = intelligence.load_all_profiles()
    seg = analytics.segment_counts(profiles)
    tier = analytics.price_tier_counts(profiles)
    ctx = {
        "tab": "customers", "n_profiles": len(profiles),
        "brands": analytics.top_affinities(profiles, "brand_affinity", 12),
        "cats": analytics.top_affinities(profiles, "category_affinity", 10),
        "strain_types": analytics.top_affinities(profiles, "strain_type_affinity", 6),
        "flavors": analytics.top_affinities(profiles, "flavor_affinity", 12),
        "terpenes": analytics.top_affinities(profiles, "terpene_affinity", 10),
        "thc_bands": analytics.thc_band_counts(profiles),
        "seg_donut": charts.donut(seg), "seg_legend": charts.legend(seg),
        "tier_donut": charts.donut(tier), "tier_legend": charts.legend(tier),
    }
    ctx.update(_filterbar(request, f))
    return render(request, "pos/insights/customers.html", ctx)


# ── P3: budtender self-service (own numbers, not staff-gated) ──────────────────
@login_required
@rate_limit("insights", limit=120, window=60)
@require_http_methods(["GET"])
def my_stats(request):
    """A budtender's own performance — scoped to their username, compared to the store
    average (anonymised: they see the team mean, not other people's individual numbers)."""
    me = request.user.username
    visits, f = _scope(request, budtender=me)
    events = analytics.events_for(visits)
    trend = analytics.daily_trend(visits, days=_WINDOWS.get(f["win"]) or 30)
    all_visits, _ = _scope(request, budtender="")   # same store/window, whole team
    mine = analytics.visit_metrics(visits)
    team = analytics.visit_metrics(all_visits)
    team_bt = [r for r in analytics.budtender_performance(all_visits) if r["budtender"]]
    # team averages for the compare row (mean across budtenders active in scope)
    n = len(team_bt) or 1
    avg = {
        "checkout_rate": round(sum(r["checkout_rate"] for r in team_bt) / n),
        "attach_rate": round(sum(r["attach_rate"] for r in team_bt) / n),
        "avg_basket": round(sum(r["avg_basket"] for r in team_bt) / n, 2),
    }
    ctx = {
        "tab": "me", "me": me, "metrics": mine, "team": team, "avg": avg,
        "funnel": analytics.funnel(visits),
        "sugg": analytics.suggestion_conversion(events),
        "spark_visits": charts.sparkline([d["visits"] for d in trend]),
        "spark_rev": charts.sparkline([d["revenue"] for d in trend], stroke=_TEAL),
        "top_cats": analytics.by_dimension(events, "category", 8),
        "top_brands": analytics.by_dimension(events, "brand", 8),
        "windows": list(_WINDOWS), "win": f["win"],
    }
    return render(request, "pos/insights/my_stats.html", ctx)
