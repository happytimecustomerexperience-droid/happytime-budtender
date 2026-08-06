"""The whole pipeline, one shopper, start to finish.

Every other test file checks a unit or a route. This one walks the actual journey
a customer takes and asserts the handoffs between stages — where things really
break. Emailed link -> landing -> menu -> filter -> cart edits -> checkout ->
released order -> budtender claims it in the POS.

The seams this exists to protect:
  * the emailed bundle seeds the cart, so "add bundle to cart" is genuinely one tap
  * the bundle discount survives every cart edit and lands on the order
  * money is decided server-side at every stage; a tampered client value never wins
  * the order a budtender claims is the order the shopper saw

Test customer: Sam Reyes, 509 420 6999.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from budtender.models import PhoneCartDraft, Product
from bundles import cart as cart_mod
from bundles import signing
from bundles.catalog import get_bundle
from bundles.tests.test_resolver import live

SECRET = "unit-test-secret-value"
LOC = "yakima"
PHONE = "5094206999"
SHOPPER = "Sam Reyes"

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def inventory():
    """A believable Yakima floor: two 3.5g flowers, two pre-rolls, an edible, a cart."""
    return [
        live(product_id="1", name="Blue Dream 3.5g", brand="Athenry", price=25.0,
             strain="Blue Dream", strain_type="hybrid", thc=22.0, qty=10),
        live(product_id="2", name="Sticky Frog OG 3.5g", brand="Sticky Frog", price=27.0,
             strain="OG Kush", strain_type="indica", thc=26.4, qty=8),
        live(product_id="10", cat_key="pre-rolls", cat_label="Pre-Rolls", raw_category="Pre-Roll",
             subcategory="1pk", name="Phat Panda Infused PR 1pk", brand="Phat Panda",
             unit_grams=1.0, price=12.0, strain_type="hybrid", thc=38.1, qty=20),
        live(product_id="11", cat_key="pre-rolls", cat_label="Pre-Rolls", raw_category="Pre-Roll",
             subcategory="1pk", name="Doja Single 1pk", brand="Doja", unit_grams=1.0,
             price=9.0, strain_type="sativa", thc=31.0, qty=15),
        live(product_id="20", cat_key="edibles", cat_label="Edibles", raw_category="Edible",
             subcategory="10pk", name="Marmas Sour Cherry 10pk", brand="Marmas",
             unit_grams=None, price=15.0, qty=12),
        live(product_id="30", cat_key="vapes", cat_label="Vapes", raw_category="Vape Cartridge",
             subcategory="1g", name="Crystal Clear LR Cart 1g", brand="Crystal Clear",
             unit_grams=1.0, price=26.0, strain_type="indica", thc=84.2, qty=9),
    ]


def patch_inventory(inv=None):
    """Every module reads live stock through cart_mod.inventory_for."""
    return patch("bundles.cart.pos_catalog.get_inventory",
                 return_value=inv if inv is not None else inventory())


@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class FullJourneyTests(TestCase):
    """One shopper, one continuous session, every stage in order."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()
        # The identity the email advertised. Stock/price never come from here.
        Product.objects.create(sku="A", product_id="1", location_slug=LOC,
                               name="Blue Dream 3.5g", brand="Athenry", category="flower",
                               subcategory="3.5g", unit_weight=3.5, price=25,
                               cost=10, margin=15, quantity_on_hand=10)
        self.bundle = get_bundle("roll-relax")

    def _link(self, items=None, **kw):
        return signing.build_url(
            "/custom-order/", bundle="roll-relax", store=LOC,
            items=items or [("1", 1), ("10", 2), ("20", 1)],
            customer_token=signing.customer_token(PHONE), **kw)

    def _draft(self):
        return PhoneCartDraft.objects.order_by("-created_at").first()

    # ── stage 1: the emailed link ────────────────────────────────────────────
    def test_01_landing_renders_and_seeds_the_cart(self):
        with patch_inventory():
            r = self.client.get(self._link())
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Blue Dream 3.5g", body)
        self.assertIn("Happy Time", body)

        # The whole point of "add bundle to cart": they arrive already holding it.
        draft = self._draft()
        self.assertIsNotNone(draft, "landing must create a cart")
        self.assertEqual(draft.status, PhoneCartDraft.Status.OPEN)
        self.assertEqual(len(draft.lines), 3)
        self.assertEqual(sum(line["quantity"] for line in draft.lines), 4)  # 1 + 2 + 1

    def test_02_landing_is_idempotent_no_double_seed_on_refresh(self):
        # Mail apps prefetch links and people refresh. Neither may double the cart.
        with patch_inventory():
            self.client.get(self._link())
            self.client.get(self._link())
            self.client.get(self._link())
        drafts = PhoneCartDraft.objects.all()
        self.assertEqual(drafts.count(), 1, "refreshing must not create extra carts")
        self.assertEqual(sum(line["quantity"] for line in drafts.first().lines), 4)

    # ── stage 2: browsing ────────────────────────────────────────────────────
    def test_03_menu_and_results_render_for_the_same_visitor(self):
        with patch_inventory():
            self.client.get(self._link())
            menu = self.client.get(reverse("bundle_menu"), {"loc": LOC})
            grid = self.client.get(reverse("bundle_results"), {"loc": LOC, "format": "json"})
        self.assertEqual(menu.status_code, 200)
        self.assertEqual(grid.status_code, 200)
        self.assertEqual(grid.json()["total"], 6)

    def test_04_filters_narrow_the_grid(self):
        with patch_inventory():
            self.client.get(self._link())
            base = reverse("bundle_results")
            flower = self.client.get(base, {"loc": LOC, "cat": "flower", "format": "json"}).json()
            search = self.client.get(base, {"loc": LOC, "q": "marmas", "format": "json"}).json()
            brand = self.client.get(base, {"loc": LOC, "brand": "Doja", "format": "json"}).json()
        self.assertEqual(flower["total"], 2)
        self.assertEqual(search["total"], 1)
        self.assertEqual(brand["total"], 1)
        self.assertEqual(search["products"][0]["name"], "Marmas Sour Cherry 10pk")

    # ── stage 3: cart edits ──────────────────────────────────────────────────
    def test_05_add_update_remove_round_trip(self):
        with patch_inventory():
            self.client.get(self._link())
            add = self.client.post(reverse("bundle_cart_add"),
                                   {"loc": LOC, "product_id": "30", "qty": 1})
            self.assertEqual(add.status_code, 200)
            self.assertEqual(len(self._draft().lines), 4)

            self.client.post(reverse("bundle_cart_update"),
                             {"loc": LOC, "product_id": "30", "qty": 3})
            line = next(x for x in self._draft().lines if str(x["product_id"]) == "30")
            self.assertEqual(line["quantity"], 3)

            self.client.post(reverse("bundle_cart_remove"), {"loc": LOC, "product_id": "30"})
            self.assertNotIn("30", [str(x["product_id"]) for x in self._draft().lines])
            self.assertEqual(len(self._draft().lines), 3)

    def test_06_cart_totals_are_computed_server_side(self):
        with patch_inventory():
            self.client.get(self._link())
            ctx = cart_mod.reprice(self._draft(), inventory())
        # 25.00 + (12.00 x 2) + 15.00 = 64.00
        self.assertEqual(ctx["quote"]["subtotal"], 64.0)

    def test_06b_cart_quotes_undiscounted_and_says_so(self):
        """The cart deliberately does NOT apply the bundle percentage.

        A shopper can edit the cart until it no longer satisfies the bundle, so
        promising a discount here would be a promise we can't keep — the register
        decides. What the cart owes the shopper instead is the bundle's identity,
        so the page can tell them what to mention at the counter.

        This is pinned because it is surprising: `bundle_discount_pct` sits right
        there in the quote, and a future reader may "fix" the zero without
        realising the qualification rule is what's missing.
        """
        with patch_inventory():
            self.client.get(self._link())
            ctx = cart_mod.reprice(self._draft(), inventory())
        quote = ctx["quote"]
        self.assertEqual(quote["discounts"], 0.0)
        self.assertEqual(quote["total"], quote["subtotal"])
        self.assertEqual(quote["bundle"], "roll-relax")
        self.assertEqual(quote["bundle_discount_pct"], 20)

    def test_06c_landing_shows_the_discounted_price_the_email_promised(self):
        """The landing page DOES apply it — that's the offer being honoured.

        Landing and cart therefore quote different numbers for the same items
        ($51.20 vs $64.00). That gap is real and visible to shoppers; it is
        recorded here so a change to either surface shows up as a diff rather
        than as a surprise at the counter.
        """
        with patch_inventory():
            r = self.client.get(self._link())
        body = r.content.decode()
        self.assertIn("51.20", body, "landing must show the discounted 'Your price'")
        self.assertIn("64.00", body, "landing must also show the undiscounted subtotal")
        self.assertIn("comes off at the register", body)

    # ── stage 4: checkout ────────────────────────────────────────────────────
    def _checkout(self, **over):
        first, _, last = SHOPPER.partition(" ")
        payload = {"loc": LOC, "first_name": first, "last_name": last or first,
                   "phone": PHONE, "email": "sam@example.com"}
        payload.update(over)
        return self.client.post(reverse("bundle_checkout"), payload)

    def test_07_checkout_get_shows_the_order(self):
        with patch_inventory():
            self.client.get(self._link())
            r = self.client.get(reverse("bundle_checkout"), {"loc": LOC})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Blue Dream 3.5g", r.content.decode())

    def test_08_checkout_places_the_order(self):
        with patch_inventory(), patch("bundles.customers.attach") as attach:
            self.client.get(self._link())
            r = self._checkout()
        self.assertEqual(r.status_code, 200, r.content[:400])
        attach.assert_called_once()

        draft = self._draft()
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.pickup_name, SHOPPER)
        self.assertEqual(draft.phone_last4, "6999")
        self.assertEqual(draft.location_slug, LOC)
        self.assertIsNotNone(draft.released_at)
        self.assertIsNotNone(draft.expires_at)
        # Undiscounted by design (see test_06b) — the register applies the 20%.
        self.assertEqual(draft.quote["total"], 64.0)
        self.assertEqual(draft.quote["bundle_discount_pct"], 20,
                         "the budtender needs to know which discount to apply")

    def test_09_every_phone_format_reaches_the_same_customer(self):
        # AlpineIQ, Dutchie and a human typing all format this differently.
        seen = set()
        for i, raw in enumerate(["5094206999", "509-420-6999", "(509) 420-6999",
                                 "+1 509 420 6999", "15094206999", "509.420.6999"]):
            c = Client()
            with patch_inventory(), patch("bundles.customers.attach"):
                c.get(self._link())
                r = c.post(reverse("bundle_checkout"),
                           {"loc": LOC, "first_name": "Sam", "last_name": f"Number{i}", "phone": raw})
            self.assertEqual(r.status_code, 200, f"{raw} was rejected")
            d = PhoneCartDraft.objects.filter(pickup_name=f"Sam Number{i}").first()
            self.assertIsNotNone(d, f"{raw} produced no order")
            self.assertEqual(d.phone_last4, "6999")
            seen.add(d.phone_hash)
        self.assertEqual(len(seen), 1, f"one customer must yield one token, got {seen}")

    def test_10_success_page_clears_the_cart_cookie(self):
        # Otherwise a refresh lets the shopper edit an order staff is already picking.
        with patch_inventory(), patch("bundles.customers.attach"):
            self.client.get(self._link())
            self._checkout()
        morsel = self.client.cookies.get(cart_mod.COOKIE)
        self.assertTrue(morsel is None or morsel.value == "",
                        "the cart cookie must not survive a placed order")

    def test_11_a_second_visit_after_ordering_starts_a_fresh_cart(self):
        with patch_inventory(), patch("bundles.customers.attach"):
            self.client.get(self._link())
            self._checkout()
            placed = self._draft()
            self.client.get(reverse("bundle_menu"), {"loc": LOC})
        new = PhoneCartDraft.objects.exclude(pk=placed.pk).first()
        self.assertIsNotNone(new, "a new visit needs its own cart")
        self.assertEqual(new.status, PhoneCartDraft.Status.OPEN)
        placed.refresh_from_db()
        self.assertEqual(placed.status, PhoneCartDraft.Status.RELEASED,
                         "the placed order must not be reopened")

    # ── stage 5: the budtender ───────────────────────────────────────────────
    def test_12_budtender_claims_the_order_into_the_pos(self):
        from django.contrib.auth.models import User

        with patch_inventory(), patch("bundles.customers.attach"):
            self.client.get(self._link())
            self._checkout()
        order = self._draft()
        self.assertEqual(order.status, PhoneCartDraft.Status.RELEASED)

        User.objects.create_user("bud", password="pw12345!")
        staff = Client()
        staff.login(username="bud", password="pw12345!")

        with patch("pos.views.catalog.find_item",
                   side_effect=lambda store, product_id=None, **kw: next(
                       (p for p in inventory() if str(p["product_id"]) == str(product_id)), None)), \
             patch("pos.views._active_store") as active:
            active.return_value = type("S", (), {"name": "yakima"})()
            claimed = staff.post("/phone-cart/claim/", {"draft_token": order.draft_token})

        self.assertIn(claimed.status_code, (200, 302), claimed.content[:300])
        cart = staff.session.get("cart", [])
        self.assertEqual(len(cart), 3, f"budtender should hold 3 lines, got {cart}")
        # Prices come from the register row, not from anything the shopper's browser sent.
        self.assertEqual({c["Cnt"] for c in cart}, {1, 2})


