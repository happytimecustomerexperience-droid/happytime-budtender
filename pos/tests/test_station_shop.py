"""The station/shop split, and the cart that must already be loaded.

This runs on a tablet at the counter. The budtender's first job is to see who is
waiting and check someone in; a 4,700-product grid under that is noise until a
customer exists. So `/pos/` is the STATION (queue, orders waiting, scan, lookup)
and `/pos/shop/` is the MENU, reachable only once a customer is selected.

The behaviour worth defending hardest is the last test class: someone who ordered
online and then walked in used to be claimed off the door queue with an EMPTY cart,
because `claim` and `phone_cart_claim` never knew about each other. The budtender
had to notice the separate "Orders waiting" row and match it up by hand, with the
customer standing there.

Dutchie is never hit — the register client and inventory are monkeypatched.
"""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from budtender.models import PhoneCartDraft
from customers.models import ShopVisit
from dutchie.session import Store
from pos import views as V

pytestmark = pytest.mark.django_db

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=1, lsp_id=1, loc_id=1, register_id=700318, username="u", password="p",
              api_key="k")

# A normalised `pos.catalog` row. `catalog.facets` indexes several of these keys
# directly (strain_type, effects, price…), so a thinner fixture KeyErrors inside the
# menu render rather than failing the assertion under test.
ITEM = {"ProductId": "1", "ProductDesc": "Blue Dream 3.5g", "UnitPrice": 25.0,
        "SerialNo": "S1", "brand": "Athenry", "cat_key": "flower", "subcategory": "3.5g",
        "TotalAvailable": 9,
        "product_id": "1", "name": "Blue Dream 3.5g", "cat_label": "Flower",
        "strain": "Blue Dream", "strain_type": "Hybrid", "effects": [], "flavors": [],
        "price": 25.0, "qty": 9, "thc": 22.0, "cbd": 0.0, "image": "", "img": "",
        "img_static": True, "bucket": "", "velocity": 0.0, "margin_pct": 0.0,
        "price_z": 0.0, "vendor": "", "received_date": "", "unit_grams": 3.5,
        "package_id": "P1", "BatchId": 9, "price_was": None}


@pytest.fixture
def auth(client, db):
    client.force_login(User.objects.create_user("bud", password="pw12345!"))
    return client


@pytest.fixture(autouse=True)
def _no_dutchie(monkeypatch):
    """Every seam that would reach the register."""
    monkeypatch.setattr(V, "load_stores", lambda: {STORE.name: STORE})
    monkeypatch.setattr(V.catalog, "get_inventory", lambda store, **kw: [ITEM])
    monkeypatch.setattr(V.catalog, "find_item",
                        lambda store, product_id=None, **kw: dict(ITEM) if product_id == "1" else None)
    monkeypatch.setattr(V, "_client", lambda store: _FakeClient())
    monkeypatch.setattr(V, "_sync_customer_to_dutchie", lambda *a, **kw: None)
    monkeypatch.setattr(V, "upsert_customer", lambda *a, **kw: None)
    monkeypatch.setattr(V, "load_customer_history", lambda **kw: None)


class _FakeClient:
    def guest_search(self, q=""):
        return {"Data": [{"Guest_id": 555, "Name": "Jane Q", "PhoneNo": "5095551234"}]}

    def create_guest(self, **kw):
        return 777

    def price_check(self, serial):
        return {"Result": True, "Data": {"Price": "$ 25.00", "Quantity": 9}}


def _sess(client, **kv):
    s = client.session
    for k, v in kv.items():
        s[k] = v
    s.save()


def _draft(**kw):
    kw.setdefault("location_slug", "yakima")
    kw.setdefault("status", PhoneCartDraft.Status.RELEASED)
    kw.setdefault("released_at", timezone.now())
    kw.setdefault("lines", [{"product_id": "1", "name": "Blue Dream 3.5g", "quantity": 2}])
    kw.setdefault("pickup_name", "Jane Q")
    return PhoneCartDraft.objects.create(**kw)


