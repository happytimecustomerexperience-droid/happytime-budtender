"""Cart edge cases: lifecycle, clamping, reprice, seeding, and the degraded paths.

`bundles/cart.py` is the only place a shopper's intent is stored, and it is stored
in a cookie-keyed `PhoneCartDraft` that may sit untouched for 30 days. So the
interesting cases are not "add works" — `test_views.py` covers the happy paths —
they are what happens when the world moves underneath a cart that already exists:
the price changed, the item sold out, the store toggled, the register is down.

Two invariants this file is guarding:
  * the client never sets a price — every render re-reads live inventory
  * nothing is ever silently dropped; a gone item is FLAGGED so the shopper sees it

Where the code's actual behaviour is surprising (quantity clamping is asymmetric,
the bundle discount is carried as a percentage rather than applied to the quote
total), the test asserts what the code really does and says so in a comment.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings

from budtender.models import PhoneCartDraft
from bundles import cart as cart_mod
from bundles import resolver
from bundles.catalog import get_bundle
from bundles.tests.test_resolver import live

SECRET = "unit-test-secret-value"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def inventory():
    """The floor. Every row carries a live qty of 10 unless a test says otherwise."""
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


class CartTestCase(TestCase):
    """LocMemCache is process-global — clear it on both ends or primed rate-limit
    buckets and warmed inventory leak into whatever class runs next."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()
        self.rf = RequestFactory()

    def _patch_inv(self, inv=None):
        return patch("bundles.cart.pos_catalog.get_inventory",
                     return_value=inventory() if inv is None else inv)

    def _patch_inv_down(self):
        """The register pull itself blowing up, not merely returning nothing."""
        return patch("bundles.cart.pos_catalog.get_inventory",
                     side_effect=RuntimeError("product_SearchV2 timed out"))

    def _add(self, product_id="1", qty=1, loc="yakima", inv=None):
        with self._patch_inv(inv):
            return self.client.post("/custom-order/cart/add",
                                    {"loc": loc, "product_id": product_id, "qty": qty})

    def _draft(self, loc="yakima", **kw):
        kw.setdefault("status", PhoneCartDraft.Status.OPEN)
        return PhoneCartDraft.objects.create(
            location_slug=loc, source=PhoneCartDraft.Source.ONLINE,
            session_token="online", **kw)

    def _request(self, token=None, path="/custom-order/cart"):
        request = self.rf.get(path)
        if token:
            request.COOKIES[cart_mod.COOKIE] = token
        return request


# ── 1. lifecycle ─────────────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class CartLifecycleTests(CartTestCase):
    def test_a_fresh_visitor_has_no_cart_when_create_is_false(self):
        self.assertIsNone(cart_mod.get_cart(self._request(), "yakima"))
        self.assertEqual(PhoneCartDraft.objects.count(), 0)   # and nothing was written

    def test_an_unknown_cookie_with_create_false_is_still_no_cart(self):
        self.assertIsNone(cart_mod.get_cart(self._request(token="not-a-real-token"), "yakima"))
        self.assertEqual(PhoneCartDraft.objects.count(), 0)

    def test_create_makes_exactly_one_open_online_draft(self):
        draft = cart_mod.get_cart(self._request(), "yakima", create=True)
        self.assertEqual(PhoneCartDraft.objects.count(), 1)
        self.assertEqual(draft.status, PhoneCartDraft.Status.OPEN)
        self.assertEqual(draft.source, PhoneCartDraft.Source.ONLINE)
        self.assertEqual(draft.location_slug, "yakima")
        self.assertTrue(draft.draft_token)
        self.assertIsNotNone(draft.expires_at)   # abandoned carts must not live forever

    def test_calling_get_cart_twice_with_the_cookie_returns_the_same_cart(self):
        first = cart_mod.get_cart(self._request(), "yakima", create=True)
        second = cart_mod.get_cart(self._request(token=first.draft_token), "yakima", create=True)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PhoneCartDraft.objects.count(), 1)

    def test_two_requests_in_a_row_share_one_cart_over_http(self):
        # The cookie is what makes this true; the view attaches it on every response.
        with self._patch_inv():
            self.client.get("/custom-order/cart?loc=yakima")
            self.client.get("/custom-order/cart?loc=yakima")
        self.assertEqual(PhoneCartDraft.objects.count(), 1)

    def test_a_released_order_is_never_handed_back_as_an_open_cart(self):
        draft = self._draft(status=PhoneCartDraft.Status.RELEASED, lines=[{"product_id": "1"}])
        self.assertIsNone(cart_mod.get_cart(self._request(token=draft.draft_token), "yakima"))

    def test_a_cart_is_per_cookie_and_store_so_lines_do_not_cross_stores(self):
        self._add("1", loc="yakima")
        with self._patch_inv():
            body = self.client.get("/custom-order/cart?loc=pullman").content.decode()
        self.assertIn("Your cart is empty", body)
        self.assertNotIn("Blue Dream", body)
        yakima = PhoneCartDraft.objects.get(location_slug="yakima")
        self.assertEqual([x["product_id"] for x in yakima.lines], ["1"])
        self.assertEqual(PhoneCartDraft.objects.count(), 2)

    def test_the_same_cookie_at_another_store_gets_a_different_draft(self):
        draft = cart_mod.get_cart(self._request(), "yakima", create=True)
        other = cart_mod.get_cart(self._request(token=draft.draft_token), "pullman", create=True)
        self.assertNotEqual(draft.pk, other.pk)
        self.assertEqual(other.location_slug, "pullman")

    def test_switching_store_and_back_orphans_the_original_cart(self):
        # BUG (reported): the cookie holds ONE token, so the second store's cart
        # overwrites it. Coming back to Yakima can no longer find the Yakima cart —
        # it silently builds a third one and the shopper's items are gone.
        self._add("1", loc="yakima")
        original = PhoneCartDraft.objects.get(location_slug="yakima")
        with self._patch_inv():
            self.client.get("/custom-order/menu?loc=pullman")
            body = self.client.get("/custom-order/cart?loc=yakima").content.decode()
        self.assertNotIn("Blue Dream", body)                     # items are not shown
        self.assertIn("Your cart is empty", body)
        self.assertEqual(PhoneCartDraft.objects.filter(location_slug="yakima").count(), 2)
        original.refresh_from_db()
        self.assertEqual(len(original.lines), 1)                 # still on disk, unreachable

    def test_every_cookieless_visit_writes_a_new_draft_row(self):
        # BUG (reported): the public GETs create=True unconditionally and carry no
        # rate limit, so a crawler mints one PhoneCartDraft per request.
        for _ in range(4):
            self.client.cookies.clear()
            with self._patch_inv():
                self.client.get("/custom-order/menu?loc=yakima")
        self.assertEqual(PhoneCartDraft.objects.count(), 4)


