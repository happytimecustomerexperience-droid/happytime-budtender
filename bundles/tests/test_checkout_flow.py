"""The /custom-order checkout: the one place a shopper turns a cart into an order.

Everything downstream trusts what this view writes — a budtender claims the draft at
the register and sells exactly these lines to exactly this person. So the properties
pinned here are the ones that cost money or trust when they break:

  * a shopper can never talk the server into a price (every line is repriced from
    LIVE inventory at submit time; the POST body is name/phone/email and nothing else)
  * an order is never confirmed for something that sold out while they typed
  * a Dutchie outage degrades, it never loses the order
  * a dead SMTP server never becomes a failed checkout
  * a double-submit is one order, not two

The fixtures read like a real Yakima order for 509-420-6999 so a failure message
points at a scenario rather than at a row of placeholder data.

Nothing here touches the network: `bundles.customers._client` (the PosRegisterClient
factory) is patched for EVERY test in this module from setUp, not per-test.
"""
from unittest.mock import MagicMock, patch

from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from budtender.models import PhoneCartDraft, Product
from bundles import cart as cart_mod
from bundles import customers, signing
from bundles.views import _clean_phone
from bundles.tests.test_resolver import live

SECRET = "unit-test-secret-value"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
SMTP = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "DEFAULT_FROM_EMAIL": "orders@happytimeweed.com",
}

# One real-looking shopper, written every way a phone reaches us.
PHONE = "5094206999"
PHONE_VARIANTS = ("5094206999", "509-420-6999", "(509) 420-6999", "+15094206999", "15094206999")
NAME = "Sam Reyes"
EMAIL = "sam.reyes@example.com"

# Staff-only signals that must never reach a shopper — page or email.
FORBIDDEN = ("margin_pct", "velocity", "price_z", "bucket", "serialno", "batchid",
             "recunitprice", "cannbisproduct", "stock_on_hand", "quote_source",
             "dutchie_acct_id", "package_id")


def inventory():
    """Live rows in `pos.catalog._normalize` shape — see test_resolver.live()."""
    return [
        live(product_id="1", name="Blue Dream 3.5g", brand="Athenry", price=25.0, qty=10),
        live(product_id="2", name="OG Kush 3.5g", brand="Fireline", price=27.0, qty=10),
        live(product_id="10", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="Sunset Pre-Roll", brand="Athenry", unit_grams=1.0, price=8.0, qty=10),
        live(product_id="11", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="Daybreak Pre-Roll", brand="Athenry", unit_grams=1.0, price=9.0, qty=10),
        live(product_id="20", cat_key="edibles", cat_label="Edibles", subcategory="10pk",
             name="Marionberry Gummies", brand="Wyld", unit_grams=None, price=15.0, qty=10),
    ]


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2,
                   BUNDLE_MAX_ORDER_TOTAL=300)
