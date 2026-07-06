"""Session-activity tracking — visit lifecycle, event log, degrade-safety, no-PII,
dedupe, the operator dashboard, and the purge command.

Tracking must NEVER break a page: the degrade tests assert a thrown error is swallowed.
"""

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from pos import views as V
from customers import tracking
from customers.models import ShopEvent, ShopVisit
from dutchie.session import Store

pytestmark = pytest.mark.django_db

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=700002, lsp_id=700045, loc_id=700498, register_id=700318,
              username="u", password="p", api_key="k")


@pytest.fixture
def user(db):
    return User.objects.create_user("bud", password="pw12345!")


@pytest.fixture
def auth(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def admin(client, db):
    client.force_login(User.objects.create_user("admin", password="pw12345!", is_staff=True))
    return client


def _use_store(monkeypatch, store=STORE):
    monkeypatch.setattr(V, "load_stores", lambda: {store.name: store} if store else {})


class _User:
    username = "bud"


def _req():
    """A bare request whose session is a plain dict — all tracking touches is get/set/pop."""
    r = RequestFactory().get("/")
    r.session = {}
    r.user = _User()
    return r


# ── unit: visit lifecycle + event log ─────────────────────────────────────────
def test_full_lifecycle_records_ordered_events_and_rollups():
    r = _req()
    r.session["store"] = "yakima"
    v = tracking.start_visit(r, acct_id=770001, name="Jane", phone="509", how="lookup")
    assert v.outcome == "open" and r.session["visit_id"] == v.id

    tracking.track(r, "search", detail="gummies", results=5)
    prod = {"product_id": "5001", "name": "Blue Dream"}
    tracking.track(r, "product_view", product=prod, dedupe_key="5001")
    tracking.track(r, "product_view", product=prod, dedupe_key="5001")          # duplicate
    tracking.track(r, "suggestions_shown", dedupe_key="a,b", ids=["a", "b"])
    tracking.track(r, "item_add", product=prod, price=40, qty=2)
    tracking.track(r, "checkout", detail="1 items", total=40)
    tracking.end_visit(r, "checked_out", shipment_id=999, cart_total=40)

    v.refresh_from_db()
    assert v.outcome == "checked_out" and v.ended_at is not None
    assert v.order_shipment_id == 999 and float(v.cart_total) == 40.0
    kinds = list(v.events.values_list("kind", flat=True))
    # how="lookup" also emits a `customer_selected` marker right after visit_start.
    assert kinds == ["visit_start", "customer_selected", "search", "product_view",
                     "suggestions_shown", "item_add", "checkout"]   # deduped product_view
    assert v.items_viewed == 1 and v.items_added == 1
    assert "visit_id" not in r.session


def test_switching_customer_closes_prior_visit():
    r = _req()
    v1 = tracking.start_visit(r, acct_id=1, name="A")
    v2 = tracking.start_visit(r, acct_id=2, name="B")
    v1.refresh_from_db()
    assert v1.outcome == "abandoned" and v1.ended_at is not None
    assert v2.id != v1.id and r.session["visit_id"] == v2.id


def test_same_customer_reuses_open_visit():
    r = _req()
    v1 = tracking.start_visit(r, acct_id=1, name="A")
    v2 = tracking.start_visit(r, acct_id=1, name="A")
    assert v1.id == v2.id and ShopVisit.objects.count() == 1


def test_track_is_noop_without_open_visit():
    tracking.track(_req(), "product_view", product={"product_id": "1", "name": "x"})
    assert ShopEvent.objects.count() == 0


def test_login_event_is_standalone():
    tracking.track(_req(), "login")
    e = ShopEvent.objects.get()
    assert e.kind == "login" and e.visit is None and e.budtender == "bud"


# ── P1: secondary events + dimension stamping + stale-close ───────────────────
def test_scan_visit_emits_id_scan_event():
    tracking.start_visit(_req(), acct_id=5, name="Al", how="scan", scan_over21=True)
    ev = ShopEvent.objects.get(kind="id_scan")
    assert ev.meta.get("over_21") is True


def test_lookup_visit_emits_customer_selected():
    tracking.start_visit(_req(), acct_id=6, name="Bo", how="phone")
    assert ShopEvent.objects.filter(kind="customer_selected").exists()


def test_guest_visit_has_no_secondary_marker():
    tracking.start_visit(_req(), acct_id=7, name="Guest", how="guest")
    assert set(ShopEvent.objects.values_list("kind", flat=True)) == {"visit_start"}


def test_product_view_stamps_brand_and_category_from_product():
    r = _req()
    tracking.start_visit(r, acct_id=8, name="Cy", how="lookup")
    tracking.track(r, "product_view", dedupe_key="9",
                   product={"product_id": "9", "name": "Zkittlez", "brand": "House", "cat_key": "flower"})
    ev = ShopEvent.objects.get(kind="product_view")
    assert ev.brand == "House" and ev.category == "flower"


def test_item_add_stamps_explicit_brand_category():
    r = _req()
    tracking.start_visit(r, acct_id=9, name="Di", how="lookup")
    # a trimmed cart line has no brand/cat — cart_add passes them explicitly from the product
    tracking.track(r, "item_add", product={"ProductId": "9", "ProductDesc": "Zkittlez"},
                   brand="House", category="flower", qty=1)
    ev = ShopEvent.objects.get(kind="item_add")
    assert ev.brand == "House" and ev.category == "flower"


def test_close_stale_visits_command():
    r = _req()
    v = tracking.start_visit(r, acct_id=10, name="Ed", how="lookup")
    old = timezone.now() - timezone.timedelta(hours=3)
    ShopVisit.objects.filter(id=v.id).update(started_at=old)
    ShopEvent.objects.filter(visit=v).update(at=old)      # no recent activity
    call_command("close_stale_visits", "--minutes", "45")
    v.refresh_from_db()
    assert v.outcome == "abandoned" and v.ended_at is not None
    assert ShopEvent.objects.filter(visit=v, kind="abandon").exists()


def test_no_pii_persisted():
    """The scan 21+ flag is fine to keep; DOB / ID# must never land in an event."""
    r = _req()
    tracking.start_visit(r, acct_id=1, name="A", phone="509", how="scan", scan_over21=True)
    blob = " ".join(f"{e.meta} {e.detail} {e.product_name}" for e in ShopEvent.objects.all()).lower()
    assert "dob" not in blob and "birth" not in blob and "mjstateid" not in blob


# ── degrade safety: a failure inside tracking never reaches the request ────────
def test_start_visit_degrades(monkeypatch):
    def boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ShopVisit.objects, "create", boom)
    assert tracking.start_visit(_req(), acct_id=1) is None     # swallowed, returns None