# ── 2. add() ─────────────────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class CartAddTests(CartTestCase):
    def setUp(self):
        super().setUp()
        self.draft = self._draft()
        self.inv = inventory()

    def test_a_new_line_is_priced_from_live_inventory(self):
        ok, err = cart_mod.add(self.draft, "1", 1, inventory=self.inv)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        line = self.draft.lines[0]
        self.assertEqual(line["product_id"], "1")
        self.assertEqual(line["unit_price"], 25.0)
        self.assertEqual(line["quantity"], 1)
        self.assertEqual(line["line_total"], 25.0)
        self.assertEqual(line["stock_on_hand"], 10)
        # Was a flat "live_register" on every line whatever the truth. It now records
        # WHICH source priced it: "price_check" for a per-serial confirmation against
        # the register, "menu_snapshot" for the ~8-minute browse cache. Under test the
        # register is unreachable by design, so the snapshot is the honest answer.
        self.assertEqual(line["quote_source"], "menu_snapshot")

    def test_adding_the_same_product_increments_one_line(self):
        cart_mod.add(self.draft, "1", 1, inventory=self.inv)
        cart_mod.add(self.draft, "1", 3, inventory=self.inv)
        self.assertEqual(len(self.draft.lines), 1)
        self.assertEqual(self.draft.lines[0]["quantity"], 4)

    def test_a_new_line_is_clamped_up_to_max_qty(self):
        # MAX_QTY only binds when the shelf is deeper than it; stock is the other cap.
        deep = [dict(p, qty=99) if p["product_id"] == "1" else p for p in self.inv]
        cart_mod.add(self.draft, "1", 999, inventory=deep)
        self.assertEqual(self.draft.lines[0]["quantity"], cart_mod.MAX_QTY)

    def test_a_new_line_is_also_clamped_to_what_is_on_the_shelf(self):
        # The reservation rule: a cart cannot hold more than exists. Before this,
        # twenty shoppers each held a product with two units on hand.
        three_left = [dict(p, qty=3) if p["product_id"] == "1" else p for p in self.inv]
        cart_mod.add(self.draft, "1", 999, inventory=three_left)
        self.assertEqual(self.draft.lines[0]["quantity"], 3)

    def test_an_increment_past_max_qty_is_clamped_too(self):
        deep = [dict(p, qty=99) if p["product_id"] == "1" else p for p in self.inv]
        cart_mod.add(self.draft, "1", cart_mod.MAX_QTY, inventory=deep)
        cart_mod.add(self.draft, "1", 5, inventory=deep)
        self.assertEqual(self.draft.lines[0]["quantity"], cart_mod.MAX_QTY)

    def test_a_new_line_is_clamped_up_from_zero_to_one(self):
        cart_mod.add(self.draft, "1", 0, inventory=self.inv)
        self.assertEqual(self.draft.lines[0]["quantity"], 1)

    def test_a_negative_increment_is_not_clamped_at_the_bottom(self):
        # BUG (reported): the new-line path clamps with max(qty, 1), the increment
        # path does not — so add(-5) drives a stored quantity NEGATIVE. Not reachable
        # from the web (the view floors qty at 1), but cart.add() is the API other
        # callers use. reprice() launders it back to 1 on the next render, which is
        # exactly why nobody has noticed.
        cart_mod.add(self.draft, "1", 1, inventory=self.inv)
        cart_mod.add(self.draft, "1", -5, inventory=self.inv)
        self.assertEqual(self.draft.lines[0]["quantity"], -4)
        ctx = cart_mod.reprice(self.draft, self.inv)
        self.assertEqual(ctx["lines"][0]["quantity"], 1)

    def test_the_view_floors_a_negative_quantity_before_it_reaches_the_cart(self):
        self._add("1", qty=1)
        self._add("1", qty=-5)
        # `self.draft` is the unit-level fixture; the HTTP path built its own.
        over_http = PhoneCartDraft.objects.exclude(pk=self.draft.pk).get()
        self.assertEqual(over_http.lines[0]["quantity"], 2)

    def test_a_product_that_is_not_on_the_floor_is_refused(self):
        ok, err = cart_mod.add(self.draft, "does-not-exist", 1, inventory=self.inv)
        self.assertFalse(ok)
        self.assertEqual(err, "not_in_stock")
        self.assertEqual(self.draft.lines, [])

    def test_a_product_below_min_stock_is_refused(self):
        # The owner's rule: exactly one unit left is one walk-in from being gone.
        scarce = [dict(p, qty=resolver.MIN_STOCK - 1) if p["product_id"] == "1" else p
                  for p in self.inv]
        ok, err = cart_mod.add(self.draft, "1", 1, inventory=scarce)
        self.assertFalse(ok)
        self.assertEqual(err, "not_in_stock")

    def test_min_stock_exactly_is_addable(self):
        edge = [dict(p, qty=resolver.MIN_STOCK) if p["product_id"] == "1" else p
                for p in self.inv]
        self.assertEqual(cart_mod.add(self.draft, "1", 1, inventory=edge), (True, ""))

    def test_an_empty_floor_refuses_everything(self):
        self.assertEqual(cart_mod.add(self.draft, "1", 1, inventory=[]), (False, "not_in_stock"))

    def test_adding_more_than_the_live_quantity_is_capped_at_add_time(self):
        # WAS: add() ignored stock and reprice() capped later, which meant the cart
        # showed 10 of something with 3 on the shelf until the next render — and,
        # worse, nothing stopped the next shopper being promised the same 3. Stock is
        # now reserved as it enters a cart, so the cap moves to add().
        three_left = [dict(p, qty=3) if p["product_id"] == "1" else p for p in self.inv]
        ok, _ = cart_mod.add(self.draft, "1", 10, inventory=three_left)
        self.assertTrue(ok)
        self.assertEqual(self.draft.lines[0]["quantity"], 3)
        ctx = cart_mod.reprice(self.draft, three_left)
        self.assertEqual(ctx["lines"][0]["quantity"], 3)
        self.assertEqual(ctx["issues"], 0, "a cart already within stock is not an issue")

    def test_the_cart_is_capped_at_max_lines(self):
        many = [live(product_id=str(100 + i), name=f"P{i}") for i in range(cart_mod.MAX_LINES + 2)]
        for i in range(cart_mod.MAX_LINES):
            self.assertTrue(cart_mod.add(self.draft, str(100 + i), 1, inventory=many)[0])
        ok, err = cart_mod.add(self.draft, str(100 + cart_mod.MAX_LINES), 1, inventory=many)
        self.assertFalse(ok)
        self.assertEqual(err, "cart_full")
        self.assertEqual(len(self.draft.lines), cart_mod.MAX_LINES)

    def test_a_full_cart_can_still_increment_a_line_it_already_holds(self):
        many = [live(product_id=str(100 + i), name=f"P{i}") for i in range(cart_mod.MAX_LINES)]
        for i in range(cart_mod.MAX_LINES):
            cart_mod.add(self.draft, str(100 + i), 1, inventory=many)
        self.assertTrue(cart_mod.add(self.draft, "100", 1, inventory=many)[0])
        self.assertEqual(self.draft.lines[0]["quantity"], 2)