class TestTheSplit:
    def test_the_station_is_the_landing_and_has_no_menu(self, auth):
        _sess(auth, role="budtender", store="yakima")
        body = auth.get(reverse("screen")).content.decode()
        assert "Orders waiting" in body
        assert "Search customer by phone" in body
        # The menu container must NOT be on the station — that is the whole point.
        assert 'id="menu"' not in body

    def test_the_shop_bounces_back_when_nobody_is_checked_in(self, auth):
        _sess(auth, role="budtender", store="yakima")
        r = auth.get(reverse("shop"))
        assert r.status_code == 302
        assert r["Location"] == reverse("screen")

    def test_the_shop_renders_the_menu_once_a_customer_is_selected(self, auth):
        _sess(auth, role="budtender", store="yakima", acct_id=555, acct_name="Jane Q")
        body = auth.get(reverse("shop")).content.decode()
        assert 'id="menu"' in body
        assert "Station" in body          # always one tap back to the queue

    def test_the_menu_partial_still_reads_live_inventory(self, auth):
        # The grid itself is served by `menu`, which reads pos.catalog — the same
        # live register source the public storefront uses.
        _sess(auth, role="budtender", store="yakima", acct_id=555)
        body = auth.get(reverse("menu")).content.decode()
        assert "Blue Dream 3.5g" in body

    def test_a_door_user_never_lands_on_the_shop(self, auth):
        _sess(auth, role="door", store="yakima", acct_id=555)
        r = auth.get(reverse("shop"))
        assert r.status_code == 302
        assert r["Location"] == reverse("door")


class TestCheckInAdvancesToTheMenu:
    def test_claiming_a_queued_customer_moves_the_tablet_to_the_shop(self, auth):
        _sess(auth, role="budtender", store="yakima")
        v = ShopVisit.objects.create(store="yakima", status="queued", acct_id=555,
                                     acct_name="Jane Q", phone="5095551234")
        r = auth.post(reverse("claim", args=[v.id]))
        assert r.status_code == 200
        # htmx does a real client-side navigation on this header; without it the
        # profile fragment would be swapped into a page that shows no menu.
        assert r["HX-Redirect"] == reverse("shop")

    def test_starting_a_guest_moves_the_tablet_to_the_shop(self, auth):
        _sess(auth, role="budtender", store="yakima")
        r = auth.post(reverse("guest_start"))
        assert r["HX-Redirect"] == reverse("shop")

    def test_loading_an_order_by_token_moves_the_tablet_to_the_shop(self, auth):
        _sess(auth, role="budtender", store="yakima")
        d = _draft(contact_phone="5095551234")
        r = auth.post(reverse("phone_cart_claim"), {"draft_token": d.draft_token})
        assert r["HX-Redirect"] == reverse("shop")