class CheckoutFlowTestCase(TestCase):
    """Shared rig: a clean cache, a fresh cart, and Dutchie permanently stubbed."""

    def setUp(self):
        # LocMemCache is process-global — the rate limiter's buckets and any primed
        # state leak into other classes if this isn't cleared on both edges.
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

        # ALWAYS patched, for every test in this module: bundles/customers.py reaches
        # Dutchie through this factory, and an unpatched test would hit the network.
        self.real_client_factory = customers._client      # captured before we hide it
        self.dutchie = MagicMock(name="PosRegisterClient")
        self.dutchie.guest_search.return_value = {"Data": []}
        factory = patch("bundles.customers._client", return_value=self.dutchie)
        self.dutchie_factory = factory.start()
        self.addCleanup(factory.stop)

    # ── seams ────────────────────────────────────────────────────────────────
    def _patch_inv(self, inv=None):
        """Patch live inventory. `bundles.cart` holds the module, so patching the
        attribute there covers `bundles.views` too — one seam, every reader."""
        return patch("bundles.cart.pos_catalog.get_inventory",
                     return_value=inventory() if inv is None else inv)

    # ── actions ──────────────────────────────────────────────────────────────
    def _add(self, product_id="1", qty=1, loc="yakima", inv=None, **extra):
        with self._patch_inv(inv):
            payload = {"loc": loc, "product_id": product_id, "qty": qty}
            payload.update(extra)
            return self.client.post("/custom-order/cart/add", payload)

    def _checkout(self, inv=None, **over):
        payload = {"loc": "yakima", "name": NAME, "phone": PHONE, "email": EMAIL}
        payload.update(over)
        with self._patch_inv(inv):
            return self.client.post("/custom-order/checkout", payload)

    def _get_checkout(self, inv=None, loc="yakima"):
        with self._patch_inv(inv):
            return self.client.get(f"/custom-order/checkout?loc={loc}")

    # ── assertions ───────────────────────────────────────────────────────────
    def _released(self):
        return PhoneCartDraft.objects.filter(status=PhoneCartDraft.Status.RELEASED)

    def assertNoOrderPlaced(self, response):
        """A rejected checkout: never a 500, and never an order in the staff queue."""
        self.assertNotEqual(response.status_code, 500)
        self.assertLess(response.status_code, 500)
        self.assertFalse(self._released().exists(), "a rejected checkout released an order")
        for draft in PhoneCartDraft.objects.all():
            self.assertEqual(draft.status, PhoneCartDraft.Status.OPEN)
            self.assertIsNone(draft.released_at)
            self.assertEqual(draft.contact_phone, "")
            self.assertEqual(draft.pickup_name, "")


# ── 1. GET ───────────────────────────────────────────────────────────────────
class CheckoutGetTests(CheckoutFlowTestCase):
    def test_renders_the_form_with_the_current_cart(self):
        self._add("1")                       # 25.00
        self._add("10", qty=2)               # 8.00 x2
        r = self._get_checkout()
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Blue Dream 3.5g", body)
        self.assertIn("Sunset Pre-Roll", body)
        self.assertIn('name="name"', body)
        self.assertIn('name="phone"', body)
        self.assertIn('name="email"', body)

    def test_shows_the_store_name_and_the_live_total(self):
        self._add("1")
        self._add("10", qty=2)
        body = self._get_checkout().content.decode()
        self.assertIn("Happy Time — Yakima", body)
        self.assertIn("41.00", body)          # 25 + 8x2, priced server-side

    def test_the_total_follows_live_inventory_not_the_stored_line(self):
        # The cart was priced at $25 when it was built; the floor says $9 now.
        self._add("1")
        cheaper = [dict(p, price=9.0) if p["product_id"] == "1" else p for p in inventory()]
        body = self._get_checkout(inv=cheaper).content.decode()
        self.assertIn("9.00", body)
        self.assertNotIn("25.00", body)

    def test_empty_cart_shows_an_actionable_empty_state_not_a_form(self):
        r = self._get_checkout()             # no cookie at all
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your cart is empty", body)
        self.assertIn("Browse the menu", body)      # a way out, not a dead end
        self.assertNotIn('name="phone"', body)

    def test_a_cart_emptied_before_arriving_shows_the_empty_state(self):
        self._add("1")
        with self._patch_inv():
            self.client.post("/custom-order/cart/remove", {"loc": "yakima", "product_id": "1"})
        self.assertIn("Your cart is empty", self._get_checkout().content.decode())

    def test_get_never_leaks_staff_signals(self):
        self._add("1")
        body = self._get_checkout().content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body)

    def test_get_never_touches_dutchie(self):
        self._add("1")
        self._get_checkout()
        self.dutchie_factory.assert_not_called()