# ── 3. set_qty() ─────────────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class CartSetQtyTests(CartTestCase):
    def setUp(self):
        super().setUp()
        self.draft = self._draft()
        self.inv = inventory()
        cart_mod.add(self.draft, "1", 2, inventory=self.inv)
        cart_mod.add(self.draft, "10", 1, inventory=self.inv)

    def test_zero_REMOVES_the_line_it_does_not_clamp_to_one(self):
        # Asserting the real behaviour: qty <= 0 deletes the line outright, so the
        # "-" button at 1 is the same gesture as Remove.
        cart_mod.set_qty(self.draft, "1", 0)
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["10"])

    def test_a_negative_quantity_also_removes_rather_than_going_negative(self):
        cart_mod.set_qty(self.draft, "1", -7)
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["10"])

    def test_a_huge_quantity_is_clamped_to_max_qty(self):
        cart_mod.set_qty(self.draft, "1", 10_000)
        self.assertEqual(self.draft.lines[0]["quantity"], cart_mod.MAX_QTY)

    def test_max_qty_is_still_capped_to_live_stock_on_the_next_render(self):
        cart_mod.set_qty(self.draft, "1", 10_000)
        ctx = cart_mod.reprice(self.draft, self.inv)          # only 10 on the floor
        self.assertEqual(ctx["lines"][0]["quantity"], 10)
        self.assertEqual(ctx["lines"][0]["issue"], "reduced")

    def test_setting_a_product_that_is_not_in_the_cart_is_a_no_op(self):
        cart_mod.set_qty(self.draft, "999", 5)
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["1", "10"])
        self.assertEqual(self.draft.lines[0]["quantity"], 2)

    def test_setting_an_absent_product_to_zero_is_also_a_no_op(self):
        cart_mod.set_qty(self.draft, "999", 0)
        self.assertEqual(len(self.draft.lines), 2)

    def test_set_qty_does_not_touch_the_stored_price(self):
        # The client never sets a price. Only reprice() may move unit_price.
        cart_mod.set_qty(self.draft, "1", 3)
        self.assertEqual(self.draft.lines[0]["unit_price"], 25.0)