@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class MoneyIntegrityTests(TestCase):
    """Whatever else drifts, the number the shopper is told must be defensible."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

    def _seed(self, inv=None):
        url = signing.build_url("/custom-order/", bundle="roll-relax", store=LOC,
                                items=[("1", 1), ("10", 2), ("20", 1)])
        with patch_inventory(inv):
            self.client.get(url)
        return PhoneCartDraft.objects.first()

    def test_client_cannot_dictate_price(self):
        draft = self._seed()
        with patch_inventory():
            self.client.post(reverse("bundle_cart_add"),
                             {"loc": LOC, "product_id": "30", "qty": 1,
                              "price": "0.01", "unit_price": "0.01", "line_total": "0.01"})
        line = next(x for x in self._reload(draft).lines if str(x["product_id"]) == "30")
        self.assertEqual(line["unit_price"], 26.0, "posted price must be ignored")

    def test_price_change_between_add_and_checkout_is_repriced(self):
        draft = self._seed()
        dearer = [dict(p, price=40.0) if p["product_id"] == "1" else p for p in inventory()]
        with patch_inventory(dearer):
            ctx = cart_mod.reprice(self._reload(draft), dearer)
        # 40 + 24 + 15 = 79. The cart quotes undiscounted (see test_06b), so the
        # point here is that the line repriced from 25 -> 40 rather than being
        # frozen at whatever it cost when the email went out.
        self.assertEqual(ctx["quote"]["subtotal"], 79.0)
        self.assertEqual(ctx["quote"]["total"], 79.0)
        repriced = next(x for x in ctx["lines"] if str(x["product_id"]) == "1")
        self.assertEqual(repriced["unit_price"], 40.0)

    def test_totals_have_no_floating_point_residue(self):
        draft = self._seed()
        with patch_inventory():
            q = cart_mod.reprice(self._reload(draft), inventory())["quote"]
        for key in ("subtotal", "discounts", "total"):
            value = q[key]
            self.assertEqual(round(value, 2), value,
                             f"{key}={value!r} carries sub-cent residue and will disagree "
                             "with the register")

    def test_discount_never_exceeds_subtotal(self):
        draft = self._seed()
        with patch_inventory():
            q = cart_mod.reprice(self._reload(draft), inventory())["quote"]
        self.assertLessEqual(q["discounts"], q["subtotal"])
        self.assertGreaterEqual(q["total"], 0)

    @staticmethod
    def _reload(draft):
        return PhoneCartDraft.objects.get(pk=draft.pk)


@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM)
class DegradedPipelineTests(TestCase):
    """The stages that must not 500 when something upstream is broken."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

    def _url(self):
        return signing.build_url("/custom-order/", bundle="roll-relax", store=LOC,
                                 items=[("1", 1), ("10", 2), ("20", 1)])

    def test_every_page_survives_a_dutchie_outage(self):
        with patch("bundles.cart.pos_catalog.get_inventory", side_effect=RuntimeError("dutchie down")):
            pages = {
                "landing": self.client.get(self._url()),
                "menu": self.client.get(reverse("bundle_menu"), {"loc": LOC}),
                "results": self.client.get(reverse("bundle_results"), {"loc": LOC}),
                "cart": self.client.get(reverse("bundle_cart"), {"loc": LOC}),
                "checkout": self.client.get(reverse("bundle_checkout"), {"loc": LOC}),
            }
        for name, resp in pages.items():
            self.assertLess(resp.status_code, 500,
                            f"{name} returned {resp.status_code} during an outage")

    def test_checkout_refuses_when_an_item_sold_out_after_it_was_added(self):
        with patch_inventory():
            self.client.get(self._url())
        gone = [p for p in inventory() if p["product_id"] != "1"]
        with patch_inventory(gone), patch("bundles.customers.attach"):
            r = self.client.post(reverse("bundle_checkout"),
                                 {"loc": LOC, "first_name": SHOPPER.split()[0],
                                  "last_name": SHOPPER.split()[-1], "phone": PHONE})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PhoneCartDraft.objects.filter(
            status=PhoneCartDraft.Status.RELEASED).count(), 0,
            "a sold-out line must not become a confirmed order")

    def test_a_dutchie_customer_lookup_failure_does_not_lose_the_order(self):
        """The realistic outage: the POS is unreachable when we look the shopper up.

        Patched at the Dutchie client rather than at `customers.attach`, because
        attach is the thing under test — it promises "never raises" and the
        checkout view leans on that promise with no try/except of its own. If this
        ever fails, a completed order is lost at the final step.
        """
        with patch_inventory():
            self.client.get(self._url())
        with patch_inventory(), \
             patch("bundles.customers._client", side_effect=RuntimeError("POS unreachable")):
            r = self.client.post(reverse("bundle_checkout"),
                                 {"loc": LOC, "first_name": SHOPPER.split()[0],
                                  "last_name": SHOPPER.split()[-1], "phone": PHONE})
        self.assertLess(r.status_code, 500, "a POS outage must not 500 the shopper")
        placed = PhoneCartDraft.objects.filter(status=PhoneCartDraft.Status.RELEASED)
        self.assertEqual(placed.count(), 1,
                         "the order must still be placed when the customer lookup fails")
        self.assertEqual(placed.first().customer_status,
                         PhoneCartDraft.Customer.UNRESOLVED,
                         "an unreachable POS should mark the customer unresolved, "
                         "so staff know to look them up at the counter")