# ── 2. POST validation ───────────────────────────────────────────────────────
class CheckoutValidationTests(CheckoutFlowTestCase):
    def test_a_phone_alone_places_the_order(self):
        # Phone is the identity, exactly as it is for an order placed by calling the
        # shop: it resolves to a Dutchie guest, so the budtender finds it the same
        # way. Requiring a name as well was friction with nothing behind it.
        self._add("1")
        r = self._checkout(name="")
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(PhoneCartDraft.objects.filter(
            status=PhoneCartDraft.Status.RELEASED).count(), 1)

    def test_an_unnamed_order_still_gets_a_callable_label(self):
        # A blank row in the pickup queue is one nobody can call out, so fall back
        # to the number when Dutchie has no name for it either.
        self._add("1")
        self._checkout(name="")
        draft = PhoneCartDraft.objects.get(status=PhoneCartDraft.Status.RELEASED)
        self.assertTrue(draft.pickup_name.strip(), "pickup_name must never be blank")
        self.assertIn(draft.contact_phone[-4:], draft.pickup_name)

    def test_a_supplied_name_is_kept(self):
        self._add("1")
        self._checkout(name="Sam Reyes")
        self.assertEqual(PhoneCartDraft.objects.get(
            status=PhoneCartDraft.Status.RELEASED).pickup_name, "Sam Reyes")

    def test_missing_phone_is_a_field_error(self):
        self._add("1")
        r = self._checkout(phone="")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Please enter a 10-digit phone number.", r.content.decode())
        self.assertNoOrderPlaced(r)

    def test_malformed_phones_are_rejected_without_a_500(self):
        self._add("1")
        for bad in ("123", "509420699", "50942069991234", "not a phone", "()-  -"):
            with self.subTest(phone=bad):
                r = self._checkout(phone=bad)
                self.assertEqual(r.status_code, 400, f"{bad!r} was accepted")
                self.assertIn("10-digit phone number", r.content.decode())
        self.assertNoOrderPlaced(r)

    def test_bad_email_is_rejected_and_the_form_is_redisplayed_with_the_name(self):
        self._add("1")
        r = self._checkout(email="not-an-email")
        body = r.content.decode()
        self.assertEqual(r.status_code, 400)
        self.assertIn("look right", body)
        self.assertIn(NAME, body)            # they don't have to retype it
        self.assertNoOrderPlaced(r)

    def test_an_empty_cart_at_submit_time_places_nothing(self):
        # Cart emptied in another tab between loading the form and pressing Place order.
        self._add("1")
        with self._patch_inv():
            self.client.post("/custom-order/cart/remove", {"loc": "yakima", "product_id": "1"})
        r = self._checkout()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your cart is empty", r.content.decode())
        self.assertNoOrderPlaced(r)

    def test_a_post_with_no_cart_at_all_is_not_a_500(self):
        r = self._checkout()                 # never added anything, no cookie
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your cart is empty", r.content.decode())
        self.assertEqual(PhoneCartDraft.objects.count(), 0)

    def test_a_rejected_checkout_never_touches_dutchie_or_email(self):
        self._add("1")
        with override_settings(**SMTP):
            self._checkout(phone="")   # phone is the only required field now
        self.dutchie_factory.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    def test_the_error_page_never_leaks_staff_signals(self):
        self._add("1")
        body = self._checkout(phone="").content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body)