# ── 4. remove() ──────────────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class CartRemoveTests(CartTestCase):
    def setUp(self):
        super().setUp()
        self.draft = self._draft()
        self.inv = inventory()

    def test_removing_an_existing_line_drops_only_that_line(self):
        cart_mod.add(self.draft, "1", 1, inventory=self.inv)
        cart_mod.add(self.draft, "10", 1, inventory=self.inv)
        cart_mod.remove(self.draft, "1")
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["10"])

    def test_removing_a_line_that_is_not_there_leaves_the_cart_alone(self):
        cart_mod.add(self.draft, "1", 1, inventory=self.inv)
        cart_mod.remove(self.draft, "nope")
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["1"])

    def test_removing_from_an_already_empty_cart_does_not_raise(self):
        cart_mod.remove(self.draft, "1")
        self.assertEqual(self.draft.lines, [])

    def test_removing_the_last_line_empties_the_cart_and_it_still_renders(self):
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/cart/remove", {"loc": "yakima", "product_id": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your cart is empty", r.content.decode())
        over_http = PhoneCartDraft.objects.exclude(pk=self.draft.pk).get()
        self.assertEqual(over_http.lines, [])

    def test_an_emptied_cart_reprices_to_a_zero_quote(self):
        cart_mod.add(self.draft, "1", 1, inventory=self.inv)
        cart_mod.remove(self.draft, "1")
        ctx = cart_mod.reprice(self.draft, self.inv)
        self.assertEqual(ctx["lines"], [])
        self.assertEqual(ctx["count"], 0)
        self.assertEqual(ctx["issues"], 0)
        self.assertEqual(ctx["quote"]["subtotal"], 0.0)
        self.assertEqual(ctx["quote"]["total"], 0.0)


