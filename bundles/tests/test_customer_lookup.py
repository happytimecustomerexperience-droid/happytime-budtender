"""The public customer lookup: /custom-order/lookup-customer.

This endpoint takes a phone number and hands back a real person's NAME, with no
login in front of it, for a cannabis dispensary. It is a PII enumeration oracle by
construction, so the tests here are mostly about what it must REFUSE to be:

  1. CHEAP TO ASK — no. The throttle is the control; both windows are asserted,
     including the slow drip that stays under the per-minute cap.
  2. CHATTY — no. The response key set is pinned exactly, so a future change that
     passes a Dutchie row through (AcctId, DOB, email, points) fails loudly here
     rather than quietly on the storefront.
  3. AN INFORMATION CHANNEL WHEN IT BREAKS — no. Dutchie down, a garbage phone and
     a genuine "no account" are indistinguishable to the caller: `{"found": false}`,
     HTTP 200. A 500, or a distinguishable error, tells a prober which numbers are
     real even while the lookup is broken.

Nothing here touches the network: `bundles.customers.lookup_by_phone` is patched in
every test, and the tests that must NOT reach Dutchie assert on the mock directly.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from budtender.models import PhoneCartDraft
from bundles import signing, views
from bundles.tests.test_views import SECRET, inventory

URL = "/custom-order/lookup-customer"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# `rate_limit` buckets on int(time.time() // window); a real clock lets a loop roll
# into the next bucket and reset the counter, so a throttle test would pass by luck.
FROZEN = 1_700_000_000.0

# What lookup_by_phone actually returns: (acct_id, name, status). The AcctId carries
# a canary — it is the field most likely to be leaked by a careless "just return
# everything we got" refactor, and it identifies a customer record in Dutchie.
MATCH = ("acct-canary-8814", "Sam Reyes", PhoneCartDraft.Customer.MATCHED)
NO_MATCH = ("", "", PhoneCartDraft.Customer.NEW)
DUTCHIE_DOWN = ("", "", PhoneCartDraft.Customer.UNRESOLVED)


@override_settings(CACHES=CACHES_LOCMEM)
class CustomerLookupTests(TestCase):
    """LocMemCache is process-global — clear it coming AND going, or a primed
    rate-limit bucket leaks into whatever class happens to run next."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()
        p = patch("bundles.customers.lookup_by_phone", return_value=NO_MATCH)
        self.lookup = p.start()
        self.addCleanup(p.stop)

    def _post(self, phone="5095551212", **extra):
        return self.client.post(URL, {"phone": phone, **extra})

    # ── the happy path ───────────────────────────────────────────────────────
    def test_the_route_is_wired_under_its_name(self):
        self.assertEqual(reverse("bundle_lookup_customer"), URL)

    def test_a_known_number_comes_back_with_the_name(self):
        self.lookup.return_value = MATCH
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"found": True, "first_name": "Sam", "last_name": "Reyes"})

    def test_an_unknown_number_comes_back_not_found(self):
        self.lookup.return_value = NO_MATCH
        self.assertEqual(self._post().json(), {"found": False})

    def test_a_phone_is_normalised_before_it_reaches_dutchie(self):
        # Whatever shape the shopper types is the same ten digits by the time it
        # is a lookup — otherwise "+1 (509) 555-1212" misses their own account.
        for typed in ("509 555 1212", "+1 (509) 555-1212", "15095551212", "509.555.1212"):
            with self.subTest(typed=typed):
                cache.clear()
                self.lookup.reset_mock()
                self._post(typed)
                self.assertEqual(self.lookup.call_args.args[1], "5095551212")

    def test_the_lookup_uses_the_store_the_shopper_is_browsing(self):
        # Guests are per-store in Dutchie; asking Yakima about a Pullman shopper is
        # a false "no account" and a duplicate guest record at claim time.
        self._post(loc="pullman")
        self.assertEqual(self.lookup.call_args.args[0], "pullman")

    # ── it must never become an oracle ───────────────────────────────────────
    def test_the_body_carries_only_the_allowlisted_keys(self):
        # Pinned EXACTLY. If someone later returns the raw Dutchie guest row, this
        # is the test that stops the DOB, the email and the address going public.
        self.lookup.return_value = MATCH
        self.assertEqual(set(self._post().json()), {"found", "first_name", "last_name"})
        cache.clear()
        self.lookup.return_value = NO_MATCH
        self.assertEqual(set(self._post().json()), {"found"})

    def test_the_dutchie_account_id_never_reaches_the_wire(self):
        self.lookup.return_value = MATCH
        self.assertNotIn("acct-canary-8814", self._post().content.decode())

    def test_a_matched_row_with_no_usable_name_is_not_a_match(self):
        # "We found your profile" over two empty boxes is worse than not asking.
        self.lookup.return_value = ("acct-canary-8814", "   ", PhoneCartDraft.Customer.MATCHED)
        self.assertEqual(self._post().json(), {"found": False})

    def test_a_single_token_name_still_fills_what_it_knows(self):
        self.lookup.return_value = ("acct-canary-8814", "Cher", PhoneCartDraft.Customer.MATCHED)
        self.assertEqual(self._post().json(),
                         {"found": True, "first_name": "Cher", "last_name": ""})

    # ── failure is indistinguishable from "no account" ───────────────────────
    def test_dutchie_raising_is_a_200_and_a_no_match(self):
        # A 500 here would both break the checkout page and, by its own existence,
        # confirm to a prober that the number reached a real lookup.
        self.lookup.side_effect = RuntimeError("register unreachable")
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"found": False})

    def test_dutchies_own_unresolved_status_is_a_no_match(self):
        self.lookup.return_value = DUTCHIE_DOWN
        self.assertEqual(self._post().json(), {"found": False})

    def test_a_short_or_garbage_phone_never_reaches_dutchie(self):
        # Free lookups are the whole game for an enumerator: reject the shape
        # BEFORE spending a register call, not after.
        for payload in ("", "12345", "abcdefghij", "555-121", "9" * 40,
                        "٥٠٩٥٥٥١٢١٢", "<script>alert(1)</script>", "null", " "):
            with self.subTest(payload=payload[:24]):
                cache.clear()
                self.lookup.reset_mock()
                r = self._post(payload)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json(), {"found": False})
                self.assertFalse(self.lookup.called,
                                 f"{payload!r} was allowed to spend a Dutchie lookup")

    def test_a_missing_phone_field_is_a_no_match_not_a_crash(self):
        r = self.client.post(URL, {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"found": False})
        self.assertFalse(self.lookup.called)

    # ── method and CSRF ──────────────────────────────────────────────────────
    def test_get_is_refused(self):
        # GET would be linkable, prefetchable and loggable in the referrer chain.
        r = self.client.get(URL, {"phone": "5095551212"})
        self.assertEqual(r.status_code, 405)
        self.assertFalse(self.lookup.called)

    def test_csrf_is_enforced(self):
        # Deliberately NOT csrf_exempt, unlike the cart routes: those move a product
        # id, this one reads a customer's identity, so it stays same-origin only.
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(URL, {"phone": "5095551212"}).status_code, 403)

    # ── the throttle ─────────────────────────────────────────────────────────
    def _frozen(self, clock):
        return patch("pos_core.ratelimit.time.time", side_effect=lambda: clock[0])

    def test_the_per_minute_throttle_engages_at_its_limit(self):
        clock = [FROZEN]
        with self._frozen(clock):
            codes = [self._post().status_code for _ in range(views.LOOKUP_PER_MINUTE + 1)]
        self.assertEqual(codes[:views.LOOKUP_PER_MINUTE], [200] * views.LOOKUP_PER_MINUTE)
        self.assertEqual(codes[-1], 429)

    def test_the_hourly_throttle_catches_a_slow_drip(self):
        # The per-minute cap alone is not a control: one number a minute is 60 names
        # an hour, 1,400 a day, from one IP, without ever tripping it.
        clock = [FROZEN]
        codes = []
        with self._frozen(clock):
            for _ in range(views.LOOKUP_PER_HOUR + 1):
                codes.append(self._post().status_code)
                clock[0] += 61   # a fresh minute bucket every time
        self.assertEqual(codes[:views.LOOKUP_PER_HOUR], [200] * views.LOOKUP_PER_HOUR)
        self.assertEqual(codes[-1], 429)

    def test_a_throttled_lookup_never_reaches_dutchie(self):
        clock = [FROZEN]
        with self._frozen(clock):
            for _ in range(views.LOOKUP_PER_MINUTE):
                self._post()
            self.lookup.reset_mock()
            r = self._post()
        self.assertEqual(r.status_code, 429)
        self.assertFalse(self.lookup.called)

    def test_a_spoofed_forwarded_for_hop_cannot_dodge_the_throttle(self):
        # Traefik APPENDS the real client IP, so the LAST hop is the trustworthy
        # one. Rotating a client-supplied first hop must not open a fresh bucket.
        clock = [FROZEN]
        body = {"phone": "5095551212"}
        with self._frozen(clock):
            for i in range(views.LOOKUP_PER_MINUTE):
                self.client.post(URL, body, HTTP_X_FORWARDED_FOR=f"9.9.9.{i}, 127.0.0.1")
            r = self.client.post(URL, body, HTTP_X_FORWARDED_FOR="9.9.9.99, 127.0.0.1")
        self.assertEqual(r.status_code, 429)

    def test_the_lookup_throttle_does_not_lock_a_shopper_out_of_checkout(self):
        # Separate scopes: burning the lookup budget must not cost a real shopper
        # the ability to place the order they came for.
        clock = [FROZEN]
        with self._frozen(clock):
            for _ in range(views.LOOKUP_PER_MINUTE + 1):
                self._post()
            r = self.client.get("/custom-order/checkout?loc=yakima")
        self.assertEqual(r.status_code, 200)

    # ── logging ──────────────────────────────────────────────────────────────
    def test_the_log_line_carries_the_last_four_and_never_the_number(self):
        self.lookup.return_value = MATCH
        with self.assertLogs("bundles.views", level="INFO") as logged:
            self._post("5095551212")
        blob = "\n".join(logged.output)
        self.assertNotIn("5095551212", blob, "the full phone number reached the log")
        self.assertIn("1212", blob)