# ── 3. phone normalisation ───────────────────────────────────────────────────
class PhoneNormalisationTests(CheckoutFlowTestCase):
    """Every shape of 509-420-6999 must be ONE customer.

    Three implementations have to agree or personalisation and guest matching drift:
    `views._clean_phone` (what we store), `customers._digits` (what we search Dutchie
    with) and `signing.customer_token` (the opaque handle in emailed links).
    """

    def test_the_three_normalisers_agree_on_every_shape(self):
        for raw in PHONE_VARIANTS:
            with self.subTest(phone=raw):
                self.assertEqual(_clean_phone(raw), PHONE)
                self.assertEqual(customers._digits(raw), PHONE)
        tokens = {signing.customer_token(raw) for raw in PHONE_VARIANTS}
        self.assertEqual(len(tokens), 1, "the same person minted more than one token")

    def test_every_shape_stores_the_same_customer(self):
        seen_phone, seen_hash, seen_last4, searched = set(), set(), set(), []
        for raw in PHONE_VARIANTS:
            with self.subTest(phone=raw):
                self._add("1")
                r = self._checkout(phone=raw)
                self.assertEqual(r.status_code, 200, f"{raw!r} was refused")
                draft = self._released().order_by("-created_at").first()
                seen_phone.add(draft.contact_phone)
                seen_hash.add(draft.phone_hash)
                seen_last4.add(draft.phone_last4)
        searched = [c.args[0] for c in self.dutchie.guest_search.call_args_list]

        self.assertEqual(seen_phone, {PHONE})
        self.assertEqual(seen_last4, {"6999"})
        self.assertEqual(len(seen_hash), 1, "one shopper produced several phone hashes")
        self.assertEqual(set(searched), {PHONE}, "Dutchie was searched with a raw phone shape")

    def test_the_same_guest_is_matched_whatever_shape_dutchie_stored(self):
        # Dutchie holds whatever shape the guest was created with; a string compare
        # would fail to recognise the customer standing at the counter.
        self.dutchie.guest_search.return_value = {
            "Data": [{"Guest_id": 4242, "Name": NAME, "PhoneNo": "(509) 420-6999"}]}
        for raw in PHONE_VARIANTS:
            with self.subTest(phone=raw):
                self._add("1")
                self._checkout(phone=raw)
        accts = set(self._released().values_list("dutchie_acct_id", flat=True))
        statuses = set(self._released().values_list("customer_status", flat=True))
        self.assertEqual(accts, {"4242"})
        self.assertEqual(statuses, {PhoneCartDraft.Customer.MATCHED})


# ── 4. happy path ────────────────────────────────────────────────────────────
class CheckoutHappyPathTests(CheckoutFlowTestCase):
    def test_the_open_cart_becomes_a_released_order_the_pos_can_claim(self):
        self._add("1")
        self._add("10", qty=2)
        cart_token = PhoneCartDraft.objects.get().draft_token

        r = self._checkout()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Order placed", r.content.decode())

        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.draft_token, cart_token, "a new row instead of releasing the cart")
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.source, PhoneCartDraft.Source.ONLINE)
        self.assertEqual(draft.location_slug, "yakima")
        self.assertEqual(draft.pickup_name, NAME)
        self.assertEqual(draft.contact_phone, PHONE)
        self.assertEqual(draft.contact_email, EMAIL)
        self.assertIsNotNone(draft.released_at)
        self.assertIsNotNone(draft.expires_at)

    def test_every_line_is_priced_from_live_inventory(self):
        self._add("1")                       # stored at 25.00
        self._add("10", qty=2)               # stored at 8.00
        # Both moved on the floor before they pressed Place order.
        moved = [dict(p, price={"1": 30.0, "10": 6.0}.get(p["product_id"], p["price"]))
                 for p in inventory()]
        self._checkout(inv=moved)

        draft = PhoneCartDraft.objects.get()
        by_id = {line["product_id"]: line for line in draft.lines}
        self.assertEqual(by_id["1"]["unit_price"], 30.0)
        self.assertEqual(by_id["10"]["unit_price"], 6.0)
        self.assertEqual(by_id["10"]["line_total"], 12.0)
        self.assertEqual(draft.quote["subtotal"], 42.0)
        self.assertEqual(draft.quote["total"], 42.0)
        self.assertEqual(draft.quote["source"], "live_register")

    def test_a_tampered_price_in_the_post_body_is_ignored(self):
        # The client sends a product id and a quantity. Nothing else is priceable —
        # if any of these were honoured, the URL would be a discount generator.
        self._add("1", unit_price="0.01", price="0.01", line_total="0.01")
        r = self._checkout(unit_price="0.01", price="0.01", line_total="0.01",
                           subtotal="0.01", total="0.01", discounts="99",
                           quote='{"total": 0.01}')
        self.assertEqual(r.status_code, 200)
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.lines[0]["unit_price"], 25.0)
        self.assertEqual(draft.lines[0]["line_total"], 25.0)
        self.assertEqual(draft.quote["total"], 25.0)
        self.assertEqual(draft.quote["discounts"], 0.0)

    def test_a_tampered_quantity_cannot_exceed_what_is_on_the_floor(self):
        thin = [dict(p, qty=3) if p["product_id"] == "1" else p for p in inventory()]
        self._add("1", qty=12, inv=thin)
        self._checkout(inv=thin)
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.lines[0]["quantity"], 3)
        self.assertEqual(draft.quote["total"], 75.0)

    def test_the_order_stores_no_phone_where_a_shopper_or_a_leak_would_find_it(self):
        self._add("1")
        r = self._checkout()
        draft = PhoneCartDraft.objects.get()

        # contact_phone is the ONE field allowed to hold it — staff must be able to
        # call about a sell-out. Everywhere else carries a hash or the last four.
        self.assertEqual(draft.phone_last4, "6999")
        self.assertEqual(len(draft.phone_last4), 4)
        self.assertTrue(draft.phone_hash)
        self.assertNotIn(PHONE, draft.phone_hash)
        self.assertNotEqual(draft.phone_hash, PHONE)
        self.assertEqual(draft.phone_hash, signing.customer_token(PHONE))
        for blob, where in ((str(draft.audit), "audit"), (str(draft.quote), "quote"),
                            (str(draft.lines), "lines")):
            self.assertNotIn(PHONE, blob, f"the full phone leaked into draft.{where}")
            self.assertNotIn("420-6999", blob)
        body = r.content.decode()
        self.assertNotIn(PHONE, body, "the success page printed the full phone")
        self.assertNotIn("420-6999", body)

    def test_the_order_is_stamped_for_the_staff_queue(self):
        self._add("1")
        self._checkout()
        draft = PhoneCartDraft.objects.get()
        entry = draft.audit[-1]
        self.assertEqual(entry["action"], "online_order_placed")
        self.assertEqual(entry["lines"], 1)
        self.assertIn("at", entry)

    def test_the_cart_cookie_is_cleared_so_a_refresh_starts_fresh(self):
        self._add("1")
        r = self._checkout()
        self.assertEqual(r.cookies[cart_mod.COOKIE].value, "")

    def test_email_is_optional(self):
        self._add("1")
        r = self._checkout(email="")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PhoneCartDraft.objects.get().contact_email, "")

    def test_the_success_page_never_leaks_staff_signals(self):
        self._add("1")
        body = self._checkout().content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body)

    def test_checkout_never_writes_to_dutchie(self):
        self._add("1")
        self._checkout()
        # Read-only at order time: a guest is created later, behind staff auth.
        self.assertEqual([name for name, _, _ in self.dutchie.method_calls], ["guest_search"])


