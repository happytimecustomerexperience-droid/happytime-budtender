"""The staff analytics page: KPI maths on a hand-computed fixture, filters reaching the budtender, call
metrics (store/channel, store-time heatmap, durations, transfers), empty vs unreachable, leak/PII guards,
gating and query counts. Offline: ``dashboard.views._budtender_post`` is replaced, no network."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from dashboard import analytics_calls, analytics_views
from voice.models import VoiceCall

URL = "dash-analytics"


@pytest.fixture
def staff(db):
    return User.objects.create_user("analyst", password="x", is_staff=True)


@pytest.fixture
def client_staff(client, staff):
    client.force_login(staff)
    return client


def _row(key, suggested, exact, sibling, not_bought, pending, unattr, label=None):
    decided = exact + sibling + not_bought
    return {"key": key, "label": label or key, "suggested": suggested, "bought_exact": exact,
            "bought_sibling": sibling, "not_bought": not_bought, "pending": pending, "unattributable": unattr,
            "conversion_rate": round((exact + sibling) / decided, 4) if decided else None}


def _product(name, **kw):
    base = {"key": f"pid:{name}", "sku": name, "store": "yakima", "product_id": name, "name": name, "brand": "Wyld",
            "category": "Edible", "strain": "Sour Apple", "strain_type": "hybrid", "size_label": "100mg",
            "price": 22.0, "thc_percent": 18.5, "snapshot_partial": False, "times_suggested": 5, "customers": 3,
            "bought_exact": 1, "bought_sibling": 0, "not_bought": 2, "pending": 1, "unattributable": 1,
            "conversion_rate": 0.3333, "last_suggested_at": "2026-10-06T18:00:00+00:00"}
    return {**base, **kw}


_TODAY = timezone.localdate()

# Hand-computed: 100 suggested = 30 exact + 10 sibling + 30 not bought + 20 pending + 10 unattributable.
# decided = 40 + 30 = 70; conversion = 40 / 70 = 0.5714; exact share of purchases = 30 / 40 = 75%.
# Ranks 1-3 (what a caller hears): suggested 90, exact 29, sibling 10, not bought 26 -> 39 / 65 = 60.0%.
SUGGESTIONS = {
    "ok": True, "window_days": 30,
    "totals": {"suggested": 100, "products": 12, "customers_known": 31, "pending": 20, "bought_exact": 30,
               "bought_sibling": 10, "bought_any": 40, "not_bought": 30, "unattributable": 10,
               "conversion_rate": 0.5714, "exact_rate": 0.4286, "sibling_rate": 0.1429},
    "by_channel": [_row("phone", 60, 20, 5, 15, 12, 8, "phone"), _row("chat", 40, 10, 5, 15, 8, 2, "chat")],
    "by_store": [_row("yakima", 70, 20, 5, 20, 15, 10, "yakima")],
    "by_category": [_row("Edible", 50, 15, 5, 15, 10, 5, "Edible")],
    "by_kind": [_row("primary", 90, 28, 9, 28, 17, 8, "primary"), _row("pairing", 10, 2, 1, 2, 3, 2, "pairing")],
    "by_rank": [_row(1, 40, 15, 5, 10, 5, 5, "#1"), _row(2, 30, 10, 3, 10, 5, 2, "#2"),
                _row(3, 20, 4, 2, 6, 6, 2, "#3"), _row(4, 10, 1, 0, 4, 4, 1, "#4")],
    "by_day": [{**_row("d", 6, 2, 1, 2, 1, 0), "key": (_TODAY - timedelta(days=1)).isoformat()},
               {**_row("d", 4, 1, 0, 1, 1, 1), "key": _TODAY.isoformat()}],
    "top_products": [_product("Zeta Gummies", times_suggested=9, price=30.0),
                     _product("Alpha Chews", times_suggested=7, price=12.5),
                     _product("Mid Mints", times_suggested=8, price=20.0)],
    "never_bought": [_product("Dud Drops", bought_exact=0, not_bought=4, conversion_rate=0.0)],
    "recent_buyers": [{
        "id": 1, "suggested_at": "2026-10-04T10:00:00+00:00", "channel": "phone", "store": "yakima",
        "customer": {"id": 77, "name": "Pat Example"}, "identity_via": "web_phone",
        "snapshot": {"name": "Zeta Gummies", "brand": "Wyld", "size_label": "100mg", "rank": 2},
        "status": "bought_sibling", "match_kind": "sibling_size", "matched_name": "Zeta Gummies 10pk",
        "matched_amount": 24.0, "matched_at": "2026-10-06T12:00:00+00:00", "days_to_purchase": 2.1}],
}
FUNNEL = {"ok": True, "sessions": 40, "unique_visitors": 31, "actions": {"searches": 55},
          "bounces": {"total": 10, "rate": 0.25},
          "funnel": [{"stage": "Opened chat", "sessions": 40}, {"stage": "Searched", "sessions": 30}],
          "zero_result_searches": [{"slots": "category=edible · size=100mg", "searches": 3}],
          "top_categories": [{"category": "flower", "searches": 20}]}


@pytest.fixture
def budtender(monkeypatch):
    """Replace the dashboard's budtender seam; records (path, payload) and answers per path."""
    calls: list[tuple[str, dict]] = []
    answers = {analytics_views.SUGGESTIONS_PATH: SUGGESTIONS, analytics_views.FUNNEL_PATH: FUNNEL}

    def fake(path, payload):
        calls.append((path, payload))
        answer = answers[path]
        return {**answer, "window_days": payload["days"]} if isinstance(answer, dict) and "totals" in answer else answer

    monkeypatch.setattr("dashboard.views._budtender_post", fake)
    fake.calls, fake.answers = calls, answers
    return fake