# ── 5. reprice() — the heart ─────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class RepriceTests(CartTestCase):
    def setUp(self):
        super().setUp()
        self.draft = self._draft()
        self.inv = inventory()

    def _seed(self, *pairs):
        for pid, qty in pairs:
            cart_mod.add(self.draft, pid, qty, inventory=self.inv)

    # price movement ---------------------------------------------------------
    def test_a_price_that_moved_up_is_repriced_to_the_live_price(self):
        self._seed(("1", 2))
        dearer = [dict(p, price=31.5) if p["product_id"] == "1" else p for p in self.inv]
        ctx = cart_mod.reprice(self.draft, dearer)
        line = ctx["lines"][0]
        self.assertEqual(line["unit_price"], 31.5)
        self.assertEqual(line["line_total"], 63.0)
        self.assertEqual(ctx["quote"]["subtotal"], 63.0)
        self.assertEqual(ctx["quote"]["total"], 63.0)

    def test_a_price_that_moved_down_is_repriced_too_and_is_persisted(self):
        self._seed(("1", 1))
        cheaper = [dict(p, price=9.0) if p["product_id"] == "1" else p for p in self.inv]
        cart_mod.reprice(self.draft, cheaper)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.lines[0]["unit_price"], 9.0)
        self.assertEqual(self.draft.quote["total"], 9.0)

    def test_the_stored_price_is_never_trusted_even_if_the_line_is_tampered_with(self):
        self._seed(("1", 1))
        self.draft.lines[0]["unit_price"] = 0.01           # a forged client payload
        self.draft.lines[0]["line_total"] = 0.01
        self.draft.save(update_fields=["lines"])
        ctx = cart_mod.reprice(self.draft, self.inv)
        self.assertEqual(ctx["lines"][0]["unit_price"], 25.0)
        self.assertEqual(ctx["quote"]["total"], 25.0)

    def test_totals_recompute_across_several_lines(self):
        self._seed(("1", 1), ("10", 2), ("20", 1))          # 25 + 8*2 + 15
        ctx = cart_mod.reprice(self.draft, self.inv)
        self.assertEqual(ctx["quote"]["subtotal"], 56.0)
        self.assertEqual(ctx["quote"]["total"], 56.0)
        self.assertEqual(ctx["count"], 4)
        self.assertEqual(ctx["issues"], 0)
        self.assertEqual(round(sum(x["line_total"] for x in ctx["lines"]), 2), 56.0)

    def test_reprice_is_stable_when_nothing_changed(self):
        self._seed(("1", 1), ("10", 2))
        first = cart_mod.reprice(self.draft, self.inv)["quote"]["total"]
        second = cart_mod.reprice(self.draft, self.inv)["quote"]["total"]
        self.assertEqual(first, second)

    # sold out ---------------------------------------------------------------
    def test_a_line_that_vanished_from_the_floor_is_flagged_not_dropped(self):
        self._seed(("1", 1), ("10", 1))
        gone = [p for p in self.inv if p["product_id"] != "1"]
        ctx = cart_mod.reprice(self.draft, gone)
        dead = ctx["lines"][0]
        self.assertEqual(dead["product_id"], "1")
        self.assertEqual(dead["name"], "Blue Dream 3.5g")   # the shopper still sees WHAT is gone
        self.assertFalse(dead["in_stock"])
        self.assertEqual(dead["issue"], "sold_out")
        self.assertEqual(dead["line_total"], 0.0)
        self.assertEqual(ctx["issues"], 1)
        self.assertEqual(len(ctx["lines"]), 2)              # not deleted

    def test_a_sold_out_line_is_excluded_from_the_total_and_the_count(self):
        self._seed(("1", 3), ("10", 2))
        gone = [p for p in self.inv if p["product_id"] != "1"]
        ctx = cart_mod.reprice(self.draft, gone)
        self.assertEqual(ctx["quote"]["subtotal"], 16.0)    # 8 * 2 only
        self.assertEqual(ctx["count"], 2)

    def test_a_line_that_fell_below_min_stock_reads_as_sold_out(self):
        self._seed(("1", 1))
        one_left = [dict(p, qty=1) if p["product_id"] == "1" else p for p in self.inv]
        ctx = cart_mod.reprice(self.draft, one_left)
        self.assertEqual(ctx["lines"][0]["issue"], "sold_out")
        self.assertEqual(ctx["quote"]["total"], 0.0)

    def test_a_sold_out_line_is_never_repriced_to_the_new_price(self):
        # It must not quietly pick up a price it can't be sold at.
        self._seed(("1", 1))
        gone = [p for p in self.inv if p["product_id"] != "1"]
        ctx = cart_mod.reprice(self.draft, gone)
        self.assertEqual(ctx["lines"][0]["line_total"], 0.0)
        self.assertNotIn(ctx["lines"][0]["product_id"], [p["product_id"] for p in gone])

    def test_a_line_that_comes_back_in_stock_clears_the_flag(self):
        self._seed(("1", 2))
        gone = [p for p in self.inv if p["product_id"] != "1"]
        cart_mod.reprice(self.draft, gone)
        self.assertEqual(self.draft.lines[0]["issue"], "sold_out")
        ctx = cart_mod.reprice(self.draft, self.inv)         # restocked
        self.assertTrue(ctx["lines"][0]["in_stock"])
        self.assertNotIn("issue", ctx["lines"][0])
        self.assertEqual(ctx["issues"], 0)
        self.assertEqual(ctx["quote"]["total"], 50.0)

    # quantity vs live stock -------------------------------------------------
    def test_a_quantity_above_live_stock_is_clamped_and_flagged(self):
        self._seed(("1", 8))
        scarce = [dict(p, qty=3) if p["product_id"] == "1" else p for p in self.inv]
        ctx = cart_mod.reprice(self.draft, scarce)
        line = ctx["lines"][0]
        self.assertEqual(line["quantity"], 3)
        self.assertEqual(line["line_total"], 75.0)          # 25 * 3, not 25 * 8
        self.assertEqual(line["issue"], "reduced")
        self.assertTrue(line["in_stock"])
        self.assertEqual(ctx["issues"], 1)
        self.assertEqual(ctx["quote"]["total"], 75.0)

    def test_a_clamped_quantity_is_persisted_so_checkout_sees_the_truth(self):
        self._seed(("1", 8))
        scarce = [dict(p, qty=3) if p["product_id"] == "1" else p for p in self.inv]
        cart_mod.reprice(self.draft, scarce)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.lines[0]["quantity"], 3)

    def test_a_price_change_and_a_stock_cut_are_applied_together(self):
        self._seed(("1", 6), ("20", 1))
        moved = []
        for p in self.inv:
            if p["product_id"] == "1":
                moved.append(dict(p, price=30.0, qty=2))
            else:
                moved.append(p)
        ctx = cart_mod.reprice(self.draft, moved)
        self.assertEqual(ctx["lines"][0]["unit_price"], 30.0)
        self.assertEqual(ctx["lines"][0]["quantity"], 2)
        self.assertEqual(ctx["quote"]["subtotal"], 75.0)    # 30*2 + 15
        self.assertEqual(ctx["issues"], 1)

    def test_a_line_stored_as_junk_is_skipped_rather_than_crashing(self):
        self._seed(("1", 1))
        self.draft.lines = [self.draft.lines[0], "not-a-dict", None]
        self.draft.save(update_fields=["lines"])
        ctx = cart_mod.reprice(self.draft, self.inv)
        self.assertEqual(len(ctx["lines"]), 1)

    # bundle discount --------------------------------------------------------
    def test_the_bundle_discount_survives_a_reprice_as_a_percentage(self):
        # NOTE the real contract: reprice() carries the bundle's PERCENTAGE forward
        # and leaves `total == subtotal`. The discount is applied at the register
        # (the cart template renders it as "applied in store"), so a repriced cart
        # can never quote a stale dollar discount against a new subtotal.
        self._seed(("1", 1), ("10", 2), ("20", 1))
        self.draft.bundle_slug = "roll-relax"
        self.draft.save(update_fields=["bundle_slug"])
        ctx = cart_mod.reprice(self.draft, self.inv)
        quote = ctx["quote"]
        self.assertEqual(quote["bundle"], "roll-relax")
        self.assertEqual(quote["bundle_name"], "Roll & Relax Bundle")
        self.assertEqual(quote["bundle_discount_pct"], 20)
        self.assertEqual(quote["subtotal"], 56.0)
        self.assertEqual(quote["discounts"], 0.0)
        self.assertEqual(quote["total"], 56.0)
        # The math the register will do still lands on the resolver's number.
        self.assertEqual(round(quote["subtotal"] * (1 - quote["bundle_discount_pct"] / 100), 2),
                         44.8)

    def test_the_discount_pct_tracks_the_new_subtotal_after_a_price_change(self):
        self._seed(("1", 1), ("10", 2), ("20", 1))
        self.draft.bundle_slug = "roll-relax"
        self.draft.save(update_fields=["bundle_slug"])
        dearer = [dict(p, price=35.0) if p["product_id"] == "1" else p for p in self.inv]
        quote = cart_mod.reprice(self.draft, dearer)["quote"]
        self.assertEqual(quote["subtotal"], 66.0)           # 35 + 16 + 15
        self.assertEqual(quote["bundle_discount_pct"], 20)
        self.assertEqual(round(quote["subtotal"] * 0.8, 2), 52.8)

    def test_the_discount_survives_even_when_a_bundle_item_sold_out(self):
        self._seed(("1", 1), ("10", 2), ("20", 1))
        self.draft.bundle_slug = "roll-relax"
        self.draft.save(update_fields=["bundle_slug"])
        gone = [p for p in self.inv if p["product_id"] != "1"]
        quote = cart_mod.reprice(self.draft, gone)["quote"]
        self.assertEqual(quote["bundle_discount_pct"], 20)
        self.assertEqual(quote["subtotal"], 31.0)           # the dead line contributes 0
        self.assertEqual(quote["total"], 31.0)

    def test_an_unknown_bundle_slug_does_not_break_the_quote(self):
        self._seed(("1", 1))
        self.draft.bundle_slug = "no-such-bundle"
        self.draft.save(update_fields=["bundle_slug"])
        quote = cart_mod.reprice(self.draft, self.inv)["quote"]
        self.assertNotIn("bundle_discount_pct", quote)
        self.assertEqual(quote["total"], 25.0)

    def test_the_quote_never_leaks_a_staff_signal(self):
        self._seed(("1", 1))
        ctx = cart_mod.reprice(self.draft, self.inv)
        for leaked in ("margin_pct", "velocity", "price_z", "bucket", "SerialNo",
                       "RecUnitPrice", "BatchId"):
            self.assertNotIn(leaked, ctx["lines"][0])