class BundleDiscountCarryTests(CheckoutFlowTestCase):
    """An emailed bundle's discount has to survive the walk from link to register."""

    def setUp(self):
        super().setUp()
        Product.objects.create(sku="A", product_id="1", location_slug="yakima",
                               name="Blue Dream 3.5g", brand="Athenry", category="flower",
                               subcategory="3.5g", unit_weight=3.5, price=25,
                               cost=10, margin=15, quantity_on_hand=10)

    def test_the_bundle_discount_reaches_the_order(self):
        url = signing.build_url("/custom-order/", bundle="roll-relax", store="yakima",
                                items=[("1", 1), ("10", 2), ("20", 1)])
        with self._patch_inv():
            self.assertEqual(self.client.get(url).status_code, 200)

        r = self._checkout()
        self.assertEqual(r.status_code, 200)
        draft = PhoneCartDraft.objects.get()
        quote = draft.quote
        self.assertEqual(draft.bundle_slug, "roll-relax")
        self.assertEqual(quote["bundle"], "roll-relax")
        self.assertEqual(quote["bundle_name"], "Roll & Relax Bundle")
        self.assertEqual(quote["bundle_discount_pct"], 20)
        # 25 + 8x2 + 15, priced live. The percentage rides along as an instruction
        # for the register — `final_total_note` says the register applies discounts
        # and taxes, so `total` here is the pre-discount subtotal by design.
        self.assertEqual(quote["subtotal"], 56.0)
        self.assertEqual(quote["total"], 56.0)
        self.assertIn("Roll &amp; Relax Bundle", r.content.decode())


