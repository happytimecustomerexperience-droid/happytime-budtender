"""Stock is promised when an order is PLACED, not when a cart is built.

The first defect, measured before any fix: twenty shoppers each got a confirmed order
AND a confirmation email for a product with two units on hand — "UNITS PROMISED: 20 vs
2 physically on hand". Eighteen people would have driven in for nothing.

The fix for that held units for every cart touched in the last 15 minutes. That turned
out to be a weapon: the cart cookie is free to mint, so one visitor could open cart after
cart and strip the whole shelf from real shoppers (finding W5a-2). So adding to a cart now
only CHECKS availability. The units are reserved when the shopper submits checkout with a
phone number — the RELEASED order holds them for DRAFT_TTL_HOURS — and checkout
re-validates stock and refuses what is gone.

Nothing here touches the network: inventory is patched and the register client is
stubbed by the shared base class.
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from budtender.models import PhoneCartDraft
from bundles import cart as cart_mod
from bundles import views
from bundles.tests.test_resolver import live

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def floor(qty=2):
    """One product, `qty` units on the shelf."""
    return [live(product_id="1", name="Last Two 3.5g", price=25.0, qty=qty)]


@override_settings(CACHES=CACHES_LOCMEM)
class ReservationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        pos = patch("bundles.customers._client")
        self.pos = pos.start()
        self.addCleanup(pos.stop)
        self.pos.return_value.guest_search.return_value = {"Data": []}
        self.phones = (f"50955{n:05d}" for n in range(100000))

    def _shopper(self, qty=2):
        """A browser with its own cart cookie holding one unit."""
        c = Client()
        with patch("bundles.cart.pos_catalog.get_inventory", return_value=floor(qty)):
            c.post("/custom-order/cart/add", {"loc": "yakima", "product_id": "1", "qty": 1})
        return c

    def _add(self, client, qty=2, n=1):
        with patch("bundles.cart.pos_catalog.get_inventory", return_value=floor(qty)):
            return client.post("/custom-order/cart/add",
                               {"loc": "yakima", "product_id": "1", "qty": n})

    def _check_out(self, client, qty=2):
        with patch("bundles.cart.pos_catalog.get_inventory", return_value=floor(qty)):
            return client.post("/custom-order/checkout", {
                "loc": "yakima", "first_name": "Sam", "last_name": "Reyes",
                "phone": next(self.phones)})

    def _draft(self, client):
        return PhoneCartDraft.objects.get(draft_token=client.cookies[cart_mod.COOKIE].value)

    # ── adding to a cart reserves nothing (finding W5a-2) ────────────────────
    def test_twenty_shoppers_can_all_put_the_last_two_units_in_a_cart(self):
        # The old behaviour capped this at two carts; one visitor could then take them all.
        clients = [self._shopper() for _ in range(20)]
        holding = [c for c in clients if self._draft(c).lines]
        self.assertEqual(len(holding), 20)
        self.assertEqual(cart_mod.reserved_units("yakima"), {})

    def test_a_cart_that_was_just_touched_still_holds_nothing(self):
        c = self._shopper()
        self.assertEqual(self._draft(c).status, PhoneCartDraft.Status.OPEN)
        self.assertEqual(cart_mod.reserved_units("yakima"), {})

    def test_one_visitor_cannot_strip_the_shelf_with_cookieless_adds(self):
        for _ in range(30):
            self._shopper(qty=12)
        real = Client()
        r = self._add(real, qty=12, n=12)
        self.assertEqual(self._draft(real).lines[0]["quantity"], 12)
        self.assertNotIn("sold out", r.content.decode().lower())

    # ── ...the order does, and checkout re-validates ─────────────────────────
    def test_twenty_shoppers_cannot_all_check_out_the_last_two_units(self):
        clients = [self._shopper() for _ in range(20)]
        statuses = [self._check_out(c).status_code for c in clients]
        placed = PhoneCartDraft.objects.filter(status=PhoneCartDraft.Status.RELEASED)
        self.assertEqual(placed.count(), 2,
                         f"{placed.count()} orders were confirmed for 2 units on the shelf")
        self.assertEqual(statuses.count(200), 2)
        self.assertEqual(statuses.count(400), 18)       # the "just sold out" path
        # The losers keep their carts: nothing is lost, they can swap the item.
        self.assertEqual(PhoneCartDraft.objects.filter(
            status=PhoneCartDraft.Status.OPEN).count(), 18)

    def test_a_refused_checkout_says_the_cart_changed_and_places_nothing(self):
        first, second, late = (self._shopper() for _ in range(3))
        self._check_out(first)
        self._check_out(second)
        r = self._check_out(late)
        self.assertEqual(r.status_code, 400)
        self.assertIn('<span class="a">sold out</span>', r.content.decode())
        self.assertEqual(self._draft(late).status, PhoneCartDraft.Status.OPEN)

    def test_a_placed_order_keeps_holding_for_the_usual_window(self):
        c = self._shopper()
        self._check_out(c)
        order = PhoneCartDraft.objects.get(status=PhoneCartDraft.Status.RELEASED)
        self.assertAlmostEqual(
            (order.expires_at - order.released_at).total_seconds(),
            views.DRAFT_TTL_HOURS * 3600, delta=60)
        self.assertEqual(cart_mod.reserved_units("yakima").get("1"), 1)

    def test_the_third_shopper_is_told_it_is_gone_not_given_a_false_order(self):
        for _ in range(2):
            self._check_out(self._shopper())
        before = PhoneCartDraft.objects.count()
        third = Client()
        r = self._add(third)
        self.assertEqual(r.status_code, 200)
        self.assertIn("sold out", r.content.decode().lower())
        self.assertEqual(PhoneCartDraft.objects.count(), before)    # no cart minted for it

    # ── who counts ───────────────────────────────────────────────────────────
    def test_a_released_order_still_holds_until_it_expires(self):
        # They are driving in to collect it — the unit is not on the shelf.
        c = self._shopper()
        PhoneCartDraft.objects.filter(draft_token=c.cookies[cart_mod.COOKIE].value).update(
            status=PhoneCartDraft.Status.RELEASED,
            expires_at=timezone.now() + timedelta(hours=4))
        self.assertEqual(cart_mod.reserved_units("yakima").get("1"), 1)

    def test_an_expired_order_stops_holding(self):
        c = self._shopper()
        PhoneCartDraft.objects.filter(draft_token=c.cookies[cart_mod.COOKIE].value).update(
            status=PhoneCartDraft.Status.RELEASED,
            expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(cart_mod.reserved_units("yakima"), {})

    def test_a_claimed_order_stops_holding(self):
        # The budtender has it; the stock left the shelf at the register. Counting it
        # here as well would double-reserve the same unit.
        c = self._shopper()
        PhoneCartDraft.objects.filter(draft_token=c.cookies[cart_mod.COOKIE].value).update(
            status=PhoneCartDraft.Status.CLAIMED)
        self.assertEqual(cart_mod.reserved_units("yakima"), {})

    def test_holds_do_not_leak_across_stores(self):
        c = self._shopper()
        PhoneCartDraft.objects.filter(draft_token=c.cookies[cart_mod.COOKIE].value).update(
            status=PhoneCartDraft.Status.RELEASED,
            expires_at=timezone.now() + timedelta(hours=4), location_slug="pullman")
        self.assertEqual(cart_mod.reserved_units("yakima"), {})

    # ── the cart-side re-check ───────────────────────────────────────────────
    def test_a_cart_whose_stock_was_taken_is_flagged_before_checkout(self):
        """In the cart at add time, gone by checkout — the shopper must be told.

        `resolver.MIN_STOCK` is a module constant, not a setting, so a floor of 1 is
        never sellable at all; this uses 2 on the shelf and lets someone else's
        released order take both.
        """
        mine = self._shopper(qty=2)
        PhoneCartDraft.objects.create(
            location_slug="yakima", status=PhoneCartDraft.Status.RELEASED,
            expires_at=timezone.now() + timedelta(hours=4),
            lines=[{"product_id": "1", "name": "Last Two 3.5g", "quantity": 2}])

        draft = self._draft(mine)
        self.assertTrue(draft.lines, "the shopper never got the line to begin with")
        with patch("bundles.cart.pos_catalog.get_inventory", return_value=floor(2)):
            ctx = cart_mod.reprice(draft)
        self.assertFalse(ctx["lines"][0]["in_stock"])
        self.assertEqual(ctx["lines"][0]["issue"], "sold_out")
        self.assertEqual(ctx["quote"]["total"], 0.0)