def _text(html: str) -> str:
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def _tiles(resp):
    return {k["label"]: k for k in resp.context["kpis"]}


# ── KPI maths ─────────────────────────────────────────────────────────────────
def test_kpis_follow_the_contract_formula(client_staff, budtender):
    resp = client_staff.get(reverse(URL))
    assert resp.status_code == 200
    tiles = _tiles(resp)
    assert tiles["Suggestions shown"]["value"] == 100
    # 40 bought / (40 bought + 30 not bought): the 20 pending and 10 unattributable are left out.
    assert tiles["Conversion rate"]["value"] == "57.1%"
    assert tiles["Conversion rate"]["sub"].startswith("40 bought of 70 decided")
    assert tiles["Exact vs sibling"]["value"] == "75% / 25%"
    assert tiles["Chats"]["value"] == 40


def test_conversion_ignores_pending_and_unattributable():
    assert analytics_views.conversion(30, 10, 30) == 0.5714
    assert analytics_views.conversion(0, 0, 0) is None  # nothing decided is "no rate", not 0%


def test_status_split_keeps_pending_and_unattributable_apart(client_staff, budtender):
    split = {s["label"]: s["n"] for s in client_staff.get(reverse(URL)).context["sug"]["split"]}
    assert split == {"Bought exact": 30, "Bought sibling": 10, "Not bought": 30, "Pending": 20, "Unattributable": 10}


def test_ranks_one_to_three_subtotal(client_staff, budtender):
    top3 = client_staff.get(reverse(URL)).context["sug"]["top3"]
    assert (top3["suggested"], top3["bought_exact"], top3["bought_sibling"], top3["not_bought"]) == (90, 29, 10, 26)
    assert top3["rate"] == "60.0%"


# ── filters reach the budtender ───────────────────────────────────────────────
def test_defaults_send_only_days(client_staff, budtender):
    client_staff.get(reverse(URL))
    assert dict(budtender.calls) == {
        analytics_views.SUGGESTIONS_PATH: {"days": 30},
        analytics_views.FUNNEL_PATH: {"days": 30, "recent": 0},
    }