# ── 5. sold out at submit ────────────────────────────────────────────────────
class SoldOutAtSubmitTests(CheckoutFlowTestCase):
    def _gone(self, product_id="1"):
        return [p for p in inventory() if p["product_id"] != product_id]

    def test_an_item_that_sells_out_while_they_type_blocks_the_order(self):
        self._add("1")
        self._add("10", qty=2)
        r = self._checkout(inv=self._gone("1"))
        body = r.content.decode()

        self.assertEqual(r.status_code, 400)
        self.assertIn("Some items changed. Please review your cart.", body)
        self.assertIn("Blue Dream 3.5g", body)      # named, not silently dropped
        self.assertIn("sold out", body)
        self.assertNotIn("Order placed", body)
        self.assertNoOrderPlaced(r)

    def test_a_cart_that_sold_out_entirely_is_still_refused(self):
        self._add("1")
        r = self._checkout(inv=self._gone("1"))
        body = r.content.decode()
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("Order placed", body)
        self.assertNoOrderPlaced(r)
        # The shopper can still see WHICH item died — the line list keeps it and
        # marks it sold out rather than dropping it.
        self.assertIn("Blue Dream 3.5g", body)
        self.assertIn("sold out", body)
        # BUG (pinned as-is): the banner above that list says "Your cart is empty",
        # not "some items changed". views.checkout sets errors["cart"] for `issues`
        # and then unconditionally overwrites it when `count` is 0 — and a cart whose
        # only line sold out has count 0. The shopper is told their cart is empty
        # while looking at the item they put in it.
        self.assertIn("Your cart is empty", body)
        self.assertNotIn("Some items changed", body)

    def test_a_partial_sell_out_reduces_rather_than_overselling(self):
        # Two left on the floor, three in the cart: we must never confirm three.
        self._add("1", qty=3)
        thin = [dict(p, qty=2) if p["product_id"] == "1" else p for p in inventory()]
        r = self._checkout(inv=thin)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Some items changed. Please review your cart.", r.content.decode())
        self.assertNoOrderPlaced(r)
        self.assertEqual(PhoneCartDraft.objects.get().lines[0]["quantity"], 2)

    def test_the_shopper_can_recover_once_stock_is_back(self):
        self._add("1")
        self.assertEqual(self._checkout(inv=self._gone("1")).status_code, 400)
        r = self._checkout()                 # restocked
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PhoneCartDraft.objects.get().status, PhoneCartDraft.Status.RELEASED)

    def test_a_blocked_order_never_touches_dutchie_or_email(self):
        self._add("1")
        with override_settings(**SMTP):
            self._checkout(inv=self._gone("1"))
        self.dutchie_factory.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    def test_an_inventory_outage_does_not_confirm_an_unpriceable_order(self):
        # No live inventory means no live price. Confirming here would hand staff an
        # order priced off a week-old cookie.
        self._add("1")
        r = self._checkout(inv=[])
        self.assertEqual(r.status_code, 400)
        self.assertNoOrderPlaced(r)


