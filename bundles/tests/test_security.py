"""Adversarial tests for /custom-order — the app's ONLY unauthenticated HTML.

Everything else in this repo is `@login_required`, so this surface is where a
mistake is reachable by anyone with the URL. These tests are written from the
attacker's side of the wire and cover six properties:

  1. LEAKAGE   — no staff-only signal from `pos.catalog._normalize` (margin_pct,
                 velocity, price_z, bucket, cost, vendor, received_date) and no
                 register plumbing (SerialNo/BatchId/RecUnitPrice/package_id/
                 CannbisProduct) reaches a rendered body.
  2. FORGERY   — the signed link IS the coupon; every hand-edit is refused.
  3. ISOLATION — one shopper's cart is unreachable from another browser.
  4. INPUT     — hostile POST bodies produce a clean 4xx or a sane clamp, never a 500.
  5. THROTTLE  — the cart and checkout rate limits actually engage.
  6. METHOD    — POST-only routes reject GET; public routes never bounce to login;
                 the neighbouring POS routes still do.

Leak assertions are made against the RENDERED RESPONSE BODY, not the serializer
output, because a template can reintroduce a field the projection dropped. The
inventory fixture below poisons every staff-only key with an unmistakable canary,
so a leak shows up as a value even when rendered under a friendly label.

Nothing here may touch the network: `pos.catalog.get_inventory` is patched per
test and `bundles.customers._client` (the Dutchie `PosRegisterClient`) is patched
for every test in this file, in the base class.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings

from budtender.models import PhoneCartDraft
from bundles import cart as cart_mod
from bundles import signing
from bundles.tests.test_resolver import live

SECRET = "unit-test-secret-value"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Frozen clock for the rate-limit buckets. `rate_limit` keys on
# `int(time.time() // window)`; a real clock lets a 240-request loop straddle a
# minute boundary and silently reset the counter, so the test would pass by luck.
FROZEN = 1_700_000_000.0

# Field NAMES that must never appear in a public body — the staff-only signals and
# register plumbing `pos.catalog._normalize` stamps onto every row.
FORBIDDEN_KEYS = (
    "margin", "margin_pct", "cost", "velocity", "price_z", "bucket",
    "serialno", "batchid", "recunitprice", "unitprice", "package_id",
    "cannbisproduct", "vendor", "received_date", "productdesc", "raw_category",
)

# Field VALUES, from the poisoned fixture below. A template that renders a staff
# field under a friendly label ("Best value!") leaks the value without the key.
FORBIDDEN_VALUES = (
    "canaryvendor", "canarybucket", "canaryserial", "canarybatch",
    "canarypackage", "canarycannabis", "canarydesc", "2029-12-25",
    "7.77", "0.6161", "1.2323", "99.91", "99.92",
)


def leaky(**kw):
    """A `live()` row whose staff-only fields carry unmistakable canaries.

    Built on the shared fixture on purpose: `live()` is pinned to every key
    `pos.catalog._normalize` emits (`test_live_fixture_matches_normalize_output`),
    so when `_normalize` grows a field this fixture grows with it and the leak
    guard keeps covering the whole row.
    """
    d = live(
        vendor="CanaryVendor", received_date="2029-12-25",
        velocity=7.77, margin_pct=0.6161, price_z=1.2323, bucket="CanaryBucket",
        SerialNo="CanarySerial", BatchId="CanaryBatch", package_id="CanaryPackage",
        UnitPrice=99.92, RecUnitPrice=99.91, CannbisProduct="CanaryCannabis",
        ProductDesc="CanaryDesc",
    )
    d.update(kw)
    return d


def inventory():
    """Yakima's floor: one of each category so every bundle slot can resolve."""
    return [
        leaky(product_id="1", name="Blue Dream 3.5g", price=25.0),
        leaky(product_id="2", name="OG Kush 3.5g", brand="Other", price=27.0),
        leaky(product_id="10", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
              name="PR One", unit_grams=1.0, price=8.0),
        leaky(product_id="20", cat_key="edibles", cat_label="Edibles", subcategory="10pk",
              name="Gummies", unit_grams=None, price=15.0),
    ]


# Store-keyed floors. `cart.inventory_for` calls `get_inventory(store_key_for(loc))`,
# so a side_effect on this seam is what proves cross-store isolation: product 900
# exists ONLY in Pullman and must be unaddressable from a Yakima cart.
PER_STORE = {
    "yakima": inventory(),
    "pullman": [leaky(product_id="900", name="Pullman Exclusive", price=40.0)],
}


