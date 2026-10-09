"""Customers page: sortable / filterable / exportable table (dashboard.customers_views). Offline,
SQLite, budtender mocked. Rows are built through ``CustomerProfile.save`` so the denormalised
columns are exactly what production derives."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.http import QueryDict
from django.urls import reverse

from crm.models import CustomerProfile
from dashboard import customers_views as cv


@pytest.fixture
def staff_client(client, django_user_model):
    client.force_login(
        django_user_model.objects.create_user("st", password="x", is_staff=True, is_superuser=True)
    )
    return client


def mk(name, *, orders=1, spend=0.0, aov=0.0, items=None, cadence=None, first="", last="",
       brand="", fav=(), cat="", segment="", **kw):
    return CustomerProfile.objects.create(
        customer_key=name, name=name, orders=orders, total_spend=spend, aov=aov, items=items,
        cadence_days=cadence, first_order=first, last_order=last, top_brand=brand,
        favorite_brands=list(fav), top_categories=[{"category": cat}] if cat else [],
        segment=segment, **kw,
    )


@pytest.fixture
def six(db):
    """Six customers that differ on every axis; Cy has unknown items, Di unknown cadence/brand/cat."""
    mk("Ann", orders=10, spend=1000, aov=100, items=50, cadence=7, first="2025-01-01",
       last="2026-10-01", brand="Wyld", fav=[{"brand": "Kiva"}], cat="Flower", segment="Loyalist")
    mk("Bob", orders=2, spend=80, aov=40, items=4, cadence=15, first="2026-01-01",
       last="2026-08-01", brand="Stiiizy", cat="Edibles", segment="Casual")
    mk("Cy", orders=5, spend=400, aov=80, items=None, cadence=30, first="2025-06-15",
       last="2026-05-01", brand="Kiva", cat="Pre-roll", segment="Loyalist")
    mk("Di", orders=1, spend=20, aov=20, items=1, cadence=None, first="2026-09-30",
       last="2026-09-30", segment="New")
    mk("Ed", orders=3, spend=150, aov=50, items=9, cadence=60, first="2025-12-01",
       last="2025-12-31", brand="WYLD EDIBLES", cat="Flower", segment="Casual")
    mk("Flo", orders=4, spend=300, aov=75, items=20, cadence=200, first="2023-01-01",
       last="2024-01-01", brand="Cookies", cat="Concentrate", segment="Lapsed")


def names(resp):
    return [r["name"] for r in resp.context["rows"]]


def get(client, **params):
    return client.get(reverse("dash-customers"), params)


# ── filters ───────────────────────────────────────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize(
    "params,expected",
    [
        ({"q": "o"}, {"Bob", "Flo"}),
        ({"q": "ANN"}, {"Ann"}),
        ({"brand": "kiva"}, {"Ann", "Cy"}),  # top brand AND favorite_brands
        ({"brand": "KIVA"}, {"Ann", "Cy"}),  # case-insensitive
        ({"brand": "wyld"}, {"Ann"}),  # exactly one known brand -> that brand only
        ({"brand": "wyld e"}, {"Ed"}),  # not a known brand -> substring
        ({"brand": "yld"}, {"Ann", "Ed"}),
        ({"category": "Flower"}, {"Ann", "Ed"}),
        ({"segment": "Casual"}, {"Bob", "Ed"}),
        ({"freq": "weekly"}, {"Ann"}),
        ({"freq": "biweekly"}, {"Bob"}),
        ({"freq": "monthly"}, {"Cy"}),
        ({"freq": "occasional"}, {"Ed"}),
        ({"freq": "rare"}, {"Flo"}),
        ({"freq": "unknown"}, {"Di"}),
        ({"aov_min": "75"}, {"Ann", "Cy", "Flo"}),
        ({"aov_max": "40"}, {"Bob", "Di"}),
        ({"aov_min": "$40", "aov_max": "75"}, {"Bob", "Ed", "Flo"}),
        ({"items_min": "9"}, {"Ann", "Ed", "Flo"}),  # Cy's unknown items never match a bound
        ({"items_max": "4"}, {"Bob", "Di"}),
        ({"orders_min": "4"}, {"Ann", "Cy", "Flo"}),
        ({"orders_max": "2"}, {"Bob", "Di"}),
        ({"spend_min": "$1,000"}, {"Ann"}),
        ({"spend_max": "100"}, {"Bob", "Di"}),
        ({"last_from": "2026-09-30"}, {"Ann", "Di"}),
        ({"last_to": "2025-12-31"}, {"Ed", "Flo"}),
        ({"first_from": "2026-01-01"}, {"Bob", "Di"}),
        ({"first_to": "2025-01-01"}, {"Ann", "Flo"}),
    ],
)
def test_each_filter_alone(staff_client, six, params, expected):
    assert set(names(get(staff_client, **params))) == expected


@pytest.mark.django_db
def test_filters_combine(staff_client, six):
    r = get(staff_client, category="Flower", segment="Casual", items_min="5")
    assert names(r) == ["Ed"]
    r = get(staff_client, brand="kiva", orders_min="6", last_from="2026-10-01")
    assert names(r) == ["Ann"]
    assert get(staff_client, category="Flower", segment="Lapsed").context["rows"] == []


@pytest.mark.django_db
def test_summary_line_counts_filtered_of_all(staff_client, six):
    html = get(staff_client, category="Flower").content.decode()
    assert "2 of 6 customers" in html
    assert "Clear filters" in html
    assert "Clear filters" not in get(staff_client).content.decode()


@pytest.mark.django_db
def test_date_bounds_are_inclusive_and_bad_dates_are_noted(staff_client, six):
    assert names(get(staff_client, last_from="2026-09-30", last_to="2026-09-30")) == ["Di"]
    assert names(get(staff_client, first_from="2025-12-01", first_to="2025-12-01")) == ["Ed"]
    # Bad dates are ignored, say so on the page, and are not a 500.
    r = get(staff_client, last_from="not-a-date", last_to="2026-13-45")
    assert r.status_code == 200
    assert len(r.context["rows"]) == 6
    assert len(r.context["notes"]) == 2
    assert "not-a-date" in r.content.decode()
    # A reversed range is called out rather than silently empty.
    r = get(staff_client, last_from="2026-10-01", last_to="2026-09-01")
    assert r.context["rows"] == [] and "after" in r.context["notes"][0]
    # Bad numbers are ignored with a note too.
    r = get(staff_client, spend_min="lots", orders_max="nan")
    assert len(r.context["rows"]) == 6 and len(r.context["notes"]) == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    "cadence,band",
    [(1, "weekly"), (9, "weekly"), (10, "biweekly"), (19, "biweekly"), (20, "monthly"),
     (45, "monthly"), (46, "occasional"), (120, "occasional"), (121, "rare"), (None, "unknown")],
)
def test_frequency_band_boundaries(db, cadence, band):
    labels = {"weekly": "weekly", "biweekly": "2 weeks", "monthly": "monthly",
              "occasional": "occasional", "rare": "rare", "unknown": "unknown"}
    assert cv.band_for(cadence) == labels[band]
    mk("Edge", cadence=cadence)
    for key in labels:
        found = cv._filtered(QueryDict(f"freq={key}"))[0].exists()
        assert found == (key == band), f"cadence {cadence} / filter {key}"


@pytest.mark.django_db
def test_unknown_frequency_filter_is_noted_not_applied(staff_client, six):
    r = get(staff_client, freq="bogus")
    assert len(r.context["rows"]) == 6 and "bogus" in r.context["notes"][0]


# ── sorting ───────────────────────────────────────────────────────────────────
ASC = {
    "name": ["Ann", "Bob", "Cy", "Di", "Ed", "Flo"],
    "orders": ["Di", "Bob", "Ed", "Flo", "Cy", "Ann"],
    "spend": ["Di", "Bob", "Ed", "Flo", "Cy", "Ann"],
    "aov": ["Di", "Bob", "Ed", "Flo", "Cy", "Ann"],
    "items": ["Di", "Bob", "Ed", "Flo", "Ann", "Cy"],  # Cy unknown -> last
    "freq": ["Ann", "Bob", "Cy", "Ed", "Flo", "Di"],  # Di unknown -> last
    "last": ["Flo", "Ed", "Cy", "Bob", "Di", "Ann"],
    "since": ["Flo", "Ann", "Cy", "Ed", "Bob", "Di"],
    "brand": ["Flo", "Cy", "Bob", "Ann", "Ed", "Di"],  # case-insensitive; Di blank -> last
    "category": ["Flo", "Bob", "Ann", "Ed", "Cy", "Di"],  # Ann/Ed tie -> name; Di blank -> last
}
DESC = {
    "name": ["Flo", "Ed", "Di", "Cy", "Bob", "Ann"],
    "orders": ["Ann", "Cy", "Flo", "Ed", "Bob", "Di"],
    "spend": ["Ann", "Cy", "Flo", "Ed", "Bob", "Di"],
    "aov": ["Ann", "Cy", "Flo", "Ed", "Bob", "Di"],
    "items": ["Ann", "Flo", "Ed", "Bob", "Di", "Cy"],  # unknown still last
    "freq": ["Flo", "Ed", "Cy", "Bob", "Ann", "Di"],  # unknown still last
    "last": ["Ann", "Di", "Bob", "Cy", "Ed", "Flo"],
    "since": ["Di", "Bob", "Ed", "Cy", "Ann", "Flo"],
    "brand": ["Ed", "Ann", "Bob", "Cy", "Flo", "Di"],
    "category": ["Cy", "Ann", "Ed", "Bob", "Flo", "Di"],  # tie keeps name asc; blank still last
}


@pytest.mark.django_db
@pytest.mark.parametrize("key", list(ASC))
def test_every_sort_key_both_directions_nulls_last(staff_client, six, key):
    assert names(get(staff_client, sort=key)) == ASC[key]
    assert names(get(staff_client, sort="-" + key)) == DESC[key]


@pytest.mark.django_db
def test_default_sort_is_spend_desc_and_junk_sort_falls_back(staff_client, six):
    assert names(get(staff_client)) == DESC["spend"]
    assert names(get(staff_client, sort="total_spend; drop")) == DESC["spend"]


@pytest.mark.django_db
def test_header_click_toggles_direction_and_keeps_filters(staff_client, six):
    r = get(staff_client, sort="-orders", category="Flower")
    cols = {c["label"]: c for c in r.context["columns"]}
    assert "sort=orders" in cols["Orders"]["href"]  # active desc -> next click is asc
    assert cols["Orders"]["arrow"] == "▼" and cols["Orders"]["aria"] == "descending"
    assert "sort=-spend" in cols["Lifetime spend"]["href"]  # a fresh numeric column starts desc
    assert "sort=name" in cols["Customer"]["href"]  # a fresh text column starts asc
    assert all("category=Flower" in c["href"] for c in cols.values())
    r = get(staff_client, sort="orders", category="Flower")
    assert "sort=-orders" in {c["label"]: c for c in r.context["columns"]}["Orders"]["href"]


# ── paging ────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_paging_keeps_filters_sort_and_page_size(staff_client, db):
    for i in range(30):
        mk(f"Pat {i:02d}", orders=i + 1, spend=i * 10, cat="Flower")
    mk("Zed", orders=99)  # filtered out by q
    r = get(staff_client, q="pat", sort="-orders", per="25", category="Flower")
    assert len(r.context["rows"]) == 25 and r.context["num_pages"] == 2
    qs = parse_qs(urlsplit(r.context["next_href"]).query)
    assert qs == {"q": ["pat"], "sort": ["-orders"], "per": ["25"], "category": ["Flower"],
                  "page": ["2"]}
    assert r.context["prev_href"] == ""
    r2 = get(staff_client, q="pat", sort="-orders", per="25", category="Flower", page="2")
    assert len(r2.context["rows"]) == 5 and r2.context["next_href"] == ""
    assert "page=1" in r2.context["prev_href"] and "q=pat" in r2.context["prev_href"]
    # Out-of-range and junk pages clamp instead of erroring.
    assert get(staff_client, page="999").status_code == 200
    assert get(staff_client, page="abc").status_code == 200
    # Page size is limited to the offered sizes.
    assert get(staff_client, per="100").context["per"] == 100
    assert get(staff_client, per="7").context["per"] == 25
    # Sorting from a later page returns to page 1 but keeps the filters.
    sort_href = {c["label"]: c for c in r2.context["columns"]}["Orders"]["href"]
    assert "page=" not in sort_href and "q=pat" in sort_href


# ── htmx partial vs full page ─────────────────────────────────────────────────
@pytest.mark.django_db
def test_htmx_request_returns_only_the_table_partial(staff_client, six):
    url = reverse("dash-customers")
    full = staff_client.get(url, {"category": "Flower"})
    assert "dashboard/customers.html" in [t.name for t in full.templates]
    html = full.content.decode()
    assert "<html" in html and 'id="customers-results"' in html and "hx-push-url" in html
    assert "hx-get" in html

    part = staff_client.get(url, {"category": "Flower"}, headers={"HX-Request": "true"})
    names_used = [t.name for t in part.templates]
    assert "dashboard/_customers_table.html" in names_used
    assert "dashboard/customers.html" not in names_used and "dashboard/base.html" not in names_used
    body = part.content.decode()
    assert "<html" not in body and "<form" not in body
    assert "2 of 6 customers" in body and "Ann" in body and "Bob" not in body
    assert "HX-Request" in part["Vary"]

    # Back/forward asks for the page via htmx but needs the whole document.
    restore = staff_client.get(
        url, headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
    )
    assert "<html" in restore.content.decode()


# ── presentation ──────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_cells_formatting_and_unknowns(staff_client, six):
    rows = {r["name"]: r for r in get(staff_client).context["rows"]}
    ann, cy, di = rows["Ann"], rows["Cy"], rows["Di"]
    assert ann["spend_txt"] == "$1,000.00" and ann["aov_txt"] == "$100.00"
    assert ann["items_txt"] == "50" and ann["per_order_txt"] == "5/order"
    assert ann["freq_txt"] == "every ~7 days" and ann["band"] == "weekly"
    assert (ann["last_txt"], ann["since_txt"]) == ("2026-10-01", "2025-01-01")
    assert ann["brand_txt"] == "Wyld" and ann["category_txt"] == "Flower"
    # Unknown stays unknown: an em dash, never 0.
    assert cy["items_txt"] == "—" and cy["per_order_txt"] == ""
    assert di["freq_txt"] == "—" and di["brand_txt"] == "—" and di["category_txt"] == "—"
    html = get(staff_client).content.decode()
    assert "$1,000.00" in html and "every ~7 days" in html and "—" in html
    mk("Zero", items=0)
    zero = next(r for r in get(staff_client).context["rows"] if r["name"] == "Zero")
    assert zero["items_txt"] == "0"  # a known zero is shown as 0


@pytest.mark.django_db
def test_date_presets_use_today_and_all_time_clears(staff_client, six, monkeypatch):
    today = date(2026, 10, 8)
    monkeypatch.setattr(cv, "_today", lambda: today)
    r = get(staff_client, last_from="2026-01-01", last_to="2026-02-01", q="a")
    last = {p["label"]: p for p in next(d for d in r.context["date_filters"] if d["prefix"] == "last")["presets"]}

    def q(preset):
        return parse_qs(urlsplit(preset["href"]).query)

    assert q(last["30 days"])["last_from"] == [(today - timedelta(days=30)).isoformat()]
    assert q(last["90 days"])["last_from"] == [(today - timedelta(days=90)).isoformat()]
    assert q(last["This year"])["last_from"] == ["2026-01-01"]
    assert "last_from" not in q(last["All time"]) and "last_to" not in q(last["All time"])
    assert q(last["30 days"])["q"] == ["a"] and "last_to" not in q(last["30 days"])


@pytest.mark.django_db
def test_page_says_dates_are_first_last_only_and_has_no_phone(staff_client, six):
    CustomerProfile.objects.create(customer_key="phone:abc123hash", phone_hash="abc123hash", name="Zed")
    html = get(staff_client).content.decode()
    assert "spend within a date range isn't available" in html
    assert "abc123hash" not in html and "phone" not in html.lower()
    assert 'placeholder="Search by name' in html


# ── query count ───────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_list_query_count_is_flat_no_n_plus_one(staff_client, six, django_assert_max_num_queries):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    params = {"q": "a", "brand": "kiva", "per": "100", "sort": "-items"}
    with CaptureQueriesContext(connection) as small:
        get(staff_client, **params)
    for i in range(60):
        mk(f"Ana {i}", orders=i, items=i, brand="Kiva", cat="Flower", segment=f"S{i % 3}")
    with CaptureQueriesContext(connection) as big:
        r = get(staff_client, **params)
    assert len(r.context["rows"]) > 50
    assert len(big) == len(small), "query count must not grow with row count"
    with django_assert_max_num_queries(12):
        get(staff_client, **params)
    with django_assert_max_num_queries(7):
        staff_client.get(reverse("dash-customers"), params, headers={"HX-Request": "true"})


# ── live-roster fallback ──────────────────────────────────────────────────────
class _FakeBT:
    def __init__(self, ok=True):
        self.ok = ok

    def list_customers(self, *, q="", limit=25, offset=0):
        if not self.ok:
            return {"ok": False, "customers": [], "total": 0}
        return {"ok": True, "total": 1, "customers": [
            {"id": 7, "name": "Live Larry", "total_orders": 9, "last_purchase_at": "2026-06-01T00:00:00",
             "price_tier": "top", "top_categories": [{"category": "flower"}]},
        ]}


@pytest.mark.django_db
def test_live_roster_fallback_shows_only_live_columns(staff_client, monkeypatch):
    from voice import budtender_client

    monkeypatch.setattr(budtender_client, "budtender", lambda: _FakeBT())
    r = get(staff_client)
    html = r.content.decode()
    assert r.context["source"] == "live" and names(r) == ["Live Larry"]
    assert "Spend, avg cart and item counts need the analytics import" in html
    assert "Lifetime spend" not in html and "Favorite brand" not in html
    assert "Export CSV" not in html and 'name="brand"' not in html
    assert "2026-06-01" in html and "flower" in html and "top" in html
    assert reverse("dash-customer-detail", args=[7]) in html  # opaque id link, no phone in URLs
    assert "1 customer<" in html or "1 customer " in html


@pytest.mark.django_db
def test_no_snapshot_and_budtender_down_shows_import_hint(staff_client, monkeypatch):
    from voice import budtender_client

    monkeypatch.setattr(budtender_client, "budtender", lambda: _FakeBT(ok=False))
    r = get(staff_client)
    assert r.context["source"] == "empty" and "import_customer_profiles" in r.content.decode()


@pytest.mark.django_db
def test_snapshot_exists_but_filter_matches_nothing_does_not_fall_back_to_live(staff_client, six):
    r = get(staff_client, q="nobody-has-this-name")
    assert r.context["source"] == "analytics" and r.context["rows"] == []
    assert "No customers match these filters" in r.content.decode()


# ── CSV export ────────────────────────────────────────────────────────────────
def read_csv(resp):
    raw = b"".join(resp.streaming_content)
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM so Excel opens it correctly"
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))


@pytest.mark.django_db
def test_export_honours_filters_and_sort(staff_client, six):
    url = reverse("dash-customers-export")
    resp = staff_client.get(url, {"category": "Flower", "sort": "orders"})
    assert resp["Content-Type"].startswith("text/csv")
    assert resp["Content-Disposition"].startswith('attachment; filename="customers-')
    rows = read_csv(resp)
    assert rows[0] == list(cv.CSV_HEADER)
    assert [r[0] for r in rows[1:]] == ["Ed", "Ann"]  # Flower only, orders ascending
    ann = rows[2]
    assert ann[:9] == ["Ann", "Loyalist", "10", "1000.0", "100.0", "50", "5.0", "7", "weekly"]
    assert ann[9:] == ["2026-10-01", "2025-01-01", "Wyld", "Flower"]
    # Unknowns are blank cells in the CSV, not 0.
    cy = read_csv(staff_client.get(url, {"q": "cy"}))[1]
    assert cy[5] == "" and cy[6] == ""
    # Default (no params) = every row, spend desc, same as the page.
    assert [r[0] for r in read_csv(staff_client.get(url))[1:]] == DESC["spend"]


@pytest.mark.django_db
def test_export_link_on_page_carries_current_filters(staff_client, six):
    r = get(staff_client, category="Flower", sort="-aov")
    href = r.context["export_href"]
    assert urlsplit(href).path == reverse("dash-customers-export")
    assert parse_qs(urlsplit(href).query) == {"category": ["Flower"], "sort": ["-aov"]}
    assert href in r.content.decode().replace("&amp;", "&")


@pytest.mark.django_db
def test_export_neutralises_formulas_but_keeps_numbers_numeric(staff_client, db):
    for i, nasty in enumerate(["=SUM(A1)", "+1-555", "-cmd", "@x", "\tx", "\rx", "Plain"]):
        mk(nasty, orders=i, brand="=evil", cat="@cat", spend=-12.5 if nasty == "Plain" else 1)
    rows = read_csv(staff_client.get(reverse("dash-customers-export"), {"sort": "orders"}))[1:]
    out = [r[0] for r in rows]
    assert out == ["'=SUM(A1)", "'+1-555", "'-cmd", "'@x", "'\tx", "'\rx", "Plain"]
    assert all(r[11] == "'=evil" and r[12] == "'@cat" for r in rows)
    assert rows[-1][3] == "-12.5"  # a negative total is a number, not text with a quote


@pytest.mark.django_db
def test_export_is_capped_and_never_has_phone_data(staff_client, six, monkeypatch):
    monkeypatch.setattr(cv, "EXPORT_CAP", 3)
    CustomerProfile.objects.create(customer_key="phone:abc123hash", phone_hash="abc123hash", name="Zed")
    resp = staff_client.get(reverse("dash-customers-export"), {"sort": "name"})
    raw = b"".join(resp.streaming_content)
    assert len(list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))) == 1 + 3
    assert b"abc123hash" not in raw and b"phone" not in raw.lower()
    html = get(staff_client).content.decode()
    assert "(first 3)" in html  # the page says the export is capped


@pytest.mark.django_db
def test_export_requires_staff(client, django_user_model):
    url = reverse("dash-customers-export")
    assert client.get(url).status_code == 302  # anonymous
    client.force_login(django_user_model.objects.create_user("plain", password="x"))
    resp = client.get(url)
    assert resp.status_code == 302 and "/admin/login" in resp["Location"]