# ── 6. customer attach ───────────────────────────────────────────────────────
class CustomerAttachTests(CheckoutFlowTestCase):
    """`cart_submit` needs an AcctId — an order with nobody attached is a dead end."""

    def _place(self):
        self._add("1")
        r = self._checkout()
        return r, PhoneCartDraft.objects.get()

    def test_an_existing_guest_is_found_by_phone_and_stamped(self):
        self.dutchie.guest_search.return_value = {"Data": [
            {"Guest_id": 4242, "Name": NAME, "PhoneNo": "(509) 420-6999"}]}
        r, draft = self._place()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(draft.dutchie_acct_id, "4242")
        self.assertEqual(draft.customer_name, NAME)
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.MATCHED)
        self.dutchie.guest_search.assert_called_once_with(PHONE)

    def test_no_guest_found_is_flagged_new_for_creation_at_claim(self):
        self.dutchie.guest_search.return_value = {"Data": []}
        r, draft = self._place()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(draft.dutchie_acct_id, "")
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.NEW)
        self.dutchie.create_guest.assert_not_called()

    def test_a_different_persons_phone_is_not_a_match(self):
        self.dutchie.guest_search.return_value = {"Data": [
            {"Guest_id": 9, "Name": "Someone Else", "PhoneNo": "5094209999"}]}
        _, draft = self._place()
        self.assertEqual(draft.dutchie_acct_id, "")
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.NEW)

    def test_a_dutchie_search_failure_does_not_lose_the_order(self):
        self.dutchie.guest_search.side_effect = RuntimeError("dutchie 503")
        r, draft = self._place()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Order placed", r.content.decode())
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        # "we couldn't check" must never be recorded as "no account", or the claim
        # path would mint a duplicate guest for an existing customer.
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.UNRESOLVED)
        self.assertEqual(draft.dutchie_acct_id, "")

    def test_a_dutchie_client_that_cannot_even_be_built_does_not_lose_the_order(self):
        # Missing store credentials / config, not just a bad response.
        self.dutchie_factory.side_effect = KeyError("yakima")
        r, draft = self._place()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.UNRESOLVED)

    def test_a_garbage_dutchie_payload_does_not_lose_the_order(self):
        self.dutchie.guest_search.return_value = "<html>504 Gateway Timeout</html>"
        r, draft = self._place()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.NEW)

    def test_the_public_endpoint_never_creates_a_guest(self):
        # Guest creation is a write, and every Dutchie write in this repo sits behind
        # staff auth. An unauthenticated create is a guest-record spam vector.
        self.dutchie.guest_search.return_value = {"Data": []}
        self._place()
        self.dutchie.create_guest.assert_not_called()

    def test_the_customer_seam_really_is_the_pos_register_client(self):
        # Guard the guard: every test in this module patches `customers._client`. If
        # that ever stopped being the single door to Dutchie, the patch would silently
        # stop protecting us and the suite would start making real calls.
        with patch("bundles.customers.PosRegisterClient") as klass, \
             patch("bundles.customers.get_store", return_value="store-cfg") as get_store:
            built = self.real_client_factory("yakima")
        get_store.assert_called_once_with("yakima")
        klass.assert_called_once_with("store-cfg")
        self.assertIs(built, klass.return_value)

    def test_the_seam_translates_mount_vernons_store_key(self):
        # location_slug and POS store key differ for exactly one store; a lookup sent
        # under the wrong key searches the wrong shop's guest list.
        with patch("bundles.customers.PosRegisterClient"), \
             patch("bundles.customers.get_store") as get_store:
            self.real_client_factory("mount-vernon")
        get_store.assert_called_once_with("mtvernon")


# ── 7. confirmation email ────────────────────────────────────────────────────
@override_settings(**SMTP)
class ConfirmationEmailTests(CheckoutFlowTestCase):
    def test_a_confirmation_is_sent_on_success(self):
        self._add("1")
        self._add("10", qty=2)
        r = self._checkout()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

        msg = mail.outbox[0]
        self.assertEqual(msg.to, [EMAIL])
        self.assertIn("Happy Time — Yakima", msg.subject)
        self.assertIn("Blue Dream 3.5g", msg.body)
        self.assertIn("Sunset Pre-Roll", msg.body)
        self.assertIn(NAME, msg.body)
        self.assertIn("41.00", msg.body)
        self.assertIn("21+", msg.body)
        code = PhoneCartDraft.objects.get().draft_token[-6:].upper()
        self.assertIn(code, msg.body)

    def test_no_email_address_means_no_send(self):
        self._add("1")
        self.assertEqual(self._checkout(email="").status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_confirmation_never_carries_staff_only_fields(self):
        self._add("1")
        self._checkout()
        blob = (mail.outbox[0].body + str(mail.outbox[0].alternatives)).lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, blob, f"{word} reached the shopper's inbox")

    def test_the_confirmation_never_carries_the_full_phone(self):
        self._add("1")
        self._checkout()
        blob = mail.outbox[0].body + str(mail.outbox[0].alternatives)
        self.assertNotIn(PHONE, blob)
        self.assertNotIn("420-6999", blob)

    def test_a_sold_out_line_is_not_billed_in_the_confirmation(self):
        # Two lines, one gone: the surviving order must not list what nobody can pick.
        self._add("1")
        self._add("10", qty=2)
        self.assertEqual(self._checkout(inv=[p for p in inventory()
                                             if p["product_id"] != "1"]).status_code, 400)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="")