# ── 6. seed_from_bundle() ────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class SeedFromBundleTests(CartTestCase):
    def setUp(self):
        super().setUp()
        self.draft = self._draft()
        self.inv = inventory()
        self.bundle = get_bundle("roll-relax")

    def _resolved(self, inv=None, items=None):
        return resolver.resolve(self.bundle, "yakima",
                                items or [("1", 1), ("10", 2), ("20", 1)],
                                inventory=self.inv if inv is None else inv)

    def test_an_empty_cart_is_seeded_with_the_resolved_lines(self):
        cart_mod.seed_from_bundle(self.draft, self._resolved(), "roll-relax")
        self.draft.refresh_from_db()
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["1", "10", "20"])
        self.assertEqual([x["quantity"] for x in self.draft.lines], [1, 2, 1])
        self.assertEqual(self.draft.lines[0]["unit_price"], 25.0)
        self.assertEqual(self.draft.lines[1]["line_total"], 16.0)

    def test_the_bundle_slug_is_carried_so_the_discount_survives(self):
        cart_mod.seed_from_bundle(self.draft, self._resolved(), "roll-relax")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.bundle_slug, "roll-relax")
        quote = cart_mod.reprice(self.draft, self.inv)["quote"]
        self.assertEqual(quote["bundle_discount_pct"], 20)
        self.assertEqual(quote["bundle_name"], "Roll & Relax Bundle")

    def test_a_cart_that_already_has_lines_is_never_double_seeded(self):
        cart_mod.add(self.draft, "2", 1, inventory=self.inv)   # the shopper's own pick
        cart_mod.seed_from_bundle(self.draft, self._resolved(), "roll-relax")
        self.draft.refresh_from_db()
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["2"])

    def test_seeding_twice_in_a_row_does_not_duplicate_lines(self):
        cart_mod.seed_from_bundle(self.draft, self._resolved(), "roll-relax")
        cart_mod.seed_from_bundle(self.draft, self._resolved(), "roll-relax")
        self.draft.refresh_from_db()
        self.assertEqual(len(self.draft.lines), 3)

    def test_a_pre_existing_cart_still_claims_the_bundle(self):
        # Was a bug: the early return happened BEFORE bundle_slug was stamped, so a
        # shopper who added one item before opening the email kept their cart and
        # silently lost the 20% — the landing page advertised it, the order didn't
        # carry it, and the budtender was never told which discount to apply.
        # Their existing lines are still theirs; only the bundle claim is added.
        cart_mod.add(self.draft, "2", 1, inventory=self.inv)
        cart_mod.seed_from_bundle(self.draft, self._resolved(), "roll-relax")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.bundle_slug, "roll-relax")
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["2"],
                         "seeding must not overwrite a cart the shopper already built")
        self.assertEqual(
            cart_mod.reprice(self.draft, self.inv)["quote"]["bundle_discount_pct"], 20)

    def test_unavailable_slots_are_skipped_rather_than_seeded_as_empty_lines(self):
        only_flower = [p for p in self.inv if p["cat_key"] == "flower"]
        resolved = self._resolved(inv=only_flower)
        self.assertGreaterEqual(resolved["missing"], 1)
        cart_mod.seed_from_bundle(self.draft, resolved, "roll-relax")
        self.draft.refresh_from_db()
        self.assertTrue(all(x["product_id"] for x in self.draft.lines))
        self.assertEqual([x["product_id"] for x in self.draft.lines], ["1"])

    def test_nothing_resolvable_seeds_nothing_but_still_records_the_bundle(self):
        # The shopper genuinely arrived from this bundle even if the floor could
        # fill none of it, and they may go on to add items by hand. Recording the
        # slug keeps the "mention your Roll & Relax" line alive for the counter;
        # the register still decides whether the basket actually qualifies.
        resolved = self._resolved(inv=[])
        cart_mod.seed_from_bundle(self.draft, resolved, "roll-relax")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.lines, [])
        self.assertEqual(self.draft.bundle_slug, "roll-relax")

    def test_a_substituted_line_is_what_gets_seeded_not_the_dead_sku(self):
        gone = [p for p in self.inv if p["product_id"] != "1"]
        resolved = self._resolved(inv=gone)
        cart_mod.seed_from_bundle(self.draft, resolved, "roll-relax")
        self.draft.refresh_from_db()
        seeded = [x["product_id"] for x in self.draft.lines]
        self.assertNotIn("1", seeded)
        self.assertIn("2", seeded)                          # the same-size stand-in

    def test_a_seeded_cart_reprices_against_live_stock_like_any_other(self):
        cart_mod.seed_from_bundle(self.draft, self._resolved(), "roll-relax")
        gone = [p for p in self.inv if p["product_id"] != "20"]
        ctx = cart_mod.reprice(self.draft, gone)
        self.assertEqual(ctx["issues"], 1)
        self.assertEqual(ctx["quote"]["subtotal"], 41.0)    # 25 + 16, the edible is dead


