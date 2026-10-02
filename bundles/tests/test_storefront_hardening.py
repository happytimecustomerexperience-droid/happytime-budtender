"""Pre-launch hardening of the public storefront (round W5a).

Each class is one finding from the read-only audit; every test here fails on the code
that audit was run against. Through the happytimeweed.com Vercel rewrite every shopper
reaches Django from Vercel's egress IP, so none of the caps below may rest on client IP
alone — which is also why the throttles here are site-wide ceilings.

Nothing here touches the network: Dutchie is stubbed by the shared base class.
"""
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase, override_settings

from budtender.models import PhoneCartDraft
from bundles import caps, signing, views
from bundles.catalog import store_info
from bundles.tests.test_checkout_flow import EMAIL, PHONE, SECRET, SMTP, CheckoutFlowTestCase
from bundles.tests.test_resolver import live

# `rate_limit` buckets on int(time.time() // window); freezing the clock keeps a loop
# from rolling into the next bucket and resetting the counter by luck.
FROZEN = 1_700_000_000.0


def frozen_clock():
    return patch("pos_core.ratelimit.time.time", return_value=FROZEN)


@contextmanager
def past_the_pytest_guard():
    """`cart.confirm_live_price` refuses to call the register under pytest. Hide pytest
    from it so the caching around that call can be exercised — against a stubbed client."""
    with patch.dict(sys.modules):
        sys.modules.pop("pytest", None)
        yield


def three_lines():
    return [live(product_id=p, name=f"Item {p}", SerialNo=f"SER{p}", BatchId=int(p) + 40,
                 price=10.0, qty=10) for p in ("1", "2", "3")]


LAB = {"batch_id": 41, "cannabinoids": [{"name": "THCA", "value": 21.5, "unit": "%"}],
       "total_cannabinoids": {"name": "Total", "value": 19.0, "unit": "%"}}


