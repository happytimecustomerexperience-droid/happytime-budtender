"""Stock a shopper is holding is stock nobody else can be promised.

The defect this closes, measured before the fix: twenty shoppers each got a confirmed
order AND a confirmation email for a product with two units on hand — "UNITS PROMISED:
20 vs 2 physically on hand". Eighteen people would have driven in for nothing.

The hold is deliberately soft. A cart holds its units only while it is being used;
`updated_at` is `auto_now`, so any render or mutation renews it. Walk away for
RESERVE_MINUTES and the units return to the shelf for everyone else — but the CART
survives its full 30 days, so coming back and refreshing re-reserves whatever is
still there. That is the "don't trap inventory for more than 15 minutes" rule.

Nothing here touches the network: inventory is patched and the register client is
stubbed by the shared base class.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from budtender.models import PhoneCartDraft
from bundles import cart as cart_mod
from bundles.tests.test_resolver import live

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def floor(qty=2):
    """One product, `qty` units on the shelf."""
    return [live(product_id="1", name="Last Two 3.5g", price=25.0, qty=qty)]


@override_settings(CACHES=CACHES_LOCMEM)
class ReservationTests(TestCase):
    def setUp(self):
        pos = patch("bundles.customers._client")
        self.pos = pos.start()
        self.addCleanup(pos.stop)
        self.pos.return_value.guest_search.return_value = {"Data": []}

    def _shopper(self, qty=2):
        """A browser with its own cart cookie."""
        c = Client()
        with patch("bundles.cart.pos_catalog.get_inventory", return_value=floor(qty)):
            c.post("/custom-order/cart/add", {"loc": "yakima", "product_id": "1", "qty": 1})
        return c

    def _add(self, client, qty=2, n=1):
        with patch("bundles.cart.pos_catalog.get_inventory", return_value=floor(qty)):
            return client.post("/custom-order/cart/add",
                               {"loc": "yakima", "product_id": "1", "qty": n})

    # ── the original defect ──────────────────────────────────────────────────
    def test_twenty_shoppers_cannot_all_hold_the_last_two_units(self):
        clients = [self._shopper() for _ in range(20)]
        holding = [c for c in clients
                   if PhoneCartDraft.objects.filter(
                       draft_token=c.cookies[cart_mod.COOKIE].value).first().lines]
        self.assertEqual(len(holding), 2,
                         f"{len(holding)} shoppers hold a product with 2 on the shelf")

    def test_the_third_shopper_is_told_it_is_gone_not_given_a_false_order(self):
        self._shopper(); self._shopper()
        third = Client()
        r = self._add(third)
        self.assertEqual(PhoneCartDraft.objects.get(
            draft_token=third.cookies[cart_mod.COOKIE].value).lines, [])
        self.assertNotEqual(r.status_code, 500)

    # ── the hold is soft ─────────────────────────────────────────────────────
    def test_an_abandoned_cart_stops_holding_after_the_window(self):
        first = self._shopper()
        token = first.cookies[cart_mod.COOKIE].value
        # They wandered off. auto_now means we have to write the past explicitly.
        PhoneCartDraft.objects.filter(draft_token=token).update(
            updated_at=timezone.now() - timedelta(minutes=cart_mod.RESERVE_MINUTES + 1))

        second = Client()
        self._add(second)
        self.assertTrue(PhoneCartDraft.objects.get(
            draft_token=second.cookies[cart_mod.COOKIE].value).lines,
            "a stale cart is still trapping the shelf")

    def test_the_cart_itself_survives_the_reservation_lapsing(self):
        # Retention is the whole reason the cart is a 30-day cookie. Losing the hold
        # must not lose the cart.
        first = self._shopper()
        token = first.cookies[cart_mod.COOKIE].value
        PhoneCartDraft.objects.filter(draft_token=token).update(
            updated_at=timezone.now() - timedelta(hours=6))
        draft = PhoneCartDraft.objects.get(draft_token=token)
        self.assertEqual(draft.status, PhoneCartDraft.Status.OPEN)
        self.assertTrue(draft.lines)

    def test_coming_back_and_refreshing_re_reserves(self):
        first = self._shopper()
        token = first.cookies[cart_mod.COOKIE].value
        PhoneCartDraft.objects.filter(draft_token=token).update(
            updated_at=timezone.now() - timedelta(hours=6))
        # A refresh of their own cart bumps updated_at via reprice's save.
        with patch("bundles.cart.pos_catalog.get_inventory", return_value=floor(2)):
            first.get("/custom-order/cart?loc=yakima")
        held = cart_mod.reserved_units("yakima")
        self.assertEqual(held.get("1"), 1, "refreshing did not renew the hold")

    # ── who counts ───────────────────────────────────────────────────────────
    def test_a_shopper_is_not_charged_for_their_own_hold(self):
        # Otherwise the shelf count drops as they add, and they get blocked by
        # themselves at half the real stock.
        c = self._shopper(qty=2)
        token = c.cookies[cart_mod.COOKIE].value
        self.assertEqual(cart_mod.reserved_units("yakima", exclude_token=token), {})

    def test_a_released_order_still_holds_until_it_expires(self):
        # They are driving in to collect it — the unit is not on the shelf.
        c = self._shopper()
        PhoneCartDraft.objects.filter(draft_token=c.cookies[cart_mod.COOKIE].value).update(
            status=PhoneCartDraft.Status.RELEASED,
            expires_at=timezone.now() + timedelta(hours=4))
        self.assertEqual(cart_mod.reserved_units("yakima").get("1"), 1)

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
            location_slug="pullman")
        self.assertEqual(cart_mod.reserved_units("yakima"), {})

    # ── the checkout re-check ────────────────────────────────────────────────
    def test_a_cart_whose_stock_was_taken_is_flagged_before_checkout(self):
        """Held at add time, gone by checkout — the shopper must be told.

        `resolver.MIN_STOCK` is a module constant, not a setting, so a floor of 1 is
        never sellable at all; this uses 2 on the shelf and lets someone else's
        released order take both.
        """
        mine = self._shopper(qty=2)
        token = mine.cookies[cart_mod.COOKIE].value
        PhoneCartDraft.objects.create(
            location_slug="yakima", status=PhoneCartDraft.Status.RELEASED,
            expires_at=timezone.now() + timedelta(hours=4),
            lines=[{"product_id": "1", "name": "Last Two 3.5g", "quantity": 2}])

        draft = PhoneCartDraft.objects.get(draft_token=token)
        self.assertTrue(draft.lines, "the shopper never got the line to begin with")
        with patch("bundles.cart.pos_catalog.get_inventory", return_value=floor(2)):
            ctx = cart_mod.reprice(draft)
        self.assertFalse(ctx["lines"][0]["in_stock"])
        self.assertEqual(ctx["lines"][0]["issue"], "sold_out")
        self.assertEqual(ctx["quote"]["total"], 0.0)