class TestClaimingLoadsTheWaitingOrder:
    """The one that matters: check the person in, their cart is already there."""

    def _queued(self, **kw):
        kw.setdefault("store", "yakima")
        kw.setdefault("status", "queued")
        return ShopVisit.objects.create(**kw)

    def test_a_queued_customer_with_an_online_order_gets_it_loaded(self, auth):
        _sess(auth, role="budtender", store="yakima")
        _draft(contact_phone="5095551234")
        v = self._queued(acct_id=555, acct_name="Jane Q", phone="5095551234")

        auth.post(reverse("claim", args=[v.id]))

        cart = auth.session["cart"]
        assert len(cart) == 1
        assert cart[0]["ProductId"] == "1"
        assert cart[0]["Cnt"] == 2                 # the quantity they actually ordered

    def test_the_order_is_marked_claimed_so_it_leaves_the_waiting_list(self, auth):
        _sess(auth, role="budtender", store="yakima")
        d = _draft(contact_phone="5095551234")
        v = self._queued(acct_id=555, acct_name="Jane Q", phone="5095551234")

        auth.post(reverse("claim", args=[v.id]))

        d.refresh_from_db()
        assert d.status == PhoneCartDraft.Status.CLAIMED
        assert d.claimed_at is not None
        assert any(e.get("action") == "pos_claim_via_queue" for e in d.audit)

    def test_staff_is_told_the_cart_was_pre_loaded(self, auth):
        # Otherwise they find items in the drawer they did not add and assume a bug.
        _sess(auth, role="budtender", store="yakima")
        _draft(contact_phone="5095551234")
        v = self._queued(acct_id=555, acct_name="Jane Q", phone="5095551234")
        body = auth.post(reverse("claim", args=[v.id])).content.decode()
        assert "online order" in body

    def test_it_matches_on_the_phone_when_the_account_id_differs(self, auth):
        # The order was placed before the account existed, so it carries only a phone.
        _sess(auth, role="budtender", store="yakima")
        _draft(contact_phone="5095551234")
        v = self._queued(acct_id=999, acct_name="Jane Q", phone="(509) 555-1234")
        auth.post(reverse("claim", args=[v.id]))
        assert len(auth.session["cart"]) == 1

    def test_a_stranger_does_not_inherit_someone_elses_order(self, auth):
        _sess(auth, role="budtender", store="yakima")
        _draft(contact_phone="5095551234")
        v = self._queued(acct_id=444, acct_name="Someone Else", phone="5090000000")
        auth.post(reverse("claim", args=[v.id]))
        assert auth.session.get("cart", []) == []

    def test_an_order_at_another_store_is_not_loaded(self, auth):
        # A Pullman order must never appear on the Yakima register.
        _sess(auth, role="budtender", store="yakima")
        _draft(contact_phone="5095551234", location_slug="pullman")
        v = self._queued(acct_id=555, acct_name="Jane Q", phone="5095551234")
        auth.post(reverse("claim", args=[v.id]))
        assert auth.session.get("cart", []) == []

    def test_an_open_cart_is_not_loaded(self, auth):
        # `open` means they are still browsing — loading it creates an order nobody
        # placed and empties their cart out from under them.
        _sess(auth, role="budtender", store="yakima")
        _draft(contact_phone="5095551234", status=PhoneCartDraft.Status.OPEN)
        v = self._queued(acct_id=555, acct_name="Jane Q", phone="5095551234")
        auth.post(reverse("claim", args=[v.id]))
        assert auth.session.get("cart", []) == []

    def test_claiming_without_any_order_still_works(self, auth):
        _sess(auth, role="budtender", store="yakima")
        v = self._queued(acct_id=555, acct_name="Jane Q", phone="5095551234")
        r = auth.post(reverse("claim", args=[v.id]))
        assert r.status_code == 200
        assert auth.session.get("cart", []) == []


class TestLoadedLinesArePricedLive:
    def test_a_loaded_line_takes_the_live_price_check_value(self, auth, monkeypatch):
        """Same per-serial confirmation `cart_add` does for a walk-in.

        Without it a claimed online order was priced off the browse cache while an
        identical walk-in add got a live check — two prices for one product in one POS.
        """
        class Pricier(_FakeClient):
            def price_check(self, serial):
                return {"Result": True, "Data": {"Price": "$ 31.00", "Quantity": 9}}

        monkeypatch.setattr(V, "_client", lambda store: Pricier())
        _sess(auth, role="budtender", store="yakima")
        d = _draft(contact_phone="5095551234")
        auth.post(reverse("phone_cart_claim"), {"draft_token": d.draft_token})
        assert auth.session["cart"][0]["UnitPrice"] == 31.00

    def test_a_failing_price_check_keeps_the_cached_price(self, auth, monkeypatch):
        class Broken(_FakeClient):
            def price_check(self, serial):
                raise RuntimeError("dutchie down")

        monkeypatch.setattr(V, "_client", lambda store: Broken())
        _sess(auth, role="budtender", store="yakima")
        d = _draft(contact_phone="5095551234")
        auth.post(reverse("phone_cart_claim"), {"draft_token": d.draft_token})
        assert auth.session["cart"][0]["UnitPrice"] == 25.0


class TestLabAndFilters:
    """The lab panel, the new strain filter, and the THC filter that was lying."""

    def test_the_lab_panel_shows_thca_not_the_menu_row_number(self, auth, monkeypatch):
        # The whole point: the card says 22% THC (decarbed), the panel says 48% THCA.
        monkeypatch.setattr(V.dutchie_lab, "lab_result", lambda store, batch: {
            "cannabinoids": [{"name": "THCA", "value": 48.0, "unit": "%"}],
            "total_cannabinoids": {"value": 44.996}})
        _sess(auth, role="budtender", store="yakima", acct_id=555)
        body = auth.get(reverse("product_lab", args=["1"])).content.decode()
        assert "48.0" in body and "THCA" in body
        assert "45.0" in body and "Total" in body

    def test_no_lab_data_says_so_instead_of_erroring(self, auth, monkeypatch):
        monkeypatch.setattr(V.dutchie_lab, "lab_result", lambda store, batch: None)
        _sess(auth, role="budtender", store="yakima", acct_id=555)
        r = auth.get(reverse("product_lab", args=["1"]))
        assert r.status_code == 200
        assert "No lab data" in r.content.decode()

    def test_a_door_user_cannot_read_lab_data(self, auth):
        _sess(auth, role="door", store="yakima")
        assert auth.get(reverse("product_lab", args=["1"])).status_code in (302, 403)