def test_track_degrades(monkeypatch):
    r = _req()
    tracking.start_visit(r, acct_id=1)
    monkeypatch.setattr(tracking, "_log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    tracking.track(r, "search", detail="q")                    # must not raise


def test_start_visit_clears_taste_even_on_post_create_failure(monkeypatch):
    """A prior shopper's taste must never leak into the next customer, even if start_visit
    fails AFTER creating the visit row."""
    r = _req()
    r.session["taste"] = {"category": {"Flower": 9}}           # previous customer's taste
    monkeypatch.setattr(tracking, "_log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert tracking.start_visit(r, acct_id=2) is None          # _log raises post-create
    assert "taste" not in r.session                            # cleared before create


# ── integration: views wire the hooks ─────────────────────────────────────────
def test_end_session_abandons_open_visit(auth, monkeypatch):
    _use_store(monkeypatch)
    v = ShopVisit.objects.create(store="yakima", budtender="bud", acct_id=1, acct_name="A")
    s = auth.session
    s["visit_id"] = v.id
    s["acct_id"] = 1
    s.save()
    auth.get(reverse("end"), SERVER_NAME="localhost")
    v.refresh_from_db()
    assert v.outcome == "abandoned" and v.ended_at is not None


def test_cart_add_logs_item_add(auth, monkeypatch):
    _use_store(monkeypatch)
    v = ShopVisit.objects.create(store="yakima", budtender="bud", acct_id=1, acct_name="A")
    s = auth.session
    s["visit_id"] = v.id
    s["cart"] = []
    s.save()
    row = {"ProductId": 1, "BatchId": 2, "SerialNo": "S1", "UnitPrice": 25.0,
           "RecUnitPrice": 25.0, "ProductDesc": "Real Product", "CannbisProduct": "Yes"}
    monkeypatch.setattr(V.catalog, "find_item", lambda store, product_id=None, serial=None: dict(row))
    monkeypatch.setattr(V, "_client", lambda store: type("C", (), {"price_check": lambda self, x: {}})())
    auth.post(reverse("cart_add"), {"ProductId": "1", "Cnt": "2"}, SERVER_NAME="localhost")
    e = ShopEvent.objects.get(kind="item_add")
    assert e.product_name == "Real Product" and e.meta.get("qty") == 2


def test_dashboard_pages_render(admin, monkeypatch):
    _use_store(monkeypatch)
    v = ShopVisit.objects.create(store="yakima", budtender="bud", acct_id=1, acct_name="Jane",
                                 outcome="checked_out", ended_at=timezone.now())
    ShopEvent.objects.create(visit=v, kind="product_view", product_name="OG Kush")
    assert admin.get(reverse("sessions"), SERVER_NAME="localhost").status_code == 200
    assert admin.get(reverse("sessions_active"), SERVER_NAME="localhost").status_code == 200
    rr = admin.get(reverse("sessions_rollups"), SERVER_NAME="localhost")
    assert rr.status_code == 200 and b"Per budtender" in rr.content
    dr = admin.get(reverse("session_detail", args=[v.id]), SERVER_NAME="localhost")
    assert dr.status_code == 200 and b"OG Kush" in dr.content and b"Viewed product" in dr.content


def test_sessions_require_admin(auth):
    v = ShopVisit.objects.create(store="yakima", budtender="bud", acct_id=1, acct_name="Jane")
    assert auth.get(reverse("sessions"), SERVER_NAME="localhost").status_code == 403
    assert auth.get(reverse("sessions_active"), SERVER_NAME="localhost").status_code == 403
    assert auth.get(reverse("sessions_rollups"), SERVER_NAME="localhost").status_code == 403
    assert auth.get(reverse("session_detail", args=[v.id]), SERVER_NAME="localhost").status_code == 403


def test_admin_can_close_and_delete_session(admin):
    v = ShopVisit.objects.create(store="yakima", budtender="bud", acct_id=1, acct_name="Jane")

    r = admin.post(reverse("session_close", args=[v.id]), SERVER_NAME="localhost")
    assert r.status_code == 302
    v.refresh_from_db()
    assert v.outcome == "abandoned" and v.ended_at is not None
    assert ShopEvent.objects.filter(visit=v, kind="admin_close").exists()

    r = admin.post(reverse("session_delete", args=[v.id]), SERVER_NAME="localhost")
    assert r.status_code == 302
    assert not ShopVisit.objects.filter(id=v.id).exists()
    assert ShopEvent.objects.filter(visit__isnull=True, kind="admin_delete").exists()


def test_purge_visits_deletes_only_old(monkeypatch):
    old = ShopVisit.objects.create(store="yakima")
    ShopVisit.objects.filter(id=old.id).update(
        started_at=timezone.now() - timezone.timedelta(days=400))
    new = ShopVisit.objects.create(store="yakima")
    call_command("purge_visits", days=365)
    assert not ShopVisit.objects.filter(id=old.id).exists()
    assert ShopVisit.objects.filter(id=new.id).exists()