class ConfirmationEmailDisabledTests(CheckoutFlowTestCase):
    def test_an_unconfigured_mailer_is_a_silent_no_op_not_a_failed_order(self):
        self._add("1")
        r = self._checkout()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Order placed", r.content.decode())
        self.assertEqual(PhoneCartDraft.objects.get().status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(**SMTP)
class ConfirmationEmailFailureTests(CheckoutFlowTestCase):
    def test_a_dead_smtp_server_does_not_roll_back_the_order(self):
        self._add("1")
        with patch("bundles.emails.EmailMultiAlternatives.send", side_effect=OSError("no smtp")):
            r = self._checkout()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Order placed", r.content.decode())
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertIsNotNone(draft.released_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_failure_while_building_the_message_still_leaves_the_order_placed(self):
        # `send_order_confirmation` only wraps `msg.send()` in try/except, and the call
        # site in views.checkout has no guard at all. So anything that raises while
        # BUILDING the body (a non-numeric line_total, a bad quote total) escapes into
        # the request. The order is already committed by then — that is the property
        # worth pinning, and it holds: the order is never lost.
        #
        # It does surface to the shopper as a 500 on an order that actually succeeded,
        # which contradicts the module docstring. See the findings note.
        self._add("1")
        with patch("bundles.emails._lines_text", side_effect=ValueError("bad line_total")):
            with self.assertRaises(ValueError):
                self._checkout()
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.pickup_name, NAME)
        self.assertIsNotNone(draft.released_at)


# ── 8. idempotency ───────────────────────────────────────────────────────────
@override_settings(**SMTP)
class CheckoutIdempotencyTests(CheckoutFlowTestCase):
    def test_double_submitting_the_form_places_one_order(self):
        self._add("1")
        first = self._checkout()
        second = self._checkout()            # the impatient second click

        self.assertEqual(first.status_code, 200)
        self.assertIn("Order placed", first.content.decode())
        # The cart cookie was cleared by the first success, and the draft is no longer
        # `open` — two independent reasons the second submit finds nothing to release.
        self.assertNotIn("Order placed", second.content.decode())
        self.assertIn("Your cart is empty", second.content.decode())
        self.assertEqual(PhoneCartDraft.objects.count(), 1)
        self.assertEqual(self._released().count(), 1)
        self.assertEqual(len(mail.outbox), 1, "the shopper was emailed twice")

    def test_replaying_the_cart_cookie_cannot_re_release_the_order(self):
        # A browser that ignored the delete-cookie, or a replayed request: the cart
        # token still exists, but the draft is no longer `open` so it cannot be found.
        self._add("1")
        token = PhoneCartDraft.objects.get().draft_token
        self._checkout()

        self.client.cookies[cart_mod.COOKIE] = token
        r = self._checkout()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Your cart is empty", r.content.decode())
        self.assertEqual(self._released().count(), 1)
        self.assertEqual(PhoneCartDraft.objects.count(), 1)

    def test_the_released_order_is_not_mutated_by_the_replay(self):
        self._add("1")
        self._checkout()
        before = PhoneCartDraft.objects.get()
        released_at, audit_len = before.released_at, len(before.audit)

        self.client.cookies[cart_mod.COOKIE] = before.draft_token
        self._checkout(name="Someone Else", phone="5095550000")
        after = PhoneCartDraft.objects.get()
        self.assertEqual(after.released_at, released_at)
        self.assertEqual(len(after.audit), audit_len)
        self.assertEqual(after.pickup_name, NAME)
        self.assertEqual(after.contact_phone, PHONE)

    def test_a_second_order_after_a_deliberate_new_cart_is_allowed(self):
        # Idempotency must not become "one order per shopper, ever".
        self._add("1")
        self._checkout()
        self._add("10", qty=2)
        r = self._checkout()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._released().count(), 2)