@override_settings(CACHES=CACHES_LOCMEM, BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2)
class LookupIsWiredIntoBothFormsTests(TestCase):
    """The endpoint is useless if the page never calls it.

    The order form exists TWICE — checkout.html and the inline copy on the bundle
    landing page — and the second one has been forgotten by a change to the first
    before. bundle.js finds the form by `data-lookup-url` and writes into `.lookup`,
    so both attributes are the contract between the template and the script; a
    template edit that drops either kills the feature silently, on a page that still
    renders and still takes orders. Assert on the RENDERED body, not the source.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

    def _patch_inv(self):
        return patch("bundles.cart.pos_catalog.get_inventory", return_value=inventory())

    def assertHooked(self, response, where):
        body = response.content.decode()
        self.assertIn('data-lookup-url="/custom-order/lookup-customer"', body,
                      f"{where}: bundle.js can't find the form to look anyone up from")
        self.assertIn('class="lookup"', body, f"{where}: nowhere to show the result")

    def test_the_checkout_form_is_hooked_up(self):
        with self._patch_inv():
            self.client.post("/custom-order/cart/add",
                             {"loc": "yakima", "product_id": "1", "qty": 1})
            self.assertHooked(self.client.get("/custom-order/checkout?loc=yakima"), "checkout")

    def test_the_inline_form_on_the_bundle_landing_page_is_hooked_up(self):
        url = signing.build_url("/custom-order/", bundle="roll-relax", store="yakima",
                                items=[("1", 1), ("10", 2), ("20", 1)])
        with self._patch_inv():
            self.assertHooked(self.client.get(url), "bundle landing")
