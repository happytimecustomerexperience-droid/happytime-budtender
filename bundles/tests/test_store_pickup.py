"""Which store, and where to drive.

Stock, price and pickup all follow the selected store, so getting it wrong is not
cosmetic — a Pullman shopper sent to the Yakima menu is shown product they cannot
walk in and buy, and a wrong address on the confirmation is a wasted trip.

Yakima is the default because it is by far the largest store; the other two are
opt-in and the choice sticks so navigation doesn't silently reset it.

Addresses and phones are pinned against happytimeweed.com's own content — if the
marketing site and this storefront disagree about where a store is, one of them is
sending people to the wrong door.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from budtender.models import PhoneCartDraft
from bundles.catalog import STORE_ADDRESS, all_stores, store_info
from bundles.tests.test_resolver import live
from bundles.views import STORE_COOKIE

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# From happytimeweed.com data/author.json + the location pages.
EXPECTED = {
    "yakima": ("1315 N 1st St", "Yakima, WA 98901", "(509) 571-1106"),
    "mount-vernon": ("200 Suzanne Ln", "Mt Vernon, WA 98273", "(360) 488-2923"),
    "pullman": ("5602 WA-270", "Pullman, WA 99163", "(509) 334-2788"),
}


def inventory():
    return [
        live(product_id="1", name="Blue Dream 3.5g", price=25.0),
        live(product_id="10", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="PR One", unit_grams=1.0, price=8.0),
    ]


def patch_inv():
    return patch("bundles.cart.pos_catalog.get_inventory", return_value=inventory())


class StoreDataTests(TestCase):
    def test_every_store_has_a_real_street_address_and_phone(self):
        for slug, (street, city, phone) in EXPECTED.items():
            info = store_info(slug)
            self.assertEqual(info["street"], street, slug)
            self.assertEqual(info["city"], city, slug)
            self.assertEqual(info["phone"], phone, slug)
            self.assertIn(street, info["address"])

    def test_no_store_is_left_with_a_city_only_placeholder(self):
        # The old value was "Yakima, WA" — useless to someone trying to drive there.
        for slug in EXPECTED:
            self.assertRegex(STORE_ADDRESS[slug], r"^\d",
                             f"{slug} address should start with a street number")

    def test_directions_link_is_built_for_each_store(self):
        for slug in EXPECTED:
            url = store_info(slug)["map_url"]
            self.assertTrue(url.startswith("https://www.google.com/maps/"), slug)
            self.assertIn("Happy+Time", url)

    def test_yakima_is_listed_first(self):
        self.assertEqual(all_stores()[0]["slug"], "yakima")
        self.assertEqual(len(all_stores()), 3)


@override_settings(CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class StoreSelectionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

    def _menu(self, qs=""):
        with patch_inv():
            return self.client.get(f"/custom-order/menu{qs}")

    def test_defaults_to_yakima_with_no_preference(self):
        body = self._menu().content.decode()
        self.assertIn("Pickup at Happy Time — Yakima", body)
        self.assertIn("1315 N 1st St", body)

    def test_an_explicit_loc_selects_that_store(self):
        # Every store's address appears — the other two are options in the picker —
        # so assert on which one is ACTIVE, not on mere presence.
        body = self._menu("?loc=pullman").content.decode()
        self.assertIn("Pickup at Happy Time — Pullman", body)
        self.assertNotIn("Pickup at Happy Time — Yakima", body)
        self.assertIn('class="storeopt on"', body)

    def test_the_choice_is_remembered_across_navigation(self):
        # Without this a Pullman shopper is dumped back on Yakima the moment they
        # click anything that doesn't carry ?loc=.
        self._menu("?loc=pullman")
        self.assertEqual(self.client.cookies[STORE_COOKIE].value, "pullman")
        body = self._menu().content.decode()
        self.assertIn("Pickup at Happy Time — Pullman", body)

    def test_an_explicit_loc_beats_a_stale_cookie(self):
        # A bundle link names its store; a leftover cookie must never override it.
        self._menu("?loc=pullman")
        body = self._menu("?loc=mount-vernon").content.decode()
        self.assertIn("Pickup at Happy Time — Mount Vernon", body)

    def test_an_unknown_loc_falls_back_rather_than_erroring(self):
        r = self._menu("?loc=mars")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Pickup at Happy Time — Yakima", r.content.decode())

    def test_the_picker_offers_all_three_stores(self):
        body = self._menu().content.decode()
        for slug, (street, _, _) in EXPECTED.items():
            self.assertIn(street, body, f"{slug} missing from the picker")


@override_settings(CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class PickupDetailTests(TestCase):
    """Checkout and the confirmation must say exactly where to go."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

    def _checkout(self, loc="yakima"):
        with patch_inv():
            self.client.post("/custom-order/cart/add",
                             {"loc": loc, "product_id": "1", "qty": 1})
            return self.client.get(f"/custom-order/checkout?loc={loc}")

    def test_checkout_states_it_is_a_pickup_reservation(self):
        body = self._checkout().content.decode()
        self.assertIn("reservation for in-store pickup", body)
        self.assertIn("no payment now", body.lower())

    def test_checkout_shows_the_full_address_phone_and_hours(self):
        for slug, (street, city, phone) in EXPECTED.items():
            body = self._checkout(slug).content.decode()
            self.assertIn(street, body, slug)
            self.assertIn(city, body, slug)
            self.assertIn(phone, body, slug)
            self.assertIn("Directions", body, slug)
            self.client = Client()   # fresh shopper per store

    def test_the_confirmation_repeats_the_address_and_directions(self):
        with patch_inv(), patch("bundles.customers.attach"):
            self.client.post("/custom-order/cart/add",
                             {"loc": "pullman", "product_id": "1", "qty": 1})
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "pullman", "name": "Sam Reyes",
                                  "phone": "5094206999"})
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("5602 WA-270", body)
        self.assertIn("Pullman, WA 99163", body)
        self.assertIn("(509) 334-2788", body)
        self.assertIn("google.com/maps", body)
        self.assertEqual(PhoneCartDraft.objects.get(
            status=PhoneCartDraft.Status.RELEASED).location_slug, "pullman")

    def test_an_order_is_filed_against_the_store_it_was_placed_at(self):
        # The draft's location_slug is what routes it to a POS queue; if it drifts
        # from the address the shopper was shown, staff and customer disagree.
        with patch_inv(), patch("bundles.customers.attach"):
            self.client.post("/custom-order/cart/add",
                             {"loc": "mount-vernon", "product_id": "1", "qty": 1})
            self.client.post("/custom-order/checkout",
                             {"loc": "mount-vernon", "name": "Sam Reyes",
                              "phone": "5094206999"})
        draft = PhoneCartDraft.objects.get(status=PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.location_slug, "mount-vernon")