# ── 1. Dutchie-key DoS ───────────────────────────────────────────────────────
class DutchieKeyDosTests(CheckoutFlowTestCase):
    def _lab(self, pid="1"):
        with self._patch_inv(three_lines()):
            return self.client.get(f"/custom-order/lab/{pid}?loc=yakima")

    def test_a_lab_result_is_fetched_once_per_batch_per_minute(self):
        with patch("bundles.views.dutchie_lab.lab_result", return_value=LAB) as lab:
            responses = [self._lab("1") for _ in range(5)]
        self.assertEqual(lab.call_count, 1, "every hit spent a request on the register's key")
        for r in responses:
            self.assertContains(r, "21.5")      # served from the cache, still rendered

    def test_each_batch_is_looked_up_on_its_own(self):
        with patch("bundles.views.dutchie_lab.lab_result", return_value=LAB) as lab:
            self._lab("1"), self._lab("2"), self._lab("1"), self._lab("2")
        self.assertEqual(sorted(c.args[1] for c in lab.call_args_list), [41, 42])

    def test_no_lab_data_is_cached_too(self):
        # `lab_result` returns None both for "none" and "register struggling" — a few
        # seconds of "none" is exactly what keeps a flood off a struggling register.
        with patch("bundles.views.dutchie_lab.lab_result", return_value=None) as lab:
            for _ in range(4):
                self.assertEqual(self._lab("1").status_code, 200)
        self.assertEqual(lab.call_count, 1)

    def test_the_lab_endpoint_is_throttled_on_every_hit(self):
        with patch("bundles.views.dutchie_lab.lab_result", return_value=LAB), frozen_clock():
            codes = [self._lab("1").status_code for _ in range(121)]
        self.assertEqual(codes[:120], [200] * 120)
        self.assertEqual(codes[-1], 429)

    def test_the_checkout_page_is_throttled_on_get_too(self):
        # GET rendered the form AFTER repricing the cart against the register, and the
        # old limiter only counted POST.
        with frozen_clock():
            codes = [self._get_checkout(inv=three_lines()).status_code for _ in range(121)]
        self.assertEqual(codes[:120], [200] * 120)
        self.assertEqual(codes[-1], 429)

    def test_the_bundle_landing_is_throttled(self):
        url = signing.build_url("/custom-order/", bundle="roll-relax", store="yakima",
                                items=[("1", 1)])
        # Primed rather than looped: a landing renders the whole page, 600 times is slow.
        cache.set(f"rl:bundle-landing:127.0.0.1:{int(FROZEN // 60)}", 600, 60)
        with frozen_clock(), self._patch_inv(three_lines()):
            r = self.client.get(url)
        self.assertEqual(r.status_code, 429)

    # ── the price check behind them ──────────────────────────────────────────
    def _register(self):
        """A stubbed register whose price-check answers $10 for every serial."""
        stack = patch("bundles.cart.get_store"), patch("bundles.cart.PosRegisterClient")
        for p in stack:
            self.addCleanup(p.stop)
        stack[0].start()
        client = stack[1].start()
        client.parse_price_check.side_effect = lambda raw: {"price": 10.0}
        return client

    def test_a_checkout_page_view_does_not_re_ask_the_register_every_time(self):
        for pid in ("1", "2", "3"):
            self._add(pid, inv=three_lines())
        client = self._register()
        with past_the_pytest_guard():
            for _ in range(4):
                self.assertEqual(self._get_checkout(inv=three_lines()).status_code, 200)
        # three lines, one confirmation each — not 4 views x 3 lines
        self.assertEqual(client.return_value.price_check.call_count, 3)

    def test_a_price_check_is_shared_between_shoppers(self):
        client = self._register()
        item = three_lines()[0]
        from bundles import cart as cart_mod
        with past_the_pytest_guard():
            prices = [cart_mod.confirm_live_price("yakima", item) for _ in range(5)]
        self.assertEqual(prices, [10.0] * 5)
        self.assertEqual(client.return_value.price_check.call_count, 1)

    def test_a_failed_price_check_is_not_cached_as_an_answer(self):
        from bundles import cart as cart_mod
        client = self._register()
        client.return_value.price_check.side_effect = [RuntimeError("register down"), {}]
        item = three_lines()[0]
        with past_the_pytest_guard():
            first = cart_mod.confirm_live_price("yakima", item)
            second = cart_mod.confirm_live_price("yakima", item)
        self.assertIsNone(first)
        self.assertEqual(second, 10.0)          # asked again, and got a real price

    def test_a_zero_price_is_an_answer_and_is_cached(self):
        # Samples exist: 0.00 is a price, not "no answer".
        from bundles import cart as cart_mod
        client = self._register()
        client.parse_price_check.side_effect = lambda raw: {"price": 0.0}
        item = three_lines()[0]
        with past_the_pytest_guard():
            self.assertEqual([cart_mod.confirm_live_price("yakima", item) for _ in range(3)],
                             [0.0] * 3)
        self.assertEqual(client.return_value.price_check.call_count, 1)


