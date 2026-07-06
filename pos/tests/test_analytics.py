"""Pure analytics engine + inline-SVG chart helpers."""

import pytest
from django.utils import timezone

from pos import analytics, charts
from customers.models import ShopEvent, ShopVisit

pytestmark = pytest.mark.django_db


def _visit(budtender="bud", outcome="checked_out", added=2, viewed=3, total=40, store="yakima"):
    v = ShopVisit.objects.create(store=store, budtender=budtender, acct_id=1, outcome=outcome,
                                 items_added=added, items_viewed=viewed, cart_total=total)
    if outcome != "open":
        ShopVisit.objects.filter(id=v.id).update(ended_at=timezone.now())
    return v


def _ev(visit, kind, **kw):
    return ShopEvent.objects.create(visit=visit, kind=kind, **kw)


# ── behavioural ───────────────────────────────────────────────────────────────
def test_visit_metrics_core():
    _visit(outcome="checked_out", added=2, total=50)
    _visit(outcome="checked_out", added=1, total=30)
    _visit(outcome="abandoned", added=0, total=0)
    m = analytics.visit_metrics(ShopVisit.objects.all())
    assert m["visits"] == 3 and m["checkouts"] == 2
    assert m["checkout_rate"] == 67          # 2/3
    assert m["revenue"] == 80.0 and m["avg_basket"] == 40.0
    assert m["attach_rate"] == 50            # 1 of 2 sales had 2+ items


def test_funnel_stages():
    _visit(outcome="checked_out", added=2, viewed=3)
    _visit(outcome="abandoned", added=0, viewed=1)
    steps = analytics.funnel(ShopVisit.objects.all())
    assert [s["label"] for s in steps] == ["Visits", "Browsed", "Added to cart", "Checked out"]
    assert steps[0]["n"] == 2 and steps[-1]["n"] == 1


def test_by_dimension_brand_views_adds():
    v = _visit()
    _ev(v, "product_view", brand="House", product_name="A")
    _ev(v, "item_add", brand="House", product_name="A")
    _ev(v, "product_view", brand="Other", product_name="B")
    rows = analytics.by_dimension(analytics.events_for(ShopVisit.objects.all()), "brand")
    house = next(r for r in rows if r["name"] == "House")
    assert house["views"] == 1 and house["adds"] == 1 and house["add_rate"] == 100
    assert house["pct"] == 100               # top row → full bar


def test_budtender_performance():
    _visit(budtender="ann", outcome="checked_out", added=2, total=60)
    _visit(budtender="ann", outcome="abandoned", added=0, total=0)
    ann = next(r for r in analytics.budtender_performance(ShopVisit.objects.all())
               if r["budtender"] == "ann")
    assert ann["visits"] == 2 and ann["checkouts"] == 1 and ann["checkout_rate"] == 50
    assert ann["avg_basket"] == 60.0 and ann["attach_rate"] == 100


def test_suggestion_conversion():
    v = _visit()
    _ev(v, "item_add", meta={"from_suggestion": True})
    _ev(v, "item_add", meta={"from_suggestion": False})
    s = analytics.suggestion_conversion(analytics.events_for(ShopVisit.objects.all()))
    assert s["total_adds"] == 2 and s["from_suggestion"] == 1 and s["rate"] == 50


def test_daily_trend_gapfilled():
    _visit()
    t = analytics.daily_trend(ShopVisit.objects.all(), days=7)
    assert len(t) == 7 and all({"date", "visits", "revenue"} <= set(d) for d in t)


# ── pure preference summarisers (no DB) ───────────────────────────────────────
def test_top_affinities():
    profiles = [{"brand_affinity": {"A": 0.6, "B": 0.4}}, {"brand_affinity": {"A": 0.2}}]
    rows = analytics.top_affinities(profiles, "brand_affinity", 5)
    assert rows[0]["name"] == "A" and rows[0]["weight"] == 0.8 and rows[0]["pct"] == 100


def test_segment_counts():
    profiles = [{"orders": 0}, {"orders": 7}, {"orders": 20}]
    seg = dict(analytics.segment_counts(profiles))
    assert seg["New"] == 1 and seg["Loyal"] == 1 and seg["Champion"] == 1


def test_thc_band_counts():
    profiles = [{"thc_min": 18, "thc_max": 22}, {"thc_min": 10, "thc_max": 14}, {}]
    bands = {b["name"]: b["n"] for b in analytics.thc_band_counts(profiles)}
    assert sum(bands.values()) == 2 and len(bands) == 2      # the {} profile is skipped


# ── charts ────────────────────────────────────────────────────────────────────
def test_sparkline():
    assert charts.sparkline([1, 2, 3]).startswith("<svg")
    assert charts.sparkline([1]) == ""                       # <2 points → nothing


def test_donut_and_legend():
    assert "<svg" in charts.donut([("A", 3), ("B", 1)])
    assert charts.donut([]) == ""
    leg = charts.legend([("A", 3), ("B", 1)])
    assert leg[0]["pct"] == 75 and leg[0]["color"].startswith("#")