class TestCredibleThcFilter:
    """`THC >= 25%` matched 0 of 644 flower products because THCContent is decarbed
    THC, not THCA. Filtering must not match on a number that isn't a percentage."""

    def _flower(self, thc):
        return {"cat_key": "flower", "thc": thc, "strain": "Blue Dream", "qty": 5,
                "name": "x", "brand": "b", "price": 10.0, "strain_type": "", "effects": [],
                "raw_category": "flower", "subcategory": "3.5g"}

    def test_a_decarbed_reading_never_satisfies_a_percentage_filter(self):
        from pos.catalog import query
        rows = [self._flower(0.5), self._flower(0.15)]
        assert query(rows, None, {"thc_min": 20}) == []

    def test_a_real_percentage_still_passes(self):
        from pos.catalog import query
        rows = [self._flower(22.0)]
        assert len(query(rows, None, {"thc_min": 20})) == 1

    def test_an_impossible_value_is_not_treated_as_potency(self):
        from pos.catalog import query
        # 1400% — a fraction misread as a percentage. Must not satisfy anything.
        assert query([self._flower(1400.0)], None, {"thc_min": 20}) == []

    def test_the_strain_filter_narrows(self):
        from pos.catalog import query
        a, b = self._flower(22.0), self._flower(22.0)
        b["strain"] = "Gelato"
        assert len(query([a, b], None, {"strain": "Gelato"})) == 1


class TestSavedOrderByPhone:
    """A budtender has the customer's phone, not a `pc-…` token."""

    def test_a_phone_number_finds_the_saved_order(self, auth):
        _sess(auth, role="budtender", store="yakima")
        d = _draft(contact_phone="5094206999")
        r = auth.post(reverse("phone_cart_claim"), {"draft_token": "509-420-6999"})
        assert r["HX-Redirect"] == reverse("shop")
        d.refresh_from_db()
        assert d.status == PhoneCartDraft.Status.CLAIMED

    def test_country_code_and_punctuation_do_not_matter(self, auth):
        _sess(auth, role="budtender", store="yakima")
        _draft(contact_phone="(509) 420-6999")
        r = auth.post(reverse("phone_cart_claim"), {"draft_token": "+1 509 420 6999"})
        assert r["HX-Redirect"] == reverse("shop")

    def test_the_token_still_works_for_the_queue_panel(self, auth):
        # The queue panel posts a hidden draft_token; that must not regress.
        _sess(auth, role="budtender", store="yakima")
        d = _draft(contact_phone="5094206999")
        r = auth.post(reverse("phone_cart_claim"), {"draft_token": d.draft_token})
        assert r["HX-Redirect"] == reverse("shop")

    def test_the_newest_order_wins_for_a_repeat_customer(self, auth):
        _sess(auth, role="budtender", store="yakima")
        old = _draft(contact_phone="5094206999")
        old.released_at = timezone.now() - timedelta(days=2)
        old.save(update_fields=["released_at"])
        new = _draft(contact_phone="5094206999")
        auth.post(reverse("phone_cart_claim"), {"draft_token": "5094206999"})
        new.refresh_from_db(); old.refresh_from_db()
        assert new.status == PhoneCartDraft.Status.CLAIMED
        assert old.status == PhoneCartDraft.Status.RELEASED

    def test_an_unknown_number_says_so_without_erroring(self, auth):
        _sess(auth, role="budtender", store="yakima")
        r = auth.post(reverse("phone_cart_claim"), {"draft_token": "5095550000"})
        assert r.status_code == 200
        assert "No saved order" in r.content.decode()