def test_every_filter_passes_through(client_staff, budtender):
    client_staff.get(reverse(URL), {"days": "custom", "custom_days": "45", "store": "pullman", "channel": "chat"})
    sent = dict(budtender.calls)
    assert sent[analytics_views.SUGGESTIONS_PATH] == {"days": 45, "store": "pullman", "channel": "chat"}
    # the funnel endpoint has no channel filter; days and store do reach it
    assert sent[analytics_views.FUNNEL_PATH] == {"days": 45, "store": "pullman", "recent": 0}


@pytest.mark.parametrize("raw,days", [("7", 7), ("90", 90), ("14", 14), ("9999", 365), ("0", 1), ("junk", 30)])
def test_days_are_bounded(client_staff, budtender, raw, days):
    client_staff.get(reverse(URL), {"days": raw})
    assert dict(budtender.calls)[analytics_views.SUGGESTIONS_PATH]["days"] == days


def test_unknown_store_and_channel_are_dropped(client_staff, budtender):
    client_staff.get(reverse(URL), {"store": "evil", "channel": "<script>"})
    assert dict(budtender.calls)[analytics_views.SUGGESTIONS_PATH] == {"days": 30}


def test_phone_channel_skips_the_chat_funnel(client_staff, budtender):
    resp = client_staff.get(reverse(URL), {"channel": "phone"})
    assert [p for p, _ in budtender.calls] == [analytics_views.SUGGESTIONS_PATH]
    assert resp.context["chat"] is None
    assert "left out while the channel filter is set to Phone" in resp.content.decode()


def test_form_keeps_filters_selected(client_staff, budtender):
    html = client_staff.get(reverse(URL), {"store": "yakima", "channel": "similar", "days": "7"}).content.decode()
    assert re.search(r'<option value="yakima"\s+selected', html)
    assert re.search(r'<option value="similar"\s+selected', html)
    assert re.search(r'name="days" value="7" checked', html)


# ── product table ─────────────────────────────────────────────────────────────
def test_products_sort_and_keep_full_details(client_staff, budtender):
    def names(sort):
        return [p["name"] for p in client_staff.get(reverse(URL), {"sort": sort}).context["sug"]["products"]]

    assert names("name") == ["Alpha Chews", "Mid Mints", "Zeta Gummies"]
    assert names("-price") == ["Zeta Gummies", "Mid Mints", "Alpha Chews"]
    assert names("bogus") == ["Zeta Gummies", "Mid Mints", "Alpha Chews"]  # falls back to most suggested
    text = _text(client_staff.get(reverse(URL)).content.decode())
    for detail in ("Wyld", "Edible", "Sour Apple", "100mg", "$30.00", "18.5%"):
        assert detail in text


def test_sort_links_keep_the_other_filters(client_staff, budtender):
    headers = client_staff.get(reverse(URL), {"store": "yakima", "sort": "name"}).context["sug"]["product_headers"]
    brand = next(h for h in headers if h["label"] == "Brand")
    assert "store=yakima" in brand["href"] and brand["href"].endswith("sort=brand")
    name = next(h for h in headers if h["label"] == "Product")
    assert name["active"] and name["href"].endswith("sort=-name")  # second click flips


# ── customers, trust, partial rows ────────────────────────────────────────────
def test_buyer_without_a_dashboard_id_is_plain_text(client_staff, budtender):
    html = client_staff.get(reverse(URL)).content.decode()
    assert "Pat Example" in html and not re.search(r"/dashboard/customers/\d+/", html)  # budtender id 77 is never guessed
    assert "Typed on website" in html and "lower trust" in html