# ── 3. order spam / phishing through the confirmation email ──────────────────
@override_settings(**SMTP)
class OrderCapTests(CheckoutFlowTestCase):
    def _order(self, phone=PHONE, email="", **over):
        self._add("1")
        return self._checkout(phone=phone, email=email, **over)

    def test_a_phone_number_gets_three_orders_a_day(self):
        codes = [self._order().status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(self._released().count(), 3)

    def test_the_refusal_says_to_call_the_store(self):
        for _ in range(3):
            self._order()
        r = self._order()
        info = store_info("yakima")
        self.assertContains(r, f"Please call {info['label']} at {info['phone']}", status_code=429)
        self.assertContains(r, "Your order", status_code=429)    # the cart page, not a bare 429

    def test_every_shape_of_one_number_counts_as_one_phone(self):
        shapes = ("5094206999", "509-420-6999", "(509) 420-6999", "+15094206999")
        codes = [self._order(phone=p).status_code for p in shapes]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_another_number_is_not_affected(self):
        for _ in range(3):
            self._order()
        self.assertEqual(self._order(phone="5095550123").status_code, 200)

    def test_an_email_address_gets_three_confirmations_a_day(self):
        phones = ("5094206991", "5094206992", "5094206993", "5094206994")
        codes = [self._order(phone=p, email=EMAIL).status_code for p in phones]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(self._released().count(), 3)

    def test_the_email_cap_ignores_case(self):
        phones = ("5094206991", "5094206992", "5094206993", "5094206994")
        codes = [self._order(phone=p, email=e).status_code
                 for p, e in zip(phones, (EMAIL, EMAIL.upper(), EMAIL.title(), EMAIL))]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_an_email_refusal_does_not_spend_the_phone_budget(self):
        for p in ("5094206991", "5094206992", "5094206993"):
            self._order(phone=p, email=EMAIL)
        self.assertEqual(self._order(phone="5094206999", email=EMAIL).status_code, 429)
        # that refused attempt must not have used one of this phone's three orders
        codes = [self._order(phone="5094206999").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 200])

    def test_orders_without_an_email_are_not_email_capped(self):
        phones = [f"50942069{n:02d}" for n in range(5)]
        self.assertEqual([self._order(phone=p).status_code for p in phones], [200] * 5)

    def test_a_double_submit_is_one_order_against_the_cap(self):
        # The loser of a double-click releases nothing; it must not also spend the budget.
        def other_tab_wins(draft):
            PhoneCartDraft.objects.filter(pk=draft.pk).update(
                status=PhoneCartDraft.Status.RELEASED)

        self._add("1")
        with patch("bundles.customers.attach", side_effect=other_tab_wins):
            self._checkout(email="")
        self.assertEqual([self._order().status_code for _ in range(3)], [200, 200, 200])

    def test_the_cache_never_holds_a_raw_phone_or_email(self):
        self._order(phone=PHONE, email=EMAIL)
        keys = " ".join(str(k) for k in cache._cache)
        self.assertIn("cap:order-phone", keys)       # the counters exist...
        self.assertIn("cap:order-email", keys)
        self.assertNotIn(PHONE, keys)                # ...and carry no PII
        self.assertNotIn("sam.reyes", keys)
        self.assertNotIn("example.com", keys)


class CapsUnitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_the_limit_is_exact_and_give_back_restores_a_unit(self):
        self.assertEqual([caps.take("t", 2, 60, "v") for _ in range(3)], [True, True, False])
        caps.give_back("t", "v")
        caps.give_back("t", "v")
        self.assertEqual([caps.take("t", 2, 60, "v") for _ in range(3)], [True, True, False])

    def test_values_and_scopes_are_independent(self):
        self.assertTrue(caps.take("a", 1, 60, "x"))
        self.assertTrue(caps.take("a", 1, 60, "y"))
        self.assertTrue(caps.take("b", 1, 60, "x"))
        self.assertFalse(caps.take("a", 1, 60, "x"))

    def test_a_blank_value_is_a_site_wide_counter(self):
        self.assertEqual([caps.take("all", 2, 60) for _ in range(3)], [True, True, False])

    def test_giving_back_with_no_counter_is_harmless(self):
        caps.give_back("never-taken", "v")


class NameCleaningTests(SimpleTestCase):
    def test_real_names_survive(self):
        for name in ("Sam", "O'Brien", "O’Neil", "Mary-Jane", "José", "Jr.", "J. R.", "Ana María",
                     "St. John"):
            with self.subTest(name=name):
                self.assertEqual(views._clean_name(name), name)

    def test_only_letters_spaces_apostrophes_hyphens_and_periods_remain(self):
        for raw in ("<script>alert(1)</script>", "Bob 555-1212 call", "a\tb\nc", "x@y.com",
                    "https://evil.example/claim?x=1", "100% free $$$", "Sam_Reyes", "ａｂｃ１２３"):
            with self.subTest(raw=raw):
                cleaned = views._clean_name(raw)
                self.assertRegex(cleaned, r"^[^\W\d_]*([ '’.-]+[^\W\d_]*)*$")
                for bad in "/:@<>()=?&%$_,;!0123456789":
                    self.assertNotIn(bad, cleaned)

    def test_a_domain_does_not_survive_as_a_link(self):
        self.assertNotIn(".", views._clean_name("www.evil.com"))
        self.assertNotIn(".", views._clean_name("https://evil.example/login"))

    def test_at_most_forty_characters(self):
        self.assertEqual(len(views._clean_name("A" * 200)), 40)
        self.assertLessEqual(len(views._clean_name("word " * 40)), 40)

    def test_nothing_nameless_gets_through(self):
        for raw in ("", None, "1234", "....", "--", "   ", "'"):
            with self.subTest(raw=raw):
                self.assertEqual(views._clean_name(raw), "")


