"""The public storefront: menu, cart, checkout, and the leak guard.

These views are the only unauthenticated HTML in the app, so the leak assertions
here matter as much as the happy paths.
"""
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from budtender.models import PhoneCartDraft, Product
from bundles import cart as cart_mod
from bundles import signing
from bundles.tests.test_resolver import live

SECRET = "unit-test-secret-value"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Staff-only signals that must never appear in a public response body.
FORBIDDEN = ("margin_pct", "velocity", "price_z", "bucket", "serialno", "batchid",
             "recunitprice", "cannbisproduct")


def inventory():
    return [
        live(product_id="1", name="Blue Dream 3.5g", price=25.0),
        live(product_id="2", name="OG Kush 3.5g", brand="Other", price=27.0),
        live(product_id="10", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="PR One", unit_grams=1.0, price=8.0),
        live(product_id="11", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="PR Two", unit_grams=1.0, price=9.0),
        live(product_id="20", cat_key="edibles", cat_label="Edibles", subcategory="10pk",
             name="Gummies", unit_grams=None, price=15.0),
    ]


class StorefrontTestCase(TestCase):
    """Shared setup + an inventory patch that covers every module that pulls it."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

    def _patch_inv(self, inv=None):
        return patch("bundles.cart.pos_catalog.get_inventory",
                     return_value=inv if inv is not None else inventory())

    def _add(self, product_id="1", qty=1, loc="yakima", inv=None):
        with self._patch_inv(inv):
            return self.client.post("/custom-order/cart/add",
                                    {"loc": loc, "product_id": product_id, "qty": qty})


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class MenuTests(StorefrontTestCase):
    def test_menu_renders_without_auth(self):
        with self._patch_inv():
            r = self.client.get("/custom-order/menu?loc=yakima")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Happy Time — Yakima", r.content.decode())

    def test_menu_sets_a_cart_cookie_for_retention(self):
        with self._patch_inv():
            r = self.client.get("/custom-order/menu?loc=yakima")
        self.assertIn(cart_mod.COOKIE, r.cookies)
        self.assertTrue(r.cookies[cart_mod.COOKIE]["httponly"])

    def test_results_returns_in_stock_products_only(self):
        inv = [live(product_id="1", name="Gone", qty=0), live(product_id="2", name="Here", qty=5)]
        with self._patch_inv(inv):
            r = self.client.get("/custom-order/results?loc=yakima&format=json")
        self.assertEqual([p["name"] for p in r.json()["products"]], ["Here"])

    def test_search_filter_applies(self):
        with self._patch_inv():
            r = self.client.get("/custom-order/results?loc=yakima&q=gummies&format=json")
        self.assertEqual([p["name"] for p in r.json()["products"]], ["Gummies"])

    def test_category_filter_applies(self):
        with self._patch_inv():
            r = self.client.get("/custom-order/results?loc=yakima&cat=pre-rolls&format=json")
        names = sorted(p["name"] for p in r.json()["products"])
        self.assertEqual(names, ["PR One", "PR Two"])

    def test_results_never_leak_staff_signals(self):
        with self._patch_inv():
            body = self.client.get("/custom-order/results?loc=yakima&format=json").content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body)

    def test_unknown_store_falls_back_rather_than_500(self):
        with self._patch_inv():
            r = self.client.get("/custom-order/menu?loc=mars")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Yakima", r.content.decode())


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class CartTests(StorefrontTestCase):
    def test_add_creates_an_open_draft_with_a_live_price(self):
        r = self._add("1")
        self.assertEqual(r.status_code, 200)
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.status, PhoneCartDraft.Status.OPEN)
        self.assertEqual(draft.source, PhoneCartDraft.Source.ONLINE)
        self.assertEqual(draft.lines[0]["unit_price"], 25.0)

    def test_adding_twice_increments_rather_than_duplicating(self):
        self._add("1")
        self._add("1")
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(len(draft.lines), 1)
        self.assertEqual(draft.lines[0]["quantity"], 2)

    def test_cart_survives_across_requests_via_the_cookie(self):
        self._add("1")
        self._add("10")
        with self._patch_inv():
            r = self.client.get("/custom-order/cart?loc=yakima")
        body = r.content.decode()
        self.assertIn("Blue Dream 3.5g", body)
        self.assertIn("PR One", body)
        self.assertEqual(PhoneCartDraft.objects.count(), 1)

    def test_out_of_stock_add_is_refused(self):
        r = self._add("999")
        self.assertIn("sold out", r.content.decode().lower())
        self.assertFalse(PhoneCartDraft.objects.get().lines)

    def test_update_quantity_and_remove(self):
        self._add("1")
        with self._patch_inv():
            self.client.post("/custom-order/cart/update", {"loc": "yakima", "product_id": "1", "qty": 3})
        self.assertEqual(PhoneCartDraft.objects.get().lines[0]["quantity"], 3)
        with self._patch_inv():
            self.client.post("/custom-order/cart/remove", {"loc": "yakima", "product_id": "1"})
        self.assertEqual(PhoneCartDraft.objects.get().lines, [])

    def test_quantity_is_clamped_to_max_then_to_available_stock(self):
        # MAX_QTY caps the request; reprice then caps to what's actually on the
        # floor (10 here), so we never promise more units than exist.
        self._add("1", qty=999)
        self.assertEqual(PhoneCartDraft.objects.get().lines[0]["quantity"], 10)

    def test_quantity_is_capped_at_max_when_stock_is_plentiful(self):
        plenty = [dict(p, qty=500) for p in inventory()]
        self._add("1", qty=999, inv=plenty)
        with self._patch_inv(plenty):
            self.client.get("/custom-order/cart?loc=yakima")
        self.assertEqual(PhoneCartDraft.objects.get().lines[0]["quantity"], cart_mod.MAX_QTY)

    def test_a_sold_out_line_is_flagged_not_silently_dropped(self):
        self._add("1")
        # Same cart, but that product is gone from the floor now.
        with self._patch_inv([p for p in inventory() if p["product_id"] != "1"]):
            r = self.client.get("/custom-order/cart?loc=yakima")
        body = r.content.decode()
        self.assertIn("Sold out", body)
        self.assertIn("Blue Dream 3.5g", body)   # still visible, not vanished

    def test_price_is_repriced_from_live_inventory_not_stored(self):
        self._add("1")
        cheaper = [dict(p, price=9.0) if p["product_id"] == "1" else p for p in inventory()]
        with self._patch_inv(cheaper):
            self.client.get("/custom-order/cart?loc=yakima")
        self.assertEqual(PhoneCartDraft.objects.get().lines[0]["unit_price"], 9.0)

    def test_carts_are_scoped_per_store(self):
        self._add("1", loc="yakima")
        with self._patch_inv():
            self.client.get("/custom-order/menu?loc=pullman")
        # A second, separate cart — a Yakima cart must not follow you to Pullman.
        self.assertEqual(PhoneCartDraft.objects.count(), 2)

    def test_cart_html_never_leaks(self):
        self._add("1")
        with self._patch_inv():
            body = self.client.get("/custom-order/cart?loc=yakima").content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body)


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2,
                   BUNDLE_MAX_ORDER_TOTAL=300)
class CheckoutTests(StorefrontTestCase):
    def _checkout(self, **over):
        payload = {"loc": "yakima", "name": "Sam Reyes", "phone": "509 555 1212",
                   "email": "sam@example.com"}
        payload.update(over)
        with self._patch_inv(over.pop("_inv", None)):
            return self.client.post("/custom-order/checkout", payload)

    def test_empty_cart_shows_the_empty_state(self):
        with self._patch_inv():
            r = self.client.get("/custom-order/checkout?loc=yakima")
        self.assertIn("cart is empty", r.content.decode().lower())

    def test_places_a_released_order_the_pos_can_claim(self):
        self._add("1")
        self._add("10", qty=2)
        r = self._checkout()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Order placed", r.content.decode())
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.source, PhoneCartDraft.Source.ONLINE)
        self.assertEqual(draft.pickup_name, "Sam Reyes")
        self.assertEqual(draft.contact_phone, "5095551212")
        self.assertEqual(draft.contact_email, "sam@example.com")
        self.assertEqual(draft.phone_last4, "1212")

    def test_email_is_optional(self):
        self._add("1")
        r = self._checkout(email="")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PhoneCartDraft.objects.get().contact_email, "")

    def test_only_phone_is_required(self):
        # Name became optional: the phone IS the identity, same as a phone order.
        self._add("1")
        self.assertEqual(self._checkout(phone="123").status_code, 400)
        self.assertEqual(PhoneCartDraft.objects.get().status, PhoneCartDraft.Status.OPEN)

    def test_bad_email_is_rejected(self):
        self._add("1")
        r = self._checkout(email="not-an-email")
        self.assertEqual(r.status_code, 400)

    def test_phone_is_normalised_to_ten_digits(self):
        self._add("1")
        self._checkout(phone="+1 (509) 555-1212")
        self.assertEqual(PhoneCartDraft.objects.get().contact_phone, "5095551212")

    def test_total_is_computed_server_side_from_live_prices(self):
        self._add("1")           # 25.00
        self._add("10", qty=2)   # 8.00 x2
        self._checkout()
        self.assertEqual(PhoneCartDraft.objects.get().quote["total"], 41.0)

    def test_order_over_the_cap_is_refused(self):
        self._add("1", qty=12)   # 25 x12 = 300
        self._add("2", qty=12)   # +27 x12 -> well over
        r = self._checkout()
        self.assertEqual(r.status_code, 400)
        self.assertIn("capped", r.content.decode())
        self.assertEqual(PhoneCartDraft.objects.get().status, PhoneCartDraft.Status.OPEN)

    def test_checkout_blocked_while_a_line_is_sold_out(self):
        self._add("1")
        gone = [p for p in inventory() if p["product_id"] != "1"]
        with self._patch_inv(gone):
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "Sam", "phone": "5095551212"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PhoneCartDraft.objects.get().status, PhoneCartDraft.Status.OPEN)

    def test_cookie_is_cleared_so_a_refresh_starts_a_new_cart(self):
        self._add("1")
        r = self._checkout()
        self.assertEqual(r.cookies[cart_mod.COOKIE].value, "")

    def test_it_never_writes_a_dutchie_order(self):
        self._add("1")
        with patch("dutchie.pos_register_client.PosRegisterClient") as client:
            self._checkout()
            client.assert_not_called()

    def test_success_page_never_leaks(self):
        self._add("1")
        body = self._checkout().content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body)


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class OrderCustomerWiringTests(StorefrontTestCase):
    """An order that reaches the register with no customer is a dead end."""

    def _place(self, guest_rows=None, fail=False):
        self._add("1")
        client = MagicMock()
        if fail:
            client.guest_search.side_effect = RuntimeError("dutchie down")
        else:
            client.guest_search.return_value = {"Data": guest_rows or []}
        with patch("bundles.customers._client", return_value=client), self._patch_inv():
            self.client.post("/custom-order/checkout",
                             {"loc": "yakima", "name": "Sam Reyes", "phone": "509 555 1212"})
        return PhoneCartDraft.objects.get()

    def test_existing_account_is_matched_and_stamped(self):
        draft = self._place([{"Guest_id": 4242, "Name": "Sam Reyes", "PhoneNo": "(509) 555-1212"}])
        self.assertEqual(draft.dutchie_acct_id, "4242")
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.MATCHED)

    def test_no_account_is_flagged_for_creation_at_claim(self):
        draft = self._place([])
        self.assertEqual(draft.dutchie_acct_id, "")
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.NEW)

    def test_lookup_outage_does_not_block_the_order(self):
        # A Dutchie outage must never stop someone placing an order.
        draft = self._place(fail=True)
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.UNRESOLVED)

    def test_checkout_never_creates_a_guest_from_the_public_endpoint(self):
        client = MagicMock()
        client.guest_search.return_value = {"Data": []}
        self._add("1")
        with patch("bundles.customers._client", return_value=client), self._patch_inv():
            self.client.post("/custom-order/checkout",
                             {"loc": "yakima", "name": "Sam Reyes", "phone": "5095551212"})
        client.create_guest.assert_not_called()


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class BundleLandingTests(StorefrontTestCase):
    def setUp(self):
        super().setUp()
        Product.objects.create(sku="A", product_id="1", location_slug="yakima",
                               name="Blue Dream 3.5g", brand="Acme", category="flower",
                               subcategory="3.5g", unit_weight=3.5, price=25,
                               cost=10, margin=15, quantity_on_hand=10)

    def _url(self, **kw):
        kw.setdefault("bundle", "roll-relax")
        kw.setdefault("store", "yakima")
        kw.setdefault("items", [("1", 1), ("10", 2), ("20", 1)])
        return signing.build_url("/custom-order/", **kw)

    def _get(self, inv=None, **kw):
        with self._patch_inv(inv):
            return self.client.get(self._url(**kw))

    def test_renders_and_seeds_the_cart(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Roll &amp; Relax Bundle", r.content.decode())
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(len(draft.lines), 3)
        self.assertEqual(draft.bundle_slug, "roll-relax")

    def test_reopening_the_email_does_not_clobber_an_existing_cart(self):
        self._add("2")                      # shopper's own pick
        self._get()
        draft = PhoneCartDraft.objects.get()
        self.assertEqual([line["product_id"] for line in draft.lines], ["2"])

    def test_bundle_discount_is_carried_into_the_order_for_staff(self):
        self._get()
        with self._patch_inv():
            self.client.post("/custom-order/checkout",
                             {"loc": "yakima", "name": "Sam", "phone": "5095551212"})
        quote = PhoneCartDraft.objects.get().quote
        self.assertEqual(quote["bundle_discount_pct"], 20)
        self.assertEqual(quote["bundle_name"], "Roll & Relax Bundle")

    def test_bad_signature_renders_a_helpful_page(self):
        with self._patch_inv():
            r = self.client.get(self._url().replace("b=roll-relax", "b=weekend"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("This link didn't open", r.content.decode())

    def test_unknown_bundle_is_404(self):
        with self._patch_inv():
            self.assertEqual(self.client.get(self._url(bundle="nope")).status_code, 404)

    def test_expired_link_still_renders(self):
        r = self._get(ttl_days=-1)
        self.assertEqual(r.status_code, 200)
        self.assertIn("offer has ended", r.content.decode())

    def test_sold_out_bundle_item_is_substituted(self):
        inv = [p for p in inventory() if p["product_id"] != "1"]
        r = self._get(inv=inv)
        self.assertIn("OG Kush 3.5g", r.content.decode())

    def test_landing_never_leaks(self):
        body = self._get().content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body)


@override_settings(CACHES=CACHES_LOCMEM)
class StoreKeyTranslationTests(TestCase):
    """Mount Vernon is the one store whose POS key and location_slug differ."""

    def test_round_trip(self):
        from dutchie import stores
        self.assertEqual(stores.location_slug("mtvernon"), "mount-vernon")
        self.assertEqual(stores.store_key("mount-vernon"), "mtvernon")
        for key in ("yakima", "pullman"):
            self.assertEqual(stores.location_slug(key), key)
            self.assertEqual(stores.store_key(key), key)

    def test_pos_queue_finds_a_mount_vernon_order(self):
        # Regression: the queue filtered location_slug by the POS store key, so a
        # Mount Vernon order was invisible to the Mount Vernon register.
        from pos.views import _phone_cart_queue
        PhoneCartDraft.objects.create(location_slug="mount-vernon",
                                      status=PhoneCartDraft.Status.RELEASED,
                                      pickup_name="Sam", lines=[{"product_id": "1"}])
        self.assertEqual(len(_phone_cart_queue("mtvernon")), 1)

    def test_open_carts_are_not_claimable(self):
        # An `open` row is a shopper still browsing; loading it at the register
        # would create a phantom order nobody placed.
        PhoneCartDraft.objects.create(location_slug="yakima",
                                      status=PhoneCartDraft.Status.OPEN,
                                      lines=[{"product_id": "1"}])
        from pos.views import _phone_cart_queue
        self.assertEqual(_phone_cart_queue("yakima"), [])