# ── 7. empty / degraded ──────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class EmptyAndDegradedTests(CartTestCase):
    def test_an_empty_cart_renders(self):
        with self._patch_inv():
            r = self.client.get("/custom-order/cart?loc=yakima")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your cart is empty", r.content.decode())

    def test_an_empty_cart_reprices_to_an_all_zero_context(self):
        ctx = cart_mod.reprice(self._draft(), inventory())
        self.assertEqual(ctx["lines"], [])
        self.assertEqual(ctx["count"], 0)
        self.assertEqual(ctx["issues"], 0)
        self.assertEqual(ctx["quote"]["total"], 0.0)
        self.assertTrue(ctx["inventory_live"])

    def test_inventory_for_swallows_a_register_outage(self):
        with self._patch_inv_down():
            self.assertEqual(cart_mod.inventory_for("yakima"), [])

    def test_reprice_marks_the_quote_unavailable_when_the_register_is_down(self):
        draft = self._draft()
        cart_mod.add(draft, "1", 2, inventory=inventory())
        with self._patch_inv_down():
            ctx = cart_mod.reprice(draft)               # no inventory passed -> live path
        self.assertFalse(ctx["inventory_live"])
        self.assertEqual(ctx["quote"]["source"], "unavailable")
        self.assertEqual(ctx["quote"]["total"], 0.0)
        self.assertEqual(ctx["lines"][0]["issue"], "sold_out")
        self.assertEqual(ctx["lines"][0]["name"], "Blue Dream 3.5g")   # still shown

    def test_the_cart_page_does_not_500_when_the_register_is_down(self):
        self._add("1")
        with self._patch_inv_down():
            r = self.client.get("/custom-order/cart?loc=yakima")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Blue Dream 3.5g", body)
        self.assertIn("Sold out", body)

    def test_an_empty_cart_page_does_not_500_when_the_register_is_down(self):
        with self._patch_inv_down():
            r = self.client.get("/custom-order/cart?loc=yakima")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your cart is empty", r.content.decode())

    def test_the_menu_still_renders_when_the_register_is_down(self):
        with self._patch_inv_down():
            r = self.client.get("/custom-order/menu?loc=yakima")
        self.assertEqual(r.status_code, 200)

    def test_adding_while_the_register_is_down_is_refused_not_crashed(self):
        with self._patch_inv_down():
            r = self.client.post("/custom-order/cart/add",
                                 {"loc": "yakima", "product_id": "1", "qty": 1})
        self.assertEqual(r.status_code, 200)
        self.assertIn("sold out", r.content.decode().lower())
        self.assertEqual(PhoneCartDraft.objects.get().lines, [])

    def test_checkout_does_not_500_when_the_register_is_down(self):
        self._add("1")
        with self._patch_inv_down():
            r = self.client.get("/custom-order/checkout?loc=yakima")
        self.assertEqual(r.status_code, 200)

    def test_an_outage_can_never_place_an_order(self):
        self._add("1")
        with self._patch_inv_down():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "first_name": "Sam", "last_name": "Reyes", "phone": "5095551212"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PhoneCartDraft.objects.get().status, PhoneCartDraft.Status.OPEN)

    def test_no_cart_path_ever_talks_to_dutchie(self):
        with patch("dutchie.pos_register_client.PosRegisterClient") as register:
            self._add("1")
            with self._patch_inv():
                self.client.get("/custom-order/cart?loc=yakima")
                self.client.post("/custom-order/cart/update",
                                 {"loc": "yakima", "product_id": "1", "qty": 2})
                self.client.post("/custom-order/cart/remove", {"loc": "yakima", "product_id": "1"})
            register.assert_not_called()