def test_buyer_links_only_through_one_stored_dashboard_link(client_staff, budtender):
    from crm.models import CustomerProfile

    rows = [{**SUGGESTIONS["recent_buyers"][0], "customer": {"id": 77, "name": "Pat Example", "voice_id": 5}}]
    budtender.answers[analytics_views.SUGGESTIONS_PATH] = {**SUGGESTIONS, "recent_buyers": rows}
    html = client_staff.get(reverse(URL)).content.decode()
    assert not re.search(r"/dashboard/customers/\d+/", html)  # a body-supplied id is never trusted
    pat = CustomerProfile.objects.create(customer_key="pat", name="Pat Example", budtender_customer_id=77,
                                         budtender_link="name_unique")
    assert f'href="/dashboard/customers/{pat.pk}/"' in client_staff.get(reverse(URL)).content.decode()
    CustomerProfile.objects.create(customer_key="pat2", name="Pat Example", budtender_customer_id=77,
                                   budtender_link="manual")
    assert not re.search(r"/dashboard/customers/\d+/", client_staff.get(reverse(URL)).content.decode())


# ── empty vs unreachable ──────────────────────────────────────────────────────
def test_budtender_down_says_so_and_the_rest_renders(client_staff, monkeypatch):
    monkeypatch.setattr("dashboard.views._budtender_post",
                        lambda path, payload: {"ok": False, "reason": "budtender unreachable (ConnectionError)"})
    VoiceCall.objects.create(call_id="d1", store="yakima", outcome="faq_answered")
    resp = client_staff.get(reverse(URL))
    html = resp.content.decode()
    assert resp.status_code == 200
    assert html.count("Analytics service unreachable") == 3  # suggestions, chat funnel, demand signals
    tiles = _tiles(resp)
    assert tiles["Suggestions shown"]["value"] == "n/a" and tiles["Conversion rate"]["value"] == "n/a"
    assert tiles["Chats"]["value"] == "n/a"
    assert tiles["Calls"]["value"] == 1  # voice-side numbers are still real
    assert "No suggestions were recorded" not in html  # unreachable is not "empty"


def test_zero_suggestions_is_an_authoritative_empty(client_staff, budtender):
    budtender.answers[analytics_views.SUGGESTIONS_PATH] = {
        "ok": True, "totals": {k: 0 for k in SUGGESTIONS["totals"] if k != "conversion_rate"} | {"conversion_rate": None}}
    resp = client_staff.get(reverse(URL))
    html = resp.content.decode()
    assert "No suggestions were recorded in this window" in html
    assert "Analytics service unreachable" not in html
    assert _tiles(resp)["Conversion rate"]["value"] == "-"


@pytest.mark.parametrize("answer", [{"ok": True}, {"ok": True, "totals": "oops"}, [], None, {"ok": False}])
def test_malformed_answers_degrade_never_500(client_staff, monkeypatch, answer):
    monkeypatch.setattr("dashboard.views._budtender_post", lambda path, payload: answer)
    assert client_staff.get(reverse(URL)).status_code == 200


def test_a_raising_budtender_seam_never_500s(client_staff, monkeypatch):
    def boom(path, payload):
        raise RuntimeError("boom")

    monkeypatch.setattr("dashboard.views._budtender_post", boom)
    resp = client_staff.get(reverse(URL))
    assert resp.status_code == 200 and "Analytics service unreachable" in resp.content.decode()


def test_unconfigured_budtender_is_unreachable_not_zero(client_staff, settings):
    settings.HHT_BUDTENDER_BASE_URL = ""
    html = client_staff.get(reverse(URL)).content.decode()
    assert "Analytics service unreachable" in html and "not configured" in html


# ── nothing private on the page ───────────────────────────────────────────────
def test_no_phone_numbers_cost_or_margin_reach_the_page(client_staff, budtender):
    dirty = {**SUGGESTIONS,
             "top_products": [_product("Zeta Gummies", phone="509-555-0142", cost=11.5, margin=0.61, unit_cost=9)],
             "recent_buyers": [{**SUGGESTIONS["recent_buyers"][0], "phone": "509-555-0142", "cost": 11.5,
                                "customer": {"id": 77, "name": "Pat Example", "phone": "+15095550142"},
                                "snapshot": {"name": "Zeta Gummies", "cost": 11.5, "margin": 0.61, "rank": 2}}]}
    budtender.answers[analytics_views.SUGGESTIONS_PATH] = dirty
    VoiceCall.objects.create(call_id="p1", store="yakima", outcome="suggested", caller_phone_hash="a" * 64)
    text = _text(client_staff.get(reverse(URL)).content.decode())
    assert not re.search(r"\+?\d?[\s.-]?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}", text)
    assert "5095550142" not in text and "a" * 64 not in text
    for word in ("cost", "margin", "11.5", "0.61"):
        assert word not in text.lower(), word