class PublicSurfaceTestCase(TestCase):
    """Shared setup.

    LocMemCache is process-global — clear it coming AND going, or a primed
    rate-limit bucket leaks into whatever class happens to run next.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()
        # Checkout resolves the shopper against Dutchie. Patch the register client
        # for EVERY test in this file — an input-abuse payload that happens to be a
        # valid order must never become a live guest_search.
        pos = patch("bundles.customers._client")
        self.pos_client = pos.start()
        self.addCleanup(pos.stop)
        self.pos_client.return_value.guest_search.return_value = {"Data": []}
        self.pos_client.return_value.create_guest.return_value = None

    # ── seams ────────────────────────────────────────────────────────────────
    def _patch_inv(self, inv=None):
        return patch("bundles.cart.pos_catalog.get_inventory",
                     return_value=inv if inv is not None else inventory())

    def _patch_stores(self):
        return patch("bundles.cart.pos_catalog.get_inventory",
                     side_effect=lambda key, *a, **kw: list(PER_STORE.get(key, [])))

    def _add(self, product_id="1", qty=1, loc="yakima", inv=None, client=None):
        with self._patch_inv(inv):
            return (client or self.client).post(
                "/custom-order/cart/add",
                {"loc": loc, "product_id": product_id, "qty": qty})

    def _signed(self, **kw):
        kw.setdefault("bundle", "roll-relax")
        kw.setdefault("store", "yakima")
        kw.setdefault("items", [("1", 1), ("10", 2), ("20", 1)])
        return signing.build_url("/custom-order/", **kw)

    # ── assertions ───────────────────────────────────────────────────────────
    def assertNoLeak(self, response, where):
        body = response.content.decode().lower()
        for word in FORBIDDEN_KEYS:
            self.assertNotIn(word, body, f"{where}: staff field name '{word}' reached the page")
        for value in FORBIDDEN_VALUES:
            self.assertNotIn(value, body, f"{where}: staff value '{value}' reached the page")


# ── 1. leakage ───────────────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2,
                   BUNDLE_MAX_ORDER_TOTAL=300)
class LeakGuardTests(PublicSurfaceTestCase):
    """Every reachable public body, rendered from a fully poisoned inventory row."""

    def test_the_guard_would_actually_catch_a_leak(self):
        # Guard the guard, twice over: the canaries must really be on the rows the
        # view sees, and `assertNoLeak` must really fail when one comes back out.
        # Without this, every assertion below could be passing vacuously.
        self.assertIn("CanaryVendor", str(list(leaky().values())))
        with self.assertRaises(AssertionError):
            self.assertNoLeak(HttpResponse("velocity: 7.77 (CanaryVendor)"), "synthetic")

    def test_the_css_is_linked_not_inlined(self):
        # The whole leak guard rests on this: `margin` is a legitimate CSS property
        # and appears ~39x in bundle.css. That is only harmless while the
        # stylesheet stays a <link>. Inline it (or add a <style> block) and the
        # `margin` assertion becomes a false alarm, so pin the assumption here.
        with self._patch_inv():
            for url in ("/custom-order/menu?loc=yakima", "/custom-order/checkout?loc=yakima"):
                body = self.client.get(url).content.decode().lower()
                self.assertIn('<link rel="stylesheet"', body, url)
                self.assertNotIn("<style", body, f"{url} inlines CSS — the leak guard is blind")

    def test_menu_body_is_clean(self):
        with self._patch_inv():
            self.assertNoLeak(self.client.get("/custom-order/menu?loc=yakima"), "menu")

    def test_results_html_body_is_clean(self):
        with self._patch_inv():
            self.assertNoLeak(self.client.get("/custom-order/results?loc=yakima"), "results html")

    def test_results_json_body_is_clean(self):
        with self._patch_inv():
            r = self.client.get("/custom-order/results?loc=yakima&format=json")
        self.assertNoLeak(r, "results json")
        # And positively: the wire shape is exactly `resolver._public`, no more.
        self.assertEqual(
            sorted(r.json()["products"][0]),
            ["brand", "category", "category_label", "image", "image_static", "name",
             "price", "product_id", "qty", "size", "strain", "strain_type", "thc"])

    def test_cart_body_is_clean(self):
        self._add("1")
        with self._patch_inv():
            self.assertNoLeak(self.client.get("/custom-order/cart?loc=yakima"), "cart")

    def test_cart_mutation_bodies_are_clean(self):
        # add/update/remove each re-render the cart partial from live rows.
        with self._patch_inv():
            self.assertNoLeak(self.client.post("/custom-order/cart/add",
                                               {"loc": "yakima", "product_id": "1", "qty": 2}),
                              "cart/add")
            self.assertNoLeak(self.client.post("/custom-order/cart/update",
                                               {"loc": "yakima", "product_id": "1", "qty": 3}),
                              "cart/update")
            self.assertNoLeak(self.client.post("/custom-order/cart/remove",
                                               {"loc": "yakima", "product_id": "1"}),
                              "cart/remove")

    def test_checkout_form_body_is_clean(self):
        self._add("1")
        with self._patch_inv():
            self.assertNoLeak(self.client.get("/custom-order/checkout?loc=yakima"), "checkout")

    def test_checkout_error_body_is_clean(self):
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "", "phone": "1"})
        self.assertEqual(r.status_code, 400)
        self.assertNoLeak(r, "checkout errors")

    def test_success_body_is_clean(self):
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "Sam Reyes", "phone": "5095551212"})
        self.assertContains(r, "Order placed")
        self.assertNoLeak(r, "success")

    def test_landing_body_is_clean(self):
        with self._patch_inv():
            r = self.client.get(self._signed())
        self.assertEqual(r.status_code, 200)
        self.assertNoLeak(r, "landing")

    def test_landing_with_a_substitution_is_clean(self):
        # The substitution path renders a DIFFERENT row than the email named,
        # chosen by a scorer that reads margin_pct and velocity — exactly the
        # place a "why did we pick this?" debug field would get left behind.
        inv = [p for p in inventory() if p["product_id"] != "1"]
        with self._patch_inv(inv):
            r = self.client.get(self._signed())
        self.assertContains(r, "OG Kush 3.5g")
        self.assertNoLeak(r, "landing (substituted)")

    def test_invalid_link_body_is_clean(self):
        with self._patch_inv():
            r = self.client.get(self._signed().replace("b=roll-relax", "b=weekend"))
        self.assertEqual(r.status_code, 400)
        self.assertNoLeak(r, "invalid link")

    def test_stock_on_hand_is_not_printed(self):
        # `_public` carries `qty` so the add button can reason about stock; the
        # templates must not print it. A public "3 left" is a fine product
        # decision — but it has to BE a decision, not something that arrives by
        # accident with the projection.
        inv = [leaky(product_id="1", name="Blue Dream 3.5g", price=25.0, qty=737)]
        with self._patch_inv(inv):
            body = self.client.get("/custom-order/results?loc=yakima").content.decode()
        self.assertNotIn("737", body)


# ── 2. forgery ───────────────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class SignedLinkForgeryTests(PublicSurfaceTestCase):
    """The URL is the coupon. Hand-editing it must never mint an offer.

    Asserted at the HTTP boundary, not on `signing.parse`: what matters is that the
    VIEW refuses, seeds no cart and shows no discount. A parser that raises into a
    handler which renders anyway would still be a live coupon generator.
    """

    def assertRefused(self, url, what):
        with self._patch_inv():
            r = self.client.get(url)
        body = r.content.decode()
        self.assertEqual(r.status_code, 400, f"{what} was not refused")
        self.assertIn("This link didn't open", body, what)
        for pct in ("20%", "25%", "30%"):
            self.assertNotIn(pct, body, f"{what} still rendered a discount")
        self.assertEqual(PhoneCartDraft.objects.count(), 0,
                         f"{what} seeded a cart before verifying the signature")

    def test_the_honest_link_is_accepted(self):
        # The control. Without it, a view that 400s on everything would "pass".
        with self._patch_inv():
            r = self.client.get(self._signed())
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Roll &amp; Relax Bundle")
        self.assertEqual(PhoneCartDraft.objects.count(), 1)

    def test_upgrading_the_bundle_slug_is_refused(self):
        # roll-relax is 20% off; weekend is 30%. This is the money attack.
        self.assertRefused(self._signed().replace("b=roll-relax", "b=weekend"),
                           "discount upgrade via b=")

    def test_appending_an_item_is_refused(self):
        self.assertRefused(self._signed() + "&i=2%3A1", "extra item")

    def test_changing_a_quantity_is_refused(self):
        self.assertRefused(self._signed(items=[("1", 1)]).replace("i=1%3A1", "i=1%3A12"),
                           "quantity bump")

    def test_changing_the_store_is_refused(self):
        self.assertRefused(self._signed().replace("loc=yakima", "loc=pullman"), "store swap")

    def test_extending_the_expiry_is_refused(self):
        url = self._signed(ttl_days=1, now=1_000_000)
        self.assertIn("exp=1086400", url)
        self.assertRefused(url.replace("exp=1086400", "exp=9999999999"), "expiry extension")

    def test_stripping_the_signature_is_refused(self):
        self.assertRefused(self._signed().split("&sig=")[0], "unsigned link")

    def test_an_empty_signature_is_refused(self):
        self.assertRefused(self._signed().split("&sig=")[0] + "&sig=", "blank signature")

    def test_a_truncated_signature_is_refused(self):
        self.assertRefused(self._signed()[:-8], "truncated signature")

    def test_a_signature_from_another_secret_is_refused(self):
        with override_settings(BUNDLE_URL_SECRET="an-attackers-own-secret"):
            forged = self._signed()
        self.assertRefused(forged, "foreign-secret signature")

    def test_a_bare_url_with_no_params_is_refused(self):
        self.assertRefused("/custom-order/", "bare /custom-order/")

    def test_a_validly_signed_link_to_an_unknown_store_is_404_not_a_fallback(self):
        # A signed link is trusted for WHAT, never for WHERE — silently falling
        # back to Yakima would quote a shopper products another store carries.
        with self._patch_inv():
            r = self.client.get(self._signed(store="mars"))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(PhoneCartDraft.objects.count(), 0)

    def test_a_validly_signed_link_to_an_unknown_bundle_is_404(self):
        with self._patch_inv():
            r = self.client.get(self._signed(bundle="free-everything"))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(PhoneCartDraft.objects.count(), 0)

    def test_the_discount_depth_never_travels_on_the_url(self):
        # Belt and braces: even the honest link must not carry a percentage the
        # client could edit. Depth is looked up server-side from the slug.
        url = self._signed()
        self.assertNotIn("discount", url)
        self.assertNotIn("pct", url)


# ── 3. cart isolation (IDOR) ─────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class CartIsolationTests(PublicSurfaceTestCase):
    """The cart is addressed ONLY by the `htco` cookie (`cart.get_cart` /
    `cart.attach_cookie`). So the isolation story is two claims: nothing else in a
    request can select a draft, and the cookie value cannot be guessed."""

    def setUp(self):
        super().setUp()
        self.alice = Client()
        self.bob = Client()
        self._add("1", client=self.alice)
        self.a_draft = PhoneCartDraft.objects.get()
        self.a_token = self.a_draft.draft_token

    def assertAliceUntouched(self):
        self.a_draft.refresh_from_db()
        self.assertEqual([line_["product_id"] for line_ in self.a_draft.lines], ["1"])
        self.assertEqual(self.a_draft.lines[0]["quantity"], 1)
        self.assertEqual(self.a_draft.status, PhoneCartDraft.Status.OPEN)

    def test_alice_can_see_her_own_cart(self):
        # The positive control for this whole class. Every other test here asserts
        # a stranger does NOT see "Blue Dream 3.5g"; if the cart page never printed
        # that string, all of them would pass while proving nothing.
        with self._patch_inv():
            r = self.alice.get("/custom-order/cart?loc=yakima")
        self.assertContains(r, "Blue Dream 3.5g")

    def test_a_stranger_sees_an_empty_cart_not_alices(self):
        with self._patch_inv():
            r = self.bob.get("/custom-order/cart?loc=yakima")
        self.assertNotContains(r, "Blue Dream 3.5g")
        self.assertContains(r, "Your cart is empty")
        self.assertAliceUntouched()

    def test_a_stranger_cannot_add_to_alices_cart(self):
        self._add("10", client=self.bob)
        self.assertAliceUntouched()
        self.assertEqual(PhoneCartDraft.objects.count(), 2)

    def test_a_stranger_cannot_change_alices_quantities(self):
        with self._patch_inv():
            self.bob.post("/custom-order/cart/update",
                          {"loc": "yakima", "product_id": "1", "qty": 12})
        self.assertAliceUntouched()

    def test_a_stranger_cannot_remove_alices_lines(self):
        with self._patch_inv():
            self.bob.post("/custom-order/cart/remove", {"loc": "yakima", "product_id": "1"})
        self.assertAliceUntouched()

    def test_a_stranger_cannot_check_alices_cart_out(self):
        with self._patch_inv():
            r = self.bob.post("/custom-order/checkout",
                              {"loc": "yakima", "name": "Mallory Vane", "phone": "5095550000"})
        self.assertContains(r, "Your cart is empty")
        self.assertAliceUntouched()
        self.assertEqual(self.a_draft.pickup_name, "")

    def test_naming_the_draft_in_the_post_body_does_not_address_it(self):
        # Every plausible parameter an attacker would reach for. The views read the
        # cookie and nothing else, so all of these must be inert.
        for field in ("draft_token", "token", "cart", "cart_token", "htco", "id", "pk",
                      "draft", "session_token", "acct_id"):
            with self.subTest(field=field), self._patch_inv():
                self.bob.post("/custom-order/cart/update",
                              {"loc": "yakima", "product_id": "1", "qty": 9,
                               field: self.a_token})
                self.bob.post("/custom-order/cart/remove",
                              {"loc": "yakima", "product_id": "1", field: self.a_token})
            self.assertAliceUntouched()

    def test_naming_the_draft_in_the_query_string_does_not_address_it(self):
        for field in ("draft_token", "token", "cart", "htco", "id"):
            with self.subTest(field=field), self._patch_inv():
                r = self.bob.get(f"/custom-order/cart?loc=yakima&{field}={self.a_token}")
            self.assertNotContains(r, "Blue Dream 3.5g")
        self.assertAliceUntouched()

    def test_a_guessed_cookie_never_lands_on_a_real_cart(self):
        guesses = [
            "", "pc-", "pc-1", "1", "0", str(self.a_draft.pk), "online",
            "pc-" + "A" * 24,                                   # right shape, wrong bytes
            self.a_token[:-1] + ("A" if not self.a_token.endswith("A") else "B"),
            self.a_token.upper(), self.a_token.lower(), self.a_token[:-4],
            self.a_token + "x", self.a_token.replace("pc-", ""),
            "pc-" + "_" * 24, "../" + self.a_token, self.a_token + "%",
            # Near-misses: a bearer credential must match exactly, with no
            # normalising of whitespace or trailing junk on the way in.
            " " + self.a_token, self.a_token + " ", self.a_token + "\t",
            self.a_token + "; x=1", "pc-" + "%00" + self.a_token[3:],
        ]
        for guess in guesses:
            with self.subTest(guess=guess):
                mallory = Client()
                mallory.cookies[cart_mod.COOKIE] = guess
                with self._patch_inv():
                    r = mallory.get("/custom-order/cart?loc=yakima")
                self.assertNotContains(r, "Blue Dream 3.5g")
        self.assertAliceUntouched()

    def test_the_cookie_token_is_not_guessable(self):
        # 'pc-' + secrets.token_urlsafe(18) — 144 bits. The cookie is a bearer
        # token, so its entropy IS the access control on someone's cart.
        tokens = [PhoneCartDraft.objects.create(location_slug="yakima").draft_token
                  for _ in range(40)]
        self.assertEqual(len(set(tokens)), 40, "draft tokens collide — they are not random")
        for token in tokens:
            self.assertTrue(token.startswith("pc-"))
            self.assertGreaterEqual(len(token), 24)
        # Nothing sequential: consecutive rows must not share a growing prefix.
        self.assertNotEqual(tokens[0][3:8], tokens[1][3:8])

    def test_a_cart_does_not_follow_the_cookie_to_another_store(self):
        # Same shopper, different store page: the Yakima cart must not be readable
        # — or quotable — from the Pullman storefront.
        with self._patch_stores():
            r = self.alice.get("/custom-order/cart?loc=pullman")
        self.assertNotContains(r, "Blue Dream 3.5g")
        self.assertAliceUntouched()

    def test_a_released_order_cannot_be_reopened_by_replaying_the_cookie(self):
        # After checkout the draft is `released` and a budtender may already be
        # picking it. Replaying the old cookie must start a NEW cart, never edit
        # the order sitting in the staff queue.
        with self._patch_inv():
            self.alice.post("/custom-order/checkout",
                            {"loc": "yakima", "name": "Alice Adams", "phone": "5095551212"})
        self.a_draft.refresh_from_db()
        self.assertEqual(self.a_draft.status, PhoneCartDraft.Status.RELEASED)

        mallory = Client()
        mallory.cookies[cart_mod.COOKIE] = self.a_token
        self._add("10", client=mallory)
        self.a_draft.refresh_from_db()
        self.assertEqual(self.a_draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual([line_["product_id"] for line_ in self.a_draft.lines], ["1"])

    def test_the_cart_cookie_is_httponly_secure_and_samesite(self):
        # HttpOnly keeps XSS from reading the cart token; Lax keeps a cross-site
        # POST from mutating someone's cart.
        with self._patch_inv():
            r = self.client.get("/custom-order/menu?loc=yakima")
        cookie = r.cookies[cart_mod.COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["samesite"], "Lax")


# ── 4. input abuse ───────────────────────────────────────────────────────────
CART_ABUSE = [
    {},
    {"loc": "yakima"},
    {"product_id": "1"},
    {"loc": "yakima", "product_id": "1", "qty": ""},
    {"loc": "yakima", "product_id": "1", "qty": "abc"},
    {"loc": "yakima", "product_id": "1", "qty": "1.9"},
    {"loc": "yakima", "product_id": "1", "qty": "-5"},
    {"loc": "yakima", "product_id": "1", "qty": "0"},
    {"loc": "yakima", "product_id": "1", "qty": "9" * 60},
    {"loc": "yakima", "product_id": "1", "qty": "-" + "9" * 60},
    {"loc": "yakima", "product_id": "1", "qty": "1e309"},
    {"loc": "yakima", "product_id": "1", "qty": "NaN"},
    {"loc": "yakima", "product_id": "1", "qty": "Infinity"},
    {"loc": "yakima", "product_id": "1", "qty": ["1", "12"]},
    {"loc": "yakima", "product_id": ["1", "10"], "qty": "1"},
    {"loc": "yakima", "product_id": "", "qty": "1"},
    {"loc": "yakima", "product_id": "does-not-exist", "qty": "1"},
    {"loc": "yakima", "product_id": "🌿" * 200, "qty": "1"},
    {"loc": "yakima", "product_id": "Ω≈ç√∫˜µ 🔥", "qty": "🔥"},
    {"loc": "yakima", "product_id": "A" * 20000, "qty": "1"},
    {"loc": "yakima", "product_id": "1' OR '1'='1", "qty": "1"},
    {"loc": "yakima", "product_id": "'; DROP TABLE budtender_phonecartdraft; --", "qty": "1"},
    {"loc": "yakima", "product_id": "1; SELECT pg_sleep(10)--", "qty": "1"},
    {"loc": "yakima", "product_id": "<script>alert(1)</script>", "qty": "1"},
    {"loc": "yakima", "product_id": "1", "qty": "<img src=x onerror=alert(1)>"},
    {"loc": "yakima", "product_id": "1\x00", "qty": "1"},
    {"loc": "../../etc/passwd", "product_id": "1", "qty": "1"},
    {"loc": "yakima" * 100, "product_id": "1", "qty": "1"},
    {"loc": "{{7*7}}", "product_id": "{{7*7}}", "qty": "{{7*7}}"},
]

CHECKOUT_ABUSE = [
    {},
    {"loc": "yakima"},
    {"loc": "yakima", "name": "S"},
    {"loc": "yakima", "name": "Sam", "phone": ""},
    {"loc": "yakima", "name": "Sam", "phone": "abc"},
    {"loc": "yakima", "name": "Sam", "phone": "-" + "9" * 40},
    {"loc": "yakima", "name": "Sam", "phone": "5095551212", "email": "not-an-email"},
    {"loc": "yakima", "name": "Sam", "phone": "5095551212", "email": "a@b@c.com"},
    {"loc": "yakima", "name": "Sam", "phone": "5095551212", "email": "x" * 400 + "@x.com"},
    {"loc": "yakima", "name": "A" * 20000, "phone": "1"},
    {"loc": "yakima", "name": "🌿🔥" * 300, "phone": "1"},
    {"loc": "yakima", "name": "Robert'); DROP TABLE customers;--", "phone": "1"},
    {"loc": "yakima", "name": "<script>alert(1)</script>", "phone": "1"},
    {"loc": "yakima", "name": ["Sam", "Mallory"], "phone": ["1", "notaphone"]},
    {"loc": "yakima", "name": "{{7*7}}", "phone": "{% raw %}", "email": "{{7*7}}@x.com"},
]


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2,
                   BUNDLE_MAX_ORDER_TOTAL=300)
class InputAbuseTests(PublicSurfaceTestCase):
    """Hostile bodies on every POST. The bar is: a clean 4xx or a sane clamp,
    never a 500, and never a stored payload that executes on someone later."""

    def setUp(self):
        super().setUp()
        # Return the 500 instead of re-raising it, so a crash shows up as a failed
        # assertion naming the payload rather than an opaque traceback.
        self.client.raise_request_exception = False

    def _assert_sane(self, r, path, payload):
        self.assertIn(r.status_code, (200, 400, 405, 429),
                      f"{path} returned {r.status_code} for {payload!r}")

    def test_cart_add_survives_every_hostile_body(self):
        for payload in CART_ABUSE:
            with self.subTest(payload=str(payload)[:80]), self._patch_inv():
                self._assert_sane(self.client.post("/custom-order/cart/add", payload),
                                  "cart/add", payload)

    def test_cart_update_survives_every_hostile_body(self):
        for payload in CART_ABUSE:
            with self.subTest(payload=str(payload)[:80]), self._patch_inv():
                self._assert_sane(self.client.post("/custom-order/cart/update", payload),
                                  "cart/update", payload)

    def test_cart_remove_survives_every_hostile_body(self):
        for payload in CART_ABUSE:
            with self.subTest(payload=str(payload)[:80]), self._patch_inv():
                self._assert_sane(self.client.post("/custom-order/cart/remove", payload),
                                  "cart/remove", payload)

    def test_checkout_survives_every_hostile_body(self):
        self._add("1")
        for payload in CHECKOUT_ABUSE:
            with self.subTest(payload=str(payload)[:80]), self._patch_inv():
                self._assert_sane(self.client.post("/custom-order/checkout", payload),
                                  "checkout", payload)

    def test_nothing_hostile_is_persisted_into_a_cart_line(self):
        # The client sends an id and a quantity; every stored field is copied from
        # the live register row. Prove no attacker string survived into `lines`.
        for payload in CART_ABUSE:
            with self._patch_inv():
                self.client.post("/custom-order/cart/add", payload)
        blob = str([d.lines for d in PhoneCartDraft.objects.all()])
        for needle in ("DROP TABLE", "<script", "onerror", "{{7*7}}", "OR '1'='1", "pg_sleep"):
            self.assertNotIn(needle, blob)

    def test_a_quantity_is_always_clamped_never_unbounded(self):
        for qty in ("9" * 60, "999999", "12", "-5", "0", "1.9"):
            with self.subTest(qty=qty):
                cache.clear()
                shopper = Client()
                self._add("1", qty=qty, client=shopper)
                draft = PhoneCartDraft.objects.filter(
                    draft_token=shopper.cookies[cart_mod.COOKIE].value).first()
                stored = draft.lines[0]["quantity"] if draft.lines else 0
                self.assertLessEqual(stored, cart_mod.MAX_QTY)
                self.assertGreaterEqual(stored, 0)

    def test_a_product_from_another_store_cannot_be_added(self):
        # Product 900 is Pullman-only. Naming it on a Yakima cart must be refused,
        # not quietly priced from Pullman's floor.
        with self._patch_stores():
            r = self.client.post("/custom-order/cart/add",
                                 {"loc": "yakima", "product_id": "900", "qty": 1})
        self.assertEqual(r.status_code, 200)
        self.assertIn("sold out", r.content.decode().lower())
        self.assertEqual(PhoneCartDraft.objects.get().lines, [])

    def test_a_bogus_store_narrows_to_the_default_it_never_widens(self):
        # `_store_from` defaults an unknown `loc` to Yakima. Prove the fallback is
        # a narrowing, not a way to reach another store's catalogue.
        with self._patch_stores():
            r = self.client.post("/custom-order/cart/add",
                                 {"loc": "pullman/../", "product_id": "900", "qty": 1})
        self.assertIn("sold out", r.content.decode().lower())
        self.assertEqual(PhoneCartDraft.objects.get().location_slug, "yakima")

    # ── XSS ──────────────────────────────────────────────────────────────────
    def test_xss_in_the_checkout_form_comes_back_escaped(self):
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "<script>alert(1)</script>",
                                  "phone": "123"})
        body = r.content.decode()
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_xss_in_the_pickup_name_comes_back_escaped_on_the_success_page(self):
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "<img src=x onerror=alert(1)>",
                                  "phone": "5095551212"})
        body = r.content.decode()
        self.assertContains(r, "Order placed")
        self.assertNotIn("<img src=x onerror=alert(1)>", body)
        self.assertIn("&lt;img", body)

    def test_xss_in_a_search_filter_comes_back_escaped(self):
        with self._patch_inv():
            r = self.client.get('/custom-order/menu?loc=yakima'
                                '&q="><script>alert(1)</script>&brand_q=<b>x</b>')
        body = r.content.decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn("<b>x</b>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_xss_arriving_from_the_register_feed_comes_back_escaped(self):
        # The product name is Dutchie's data, not ours. A poisoned catalogue row
        # must not become script on a public page.
        inv = [leaky(product_id="1", name="<script>alert('pwn')</script> OG",
                     brand="<img src=x onerror=alert(2)>")]
        with self._patch_inv(inv):
            r = self.client.get("/custom-order/results?loc=yakima")
        body = r.content.decode()
        self.assertNotIn("<script>alert('pwn')</script>", body)
        self.assertNotIn("<img src=x onerror=alert(2)>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_an_image_url_from_the_feed_cannot_break_out_of_its_attribute(self):
        inv = [leaky(product_id="1", img='" onerror="alert(1)')]
        with self._patch_inv(inv):
            body = self.client.get("/custom-order/results?loc=yakima").content.decode()
        self.assertNotIn('onerror="alert(1)"', body)
        self.assertNotIn('" onerror=', body)

    # ── regression guards for two holes this file found ──────────────────────
    # Both were live when these tests were first written and are fixed in
    # bundles/views.py now (`_CONTROL_RE`, `_PHONE_RE`); these pin the fixes.
    def test_a_null_byte_in_the_name_never_reaches_the_insert(self):
        # WAS: `checkout` wrote `name` straight to a Postgres text column and
        # psycopg rejects NUL — django.db.utils.DataError, i.e. an unauthenticated
        # 500 anyone could trigger at will, AFTER the shopper thought they'd ordered.
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "Sam\x00Reyes", "phone": "5095551212"})
        self.assertLess(r.status_code, 500, "a NUL byte in `name` crashed checkout")
        draft = PhoneCartDraft.objects.get()
        self.assertNotIn("\x00", draft.pickup_name)
        self.assertTrue(draft.pickup_name.isprintable())

    def test_a_null_byte_in_the_email_never_reaches_the_insert(self):
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "Sam Reyes", "phone": "5095551212",
                                  "email": "sa\x00m@example.com"})
        self.assertLess(r.status_code, 500, "a NUL byte in `email` crashed checkout")
        self.assertNotIn("\x00", PhoneCartDraft.objects.get().contact_email)

    def test_every_c0_control_character_is_stripped_from_the_contact_fields(self):
        # The order must still GO THROUGH — asserting only "not a 500" would pass
        # for the wrong reason if the controls instead tripped email validation.
        controls = "".join(chr(c) for c in list(range(0x00, 0x20)) + [0x7f])
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": f"Sam{controls}Reyes",
                                  "phone": "5095551212",
                                  "email": "sam\x00\x01\x7f@example.com"})
        self.assertEqual(r.status_code, 200)
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.pickup_name, "SamReyes")
        self.assertEqual(draft.contact_email, "sam@example.com")
        for char in controls:
            self.assertNotIn(char, draft.pickup_name)
            self.assertNotIn(char, draft.contact_email)

    def test_a_phone_must_be_ten_ascii_digits(self):
        # WAS: `_clean_phone` stripped `\D`, which under Python's Unicode semantics
        # KEEPS Arabic-Indic digits — so '٥٠٩٥٥٥١٢١٢' passed the 10-digit check and
        # the order reached the staff queue with a number nobody could dial, and a
        # phone_hash / Dutchie guest_search keyed on non-ASCII.
        self._add("1")
        with self._patch_inv():
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "Sam Reyes", "phone": "٥٠٩٥٥٥١٢١٢"})
        self.assertEqual(r.status_code, 400)
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.status, PhoneCartDraft.Status.OPEN)
        self.assertEqual(draft.contact_phone, "")

    def test_a_released_phone_is_always_ten_ascii_digits(self):
        # The positive half: whatever shape a real shopper types, what lands in the
        # staff queue is dialable.
        for typed in ("509 555 1212", "+1 (509) 555-1212", "15095551212", "509.555.1212"):
            with self.subTest(typed=typed):
                cache.clear()
                shopper = Client()
                self._add("1", client=shopper)
                with self._patch_inv():
                    shopper.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "Sam Reyes", "phone": typed})
                draft = PhoneCartDraft.objects.filter(
                    status=PhoneCartDraft.Status.RELEASED).order_by("-id").first()
                self.assertEqual(draft.contact_phone, "5095551212")
                self.assertTrue(draft.phone_last4.isascii())


# ── 5. rate limits ───────────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class RateLimitTests(PublicSurfaceTestCase):
    """`@rate_limit` is only a control if it is actually wired to these views.

    The clock is frozen for the whole loop — `rate_limit` buckets on
    `int(time.time() // window)`, so a real clock lets a long loop roll into the
    next bucket and reset the counter, which would make these tests pass by luck.
    """

    def _frozen(self):
        return patch("pos_core.ratelimit.time.time", return_value=FROZEN)

    def _spam_cart(self, n):
        for _ in range(n):
            self.client.post("/custom-order/cart/add",
                             {"loc": "yakima", "product_id": "1", "qty": 1})

    def test_the_cart_throttle_engages_at_its_limit(self):
        with self._frozen(), self._patch_inv():
            codes = [self.client.post("/custom-order/cart/add",
                                      {"loc": "yakima", "product_id": "1", "qty": 1}).status_code
                     for _ in range(241)]
        self.assertEqual(codes[239], 200)
        self.assertEqual(codes[240], 429)
        self.assertEqual(codes.count(429), 1)

    def test_the_cart_throttle_is_shared_across_add_update_and_remove(self):
        # One scope ("bundle-cart") — otherwise an abuser just rotates endpoints.
        with self._frozen(), self._patch_inv():
            self._spam_cart(240)
            for path in ("/custom-order/cart/add", "/custom-order/cart/update",
                         "/custom-order/cart/remove"):
                r = self.client.post(path, {"loc": "yakima", "product_id": "1"})
                self.assertEqual(r.status_code, 429, path)

    def test_a_spoofed_forwarded_for_hop_cannot_dodge_the_cart_throttle(self):
        # Traefik APPENDS the real client IP, so the LAST hop is the trustworthy
        # one. A client-supplied first hop must not open a fresh bucket.
        with self._frozen(), self._patch_inv():
            self._spam_cart(240)
            r = self.client.post("/custom-order/cart/add",
                                 {"loc": "yakima", "product_id": "1", "qty": 1},
                                 HTTP_X_FORWARDED_FOR="9.9.9.9, 127.0.0.1")
        self.assertEqual(r.status_code, 429)

    def test_the_checkout_throttle_engages_at_its_limit(self):
        with self._frozen(), self._patch_inv():
            codes = [self.client.get("/custom-order/checkout?loc=yakima").status_code
                     for _ in range(21)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)

    def test_the_checkout_throttle_covers_post_not_just_get(self):
        # 20 order attempts an hour per IP — not 20 page views plus unlimited
        # submits. The submit is the expensive half (Dutchie lookup, held stock).
        self._add("1")
        with self._frozen(), self._patch_inv():
            for _ in range(20):
                self.client.get("/custom-order/checkout?loc=yakima")
            r = self.client.post("/custom-order/checkout",
                                 {"loc": "yakima", "name": "Sam Reyes", "phone": "5095551212"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(PhoneCartDraft.objects.get().status, PhoneCartDraft.Status.OPEN)

    def test_the_cart_and_checkout_buckets_are_independent(self):
        # Browsing hard must not lock a shopper out of placing their order.
        with self._frozen(), self._patch_inv():
            self._spam_cart(241)
            self.assertEqual(self.client.get("/custom-order/checkout?loc=yakima").status_code, 200)

    def test_the_throttle_response_leaks_nothing(self):
        with self._frozen(), self._patch_inv():
            self._spam_cart(241)
            r = self.client.post("/custom-order/cart/add",
                                 {"loc": "yakima", "product_id": "1", "qty": 1})
        self.assertEqual(r.status_code, 429)
        self.assertNoLeak(r, "429 body")


# ── 6. method + auth boundary ────────────────────────────────────────────────
PUBLIC_GET = ("/custom-order/menu?loc=yakima", "/custom-order/results?loc=yakima",
              "/custom-order/cart?loc=yakima", "/custom-order/checkout?loc=yakima")
POST_ONLY = ("/custom-order/cart/add", "/custom-order/cart/update", "/custom-order/cart/remove")
GET_ONLY = ("/custom-order/", "/custom-order/menu", "/custom-order/results", "/custom-order/cart")

# Staff screens that neighbour the storefront in the SAME root namespace: `/` and
# `/custom-order/` are one `include()` apart in core/urls.py, so this is exactly
# the boundary a mis-ordered route would erase.
POS_GATED = ("/", "/pos/", "/menu/", "/queue/", "/door/", "/sessions/", "/insights/",
             "/shifts/", "/customer/")


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2)
class MethodAndAuthBoundaryTests(PublicSurfaceTestCase):
    def test_post_only_cart_routes_reject_every_other_verb(self):
        for path in POST_ONLY:
            for verb in ("get", "put", "patch", "delete", "options"):
                with self.subTest(path=path, verb=verb):
                    self.assertEqual(getattr(self.client, verb)(path).status_code, 405,
                                     f"{verb.upper()} {path}")

    def test_get_only_routes_reject_post(self):
        for path in GET_ONLY:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, {}).status_code, 405)

    def test_checkout_accepts_get_and_post_and_nothing_else(self):
        with self._patch_inv():
            self.assertEqual(self.client.get("/custom-order/checkout?loc=yakima").status_code, 200)
        for verb in ("put", "patch", "delete"):
            with self.subTest(verb=verb):
                self.assertEqual(getattr(self.client, verb)("/custom-order/checkout").status_code,
                                 405)

    def test_a_rejected_verb_does_not_burn_the_rate_limit_budget(self):
        # `require_POST` wraps `rate_limit`, so a GET flood cannot exhaust a real
        # shopper's quota. Cheap to get backwards, expensive in production.
        with patch("pos_core.ratelimit.time.time", return_value=FROZEN):
            for _ in range(300):
                self.client.get("/custom-order/cart/add")
            with self._patch_inv():
                r = self.client.post("/custom-order/cart/add",
                                     {"loc": "yakima", "product_id": "1", "qty": 1})
        self.assertEqual(r.status_code, 200)

    def test_no_public_route_bounces_an_anonymous_shopper_to_a_login(self):
        with self._patch_inv():
            pages = [self.client.get(p) for p in PUBLIC_GET]
            pages.append(self.client.get(self._signed()))
            pages.append(self.client.post("/custom-order/cart/add",
                                          {"loc": "yakima", "product_id": "1", "qty": 1}))
            pages.append(self.client.post("/custom-order/checkout",
                                          {"loc": "yakima", "name": "", "phone": ""}))
        for r in pages:
            path = r.request["PATH_INFO"]
            self.assertNotEqual(r.status_code, 302, f"{path} redirected")
            self.assertNotIn("login", (r.headers.get("Location") or "").lower(), path)

    def test_the_neighbouring_pos_screens_still_require_a_login(self):
        for path in POS_GATED:
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 302, f"{path} is not login-gated")
                self.assertIn("/login/", r.headers.get("Location", ""))

    def test_the_pos_claim_endpoint_is_not_reachable_anonymously(self):
        # This is the view that pulls an online order into the register cart. If
        # the public storefront ever shadowed it, anyone could claim a stranger's
        # order — so assert the gate AND that the draft is untouched.
        draft = PhoneCartDraft.objects.create(
            location_slug="yakima", status=PhoneCartDraft.Status.RELEASED,
            pickup_name="Sam", lines=[{"product_id": "1", "quantity": 1}])
        r = self.client.post("/phone-cart/claim/", {"token": draft.draft_token,
                                                    "draft_token": draft.draft_token})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r.headers.get("Location", ""))
        draft.refresh_from_db()
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)

    def test_the_public_storefront_is_marked_noindex(self):
        # Live pricing and a shopper's cart have no business in a search index.
        with self._patch_inv():
            body = self.client.get("/custom-order/menu?loc=yakima").content.decode()
        self.assertIn('name="robots" content="noindex, nofollow"', body)