@override_settings(**SMTP)
class NameInTheEmailTests(CheckoutFlowTestCase):
    def test_a_link_typed_as_a_name_never_reaches_the_email_or_the_queue(self):
        self._add("1")
        r = self._checkout(first_name="Click", last_name="https://evil.example/claim?x=1 your prize")
        self.assertEqual(r.status_code, 200)
        order = self._released().get()
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        for text in (order.pickup_name, sent.body, sent.alternatives[0][0]):
            self.assertNotIn("://", text)
            self.assertNotIn("evil.example", text)
        self.assertIn("Hi Click httpsevil", sent.body)
        self.assertLessEqual(len(order.pickup_name), 40)


# ── 4. bundle link replay ────────────────────────────────────────────────────
@override_settings(BUNDLE_URL_SECRET=SECRET)
class SignatureAmbiguityTests(TestCase):
    """`canonical()` escapes nothing and is pinned across two repos, so the verifier
    refuses any value that could be read as a second parameter."""

    ITEMS = [("111", 1), ("222", 2)]

    def _genuine(self, **kw):
        url = signing.build_url("https://x.test/custom-order", bundle="roll-relax", store="yakima",
                                items=self.ITEMS, customer_token="abc123", ttl_days=1,
                                now=1_000_000, **kw)
        return QueryDict(url.split("?", 1)[1], mutable=True)

    def test_an_expiry_hidden_inside_another_field_is_rejected(self):
        qd = self._genuine()
        exp = qd["exp"]
        # The premise: moving `exp` into `c` leaves the signed string byte-identical.
        smuggled = {"b": "roll-relax", "loc": "yakima", "c": f"abc123&exp={exp}",
                    "i": [f"{s}:{q}" for s, q in self.ITEMS]}
        genuine = {"b": "roll-relax", "loc": "yakima", "c": "abc123", "exp": exp,
                   "i": [f"{s}:{q}" for s, q in self.ITEMS]}
        self.assertEqual(signing.canonical(smuggled), signing.canonical(genuine))

        del qd["exp"]
        qd["c"] = f"abc123&exp={exp}"
        with self.assertRaises(signing.BundleUrlError):
            signing.parse(qd, now=1_000_000 + 400 * 86400)       # long expired, and no longer says so

    def test_an_item_cannot_carry_a_second_item(self):
        qd = self._genuine()
        qd.setlist("i", ["111:1&i=222:2"])
        with self.assertRaises(signing.BundleUrlError):
            signing.parse(qd, now=1_000_100)

    def test_an_equals_sign_in_a_signed_value_is_not_honoured(self):
        # Even a value the signer genuinely signed: nothing legitimate contains one.
        params = {"b": "roll-relax", "loc": "yakima", "c": "a=b", "exp": "9999999999",
                  "i": ["111:1"]}
        qd = QueryDict(mutable=True)
        for k, v in params.items():
            qd.setlist(k, v if isinstance(v, list) else [v])
        qd["sig"] = signing.sign(params)
        with self.assertRaises(signing.BundleUrlError):
            signing.parse(qd, now=1_000_100)

    def test_ordinary_links_still_verify(self):
        req = signing.parse(self._genuine(), now=1_000_100)
        self.assertEqual((req.bundle, req.customer_token, req.items),
                         ("roll-relax", "abc123", self.ITEMS))
        self.assertFalse(req.expired)