def test_page_loads_no_remote_script(client_staff, budtender):
    assert not re.search(r"<script[^>]+src=\"https?://", client_staff.get(reverse(URL)).content.decode())


# ── gating ────────────────────────────────────────────────────────────────────
def test_anonymous_and_non_staff_are_sent_to_login(client, db):
    assert client.get(reverse(URL)).status_code == 302
    client.force_login(User.objects.create_user("shopper", password="x"))
    resp = client.get(reverse(URL))
    assert resp.status_code == 302 and "login" in resp["Location"]


# ── call metrics ──────────────────────────────────────────────────────────────
def _call(call_id, when, **kw):
    vc = VoiceCall.objects.create(call_id=call_id, **kw)
    VoiceCall.objects.filter(pk=vc.pk).update(created_at=when)
    return vc


SINCE = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.django_db
def test_heatmap_buckets_in_store_time_near_midnight():
    # 06:50 UTC on Oct 6 is 23:50 PDT on Mon Oct 5; 07:10 UTC is 00:10 PDT on Tue Oct 6.
    _call("late", datetime(2026, 10, 6, 6, 50, tzinfo=UTC), store="yakima")
    _call("early", datetime(2026, 10, 6, 7, 10, tzinfo=UTC), store="yakima")
    m = analytics_calls.call_metrics(SINCE)
    assert m["heat"][0][23] == 1  # Monday 11pm
    assert m["heat"][1][0] == 1  # Tuesday 12am
    assert m["heat"][1][6] == 0 and sum(map(sum, m["heat"])) == 2  # not UTC's Tuesday 6am
    assert {d.isoformat(): n for d, n in m["by_day"].items()} == {"2026-10-05": 1, "2026-10-06": 1}
    assert m["peak"]["n"] == 1


@pytest.mark.django_db
def test_call_filters_store_and_channel(client_staff, budtender):
    _call("y1", datetime.now(UTC), store="yakima", outcome="suggested")
    _call("y2", datetime.now(UTC), store="yakima", outcome="faq_answered")
    _call("p1", datetime.now(UTC), store="pullman", outcome="faq_answered")
    assert client_staff.get(reverse(URL)).context["calls"]["total"] == 3
    assert client_staff.get(reverse(URL), {"store": "yakima"}).context["calls"]["total"] == 2
    assert client_staff.get(reverse(URL), {"store": "pullman", "channel": "phone"}).context["calls"]["total"] == 1
    chat = client_staff.get(reverse(URL), {"channel": "chat"})
    assert chat.context["calls"] is None  # calls are phone only
    assert _tiles(chat)["Calls"]["value"] == "-"


@pytest.mark.django_db
def test_call_window_respects_days(client_staff, budtender):
    _call("old", datetime.now(UTC).replace(year=2020), store="yakima")
    _call("new", datetime.now(UTC), store="yakima")
    assert client_staff.get(reverse(URL), {"days": "30"}).context["calls"]["total"] == 1


@pytest.mark.django_db
def test_durations_transfers_and_outcome_labels():
    now = datetime.now(UTC)
    for i, secs in enumerate([10, 20, 30, 40, 100]):
        _call(f"dur{i}", now, duration_s=secs, outcome="faq_answered")
    _call("t1", now, escalated=True, transfer_disposition="connected", outcome="escalation")
    _call("t2", now, escalated=True, transfer_disposition="connected", outcome="escalation")
    _call("t3", now, escalated=True, transfer_disposition="no_answer", outcome="transfer_unavailable")
    _call("v1", now, outcome="vendor_callback")
    _call("v2", now, outcome="vendor_direct")
    m = analytics_calls.call_metrics(SINCE)
    assert (m["duration"]["median"], m["duration"]["p90"], m["duration"]["n"]) == (30, 100, 5)
    assert analytics_calls.fmt_duration(100) == "1m 40s"
    assert m["transfers"]["attempts"] == 3 and m["transfers"]["connected"] == 2
    assert m["transfers"]["rate"] == 0.6667
    assert (m["vendor_callbacks"], m["vendor_direct"], m["transfer_unavailable"]) == (1, 1, 1)
    labels = {o["key"]: o["label"] for o in m["outcomes"]}
    assert labels["transfer_unavailable"] == "Transfer: person unavailable"
    assert labels["vendor_direct"] == "Vendor sent to owner"


@pytest.mark.django_db
def test_transfer_tile(client_staff, budtender):
    now = datetime.now(UTC)
    for i, disposition in enumerate(["connected", "connected", "no_answer"]):
        _call(f"x{i}", now, escalated=True, transfer_disposition=disposition, outcome="escalation")
    tile = _tiles(client_staff.get(reverse(URL)))["Transfer success"]
    assert tile["value"] == "66.7%" and "2 of 3" in tile["sub"]


@pytest.mark.django_db
def test_call_metrics_query_count_does_not_grow_with_calls(django_assert_num_queries):
    now = datetime.now(UTC)
    _call("one", now, store="yakima", duration_s=5, escalated=True, transfer_disposition="connected")
    with django_assert_num_queries(6):
        analytics_calls.call_metrics(SINCE)
    for i in range(40):
        _call(f"many{i}", now, store=("yakima", "pullman")[i % 2], duration_s=i, outcome="suggested")
    with django_assert_num_queries(6):
        analytics_calls.call_metrics(SINCE)


@pytest.mark.django_db
def test_page_query_count_is_flat(client_staff, budtender, django_assert_max_num_queries):
    now = datetime.now(UTC)
    for i in range(60):
        _call(f"q{i}", now, store="yakima", duration_s=i, outcome="suggested")
    with django_assert_max_num_queries(12):
        assert client_staff.get(reverse(URL)).status_code == 200


# ── charts ────────────────────────────────────────────────────────────────────
def test_stacked_columns_follow_the_mark_specs():
    series = [("a", "A", "c-exact"), ("b", "B", "c-not")]
    chart = analytics_views.stacked_columns(
        [{"label": "d1", "values": {"a": 3, "b": 1}}, {"label": "d2", "values": {"a": 0, "b": 0}}], series)
    assert chart["cols"][0]["total"] == 4 and len(chart["cols"][0]["segs"]) == 2
    assert chart["cols"][1]["segs"] == []  # a zero day draws nothing
    assert chart["ticks"][0]["label"] == 0 and chart["ticks"][-1]["label"] >= 4


def test_bar_path_rounds_the_data_end_only():
    path = analytics_views._bar_path(10, 20, 24, 30, 4)
    assert path.startswith("M10.0,50.0V24.0Q10.0,20.0 14.0,20.0H30.0Q34.0,20.0 34.0,24.0V50.0Z")  # square at y=50
    assert analytics_views._bar_path(0, 0, 24, 30, 0) == "M0.0,30.0V0.0H24.0V30.0Z"


def test_long_windows_are_drawn_per_week(client_staff, budtender):
    resp = client_staff.get(reverse(URL), {"days": "365"})
    # the fixture carries two days, but the axis spans the whole year, so it is bucketed by week
    assert resp.context["sug"]["trend"]["unit"] == "week"
    assert resp.context["calls"]["volume"]["unit"] == "week"
    assert len(resp.context["sug"]["trend"]["cols"]) <= 54
    assert resp.context["sug"]["trend"]["total"] == 10


def test_nice_axis_tops():
    assert analytics_views._nice_top(0) == (4, 1)
    assert analytics_views._nice_top(7) == (8, 2)
    assert analytics_views._nice_top(23) == (30, 10)
    assert analytics_views._nice_top(180) == (200, 50)