class BundleReplayTests(CheckoutFlowTestCase):
    ITEMS = [("1", 1), ("10", 1), ("20", 1)]

    def _link(self, phone=PHONE, **kw):
        return signing.build_url("/custom-order/", bundle="roll-relax", store="yakima",
                                 items=self.ITEMS,
                                 customer_token=signing.customer_token(phone) if phone else "", **kw)

    def _land(self, url):
        with self._patch_inv():
            return self.client.get(url)

    def _expired_link(self):
        return self._link(now=int(time.time()) - 60 * 86400, ttl_days=14)

    # (a) an expired link no longer carries the offer
    def test_an_expired_link_fills_the_cart_but_not_the_offer(self):
        r = self._land(self._expired_link())
        self.assertContains(r, "offer has ended")
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(len(draft.lines), 3)                   # still a one-tap cart
        self.assertEqual(draft.bundle_slug, "")
        self.assertNotIn("bundle_discount_pct", draft.quote)
        body = r.content.decode()
        self.assertNotIn("comes off at the register", body)
        self.assertNotIn("Bundle discount", body)

    def test_an_expired_link_cannot_carry_the_offer_through_checkout(self):
        self._land(self._expired_link())
        self._checkout(email="")
        order = self._released().get()
        self.assertEqual(order.bundle_slug, "")
        self.assertNotIn("bundle_discount_pct", order.quote)

    def test_a_live_link_still_claims_the_offer(self):
        r = self._land(self._link())
        self.assertNotContains(r, "offer has ended")
        draft = PhoneCartDraft.objects.get()
        self.assertEqual(draft.bundle_slug, "roll-relax")
        self.assertEqual(draft.quote["bundle_discount_pct"], 20)

    def test_an_expiry_smuggled_into_the_link_is_a_dead_link(self):
        url = self._expired_link()
        qd = QueryDict(url.split("?", 1)[1], mutable=True)
        exp = qd.pop("exp")[0]
        qd["c"] = f"{qd['c']}&exp={exp}"
        with self._patch_inv():
            r = self.client.get("/custom-order/?" + qd.urlencode())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PhoneCartDraft.objects.count(), 0)

    # (b) the offer belongs to the phone it was sent to
    def test_the_offer_goes_through_for_the_phone_it_was_sent_to(self):
        self._land(self._link())
        r = self._checkout(email="", phone="(509) 420-6999")       # any shape of that number
        self.assertContains(r, "20% discount is applied")
        order = self._released().get()
        self.assertEqual(order.bundle_slug, "roll-relax")
        self.assertEqual(order.quote["bundle_discount_pct"], 20)

    def test_a_forwarded_link_prices_normally_for_a_different_number(self):
        self._land(self._link())
        r = self._checkout(email="", phone="5095550123")
        self.assertEqual(r.status_code, 200)
        order = self._released().get()
        self.assertEqual(order.bundle_slug, "")
        for key in ("bundle", "bundle_name", "bundle_discount_pct"):
            self.assertNotIn(key, order.quote)
        self.assertEqual(order.quote["total"], order.quote["subtotal"])
        body = r.content.decode()
        self.assertNotIn("discount is applied", body)
        self.assertIn("isn't on this order", body)

    def test_a_link_sent_to_nobody_in_particular_is_honoured_for_anyone(self):
        # An anonymous send carries no `c`: there is no recipient to bind it to.
        self._land(self._link(phone=""))
        self._checkout(email="", phone="5095550123")
        self.assertEqual(self._released().get().bundle_slug, "roll-relax")

    def test_a_refused_checkout_leaves_the_offer_on_the_cart(self):
        # Dropping the bundle is decided only once the order is really going ahead.
        self._land(self._link())
        r = self._checkout(email="", phone="12")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PhoneCartDraft.objects.get().bundle_slug, "roll-relax")

