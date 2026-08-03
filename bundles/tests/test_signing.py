"""The bundle URL is a coupon. These tests are the forgery guard.

A landing page that tells a budtender "this customer gets 30% off" must not be
mintable by hand-editing a query string.
"""
from django.http import QueryDict
from django.test import TestCase, override_settings

from bundles import signing

SECRET = "unit-test-secret-value"

# ── GOLDEN VECTOR (keep identical to alpine-automations/audiences/tests_bundles.py) ──
# The signer lives in another repo that deploys separately, so this pinned pair is
# what stops the two canonicalisers drifting. If you change the algorithm, BOTH
# repos' tests fail — that is the point. Never update one side's expected value
# alone; a mismatch means every emailed link stops verifying in production.
GOLDEN_SECRET = "golden-vector-secret"
GOLDEN_PARAMS = {
    "b": "roll-relax",
    "loc": "yakima",
    "i": ["3483543:1", "3554685:2"],
    "c": "a3f91c88de4b7205ff31c0a9e7b6d412",
    "exp": "1786928148",
}
GOLDEN_CANONICAL = (
    "b=roll-relax&c=a3f91c88de4b7205ff31c0a9e7b6d412&exp=1786928148"
    "&i=3483543:1&i=3554685:2&loc=yakima"
)
GOLDEN_SIG = "3ddcc41e83b38e4e05de8548747681913b0b6a8e673e2bf4bf1099075397889b"
GOLDEN_PHONE = "+1 (509) 555-1212"
GOLDEN_CUSTOMER_TOKEN = "381d925946854215ddba52ae8a8f108b"


@override_settings(BUNDLE_URL_SECRET=GOLDEN_SECRET)
class GoldenVectorTests(TestCase):
    """Cross-repo contract. See the note above before changing any expected value."""

    def test_canonical_form_is_pinned(self):
        self.assertEqual(signing.canonical(GOLDEN_PARAMS), GOLDEN_CANONICAL)

    def test_signature_is_pinned(self):
        self.assertEqual(signing.sign(GOLDEN_PARAMS), GOLDEN_SIG)

    def test_customer_token_is_pinned(self):
        self.assertEqual(signing.customer_token(GOLDEN_PHONE), GOLDEN_CUSTOMER_TOKEN)

    def test_a_url_signed_by_the_sender_verifies_here(self):
        # End-to-end proof the two halves agree: this query string is exactly what
        # alpine-automations' bundle_url() emits for the golden vector.
        qs = ("b=roll-relax&loc=yakima&i=3483543%3A1&i=3554685%3A2"
              "&c=a3f91c88de4b7205ff31c0a9e7b6d412&exp=1786928148&sig=" + GOLDEN_SIG)
        req = signing.parse(QueryDict(qs), now=1_700_000_000)
        self.assertEqual(req.bundle, "roll-relax")
        self.assertEqual(req.items, [("3483543", 1), ("3554685", 2)])


@override_settings(BUNDLE_URL_SECRET=SECRET)
class CanonicalTests(TestCase):
    def test_param_order_does_not_change_the_signature(self):
        a = {"b": "weekend", "loc": "yakima", "i": ["1:1", "2:2"], "exp": "999"}
        b = {"exp": "999", "i": ["2:2", "1:1"], "loc": "yakima", "b": "weekend"}
        self.assertEqual(signing.canonical(a), signing.canonical(b))
        self.assertEqual(signing.sign(a), signing.sign(b))

    def test_unsigned_params_are_excluded(self):
        # AlpineIQ and friends append click-tracking; that must never break a live link.
        base = {"b": "weekend", "loc": "yakima", "exp": "1"}
        with_utm = dict(base, utm_source="email", aiq_click="xyz")
        self.assertEqual(signing.sign(base), signing.sign(with_utm))

    def test_missing_secret_fails_closed(self):
        with override_settings(BUNDLE_URL_SECRET=""):
            with self.assertRaises(signing.BundleUrlError):
                signing.sign({"b": "weekend"})


@override_settings(BUNDLE_URL_SECRET=SECRET)
class ParseTests(TestCase):
    def _url_params(self, **kw):
        url = signing.build_url("https://x.test/custom-order", **kw)
        return QueryDict(url.split("?", 1)[1])

    def test_round_trip(self):
        qd = self._url_params(bundle="weekend", store="yakima",
                              items=[("3483543", 1), ("3554685", 2)],
                              customer_token="abc123", now=1_000_000)
        req = signing.parse(qd, now=1_000_100)
        self.assertEqual(req.bundle, "weekend")
        self.assertEqual(req.store, "yakima")
        self.assertEqual(req.items, [("3483543", 1), ("3554685", 2)])
        self.assertEqual(req.customer_token, "abc123")
        self.assertFalse(req.expired)

    def test_tampered_item_is_rejected(self):
        qd = self._url_params(bundle="roll-relax", store="yakima",
                              items=[("111", 1)], now=1_000_000)
        forged = qd.copy()
        forged.setlist("i", ["999:9"])
        with self.assertRaises(signing.BundleUrlError):
            signing.parse(forged, now=1_000_100)

    def test_upgrading_the_discount_by_editing_the_bundle_is_rejected(self):
        qd = self._url_params(bundle="roll-relax", store="yakima",
                              items=[("111", 1)], now=1_000_000)
        forged = qd.copy()
        forged["b"] = "weekend"     # 20% -> 30%
        with self.assertRaises(signing.BundleUrlError):
            signing.parse(forged, now=1_000_100)

    def test_missing_signature_is_rejected(self):
        with self.assertRaises(signing.BundleUrlError):
            signing.parse(QueryDict("b=weekend&loc=yakima&i=1:1&exp=9999999999"))

    def test_signature_from_a_different_secret_is_rejected(self):
        qd = self._url_params(bundle="weekend", store="yakima",
                              items=[("111", 1)], now=1_000_000)
        with override_settings(BUNDLE_URL_SECRET="a-different-secret"):
            with self.assertRaises(signing.BundleUrlError):
                signing.parse(qd, now=1_000_100)

    def test_expired_link_parses_but_is_flagged(self):
        # Deliberately not an error: the shopper still gets a working page.
        qd = self._url_params(bundle="weekend", store="yakima",
                              items=[("111", 1)], ttl_days=1, now=1_000_000)
        req = signing.parse(qd, now=1_000_000 + 2 * 86400)
        self.assertTrue(req.expired)
        self.assertEqual(req.items, [("111", 1)])

    def test_quantities_are_clamped(self):
        qd = self._url_params(bundle="weekend", store="yakima",
                              items=[("111", 9999)], now=1_000_000)
        self.assertEqual(signing.parse(qd, now=1_000_100).items[0][1], signing.MAX_QTY)

    def test_item_count_is_capped(self):
        items = [(str(i), 1) for i in range(40)]
        qd = self._url_params(bundle="weekend", store="yakima", items=items, now=1_000_000)
        self.assertEqual(len(signing.parse(qd, now=1_000_100).items), signing.MAX_ITEMS)


@override_settings(BUNDLE_URL_SECRET=SECRET)
class CustomerTokenTests(TestCase):
    def test_token_is_stable_across_phone_formats(self):
        # These are one person; alpine-automations, Dutchie and AlpineIQ each
        # export a different shape. One token, or personalization silently misses.
        tokens = {
            signing.customer_token("+1 (509) 555-1212"),
            signing.customer_token("15095551212"),
            signing.customer_token("5095551212"),
            signing.customer_token("509-555-1212"),
        }
        self.assertEqual(len(tokens), 1)

    def test_token_is_opaque(self):
        t = signing.customer_token("5095551212")
        self.assertEqual(len(t), 32)
        self.assertNotIn("5551212", t)           # the phone must not be recoverable

    def test_blank_phone_yields_no_token(self):
        self.assertEqual(signing.customer_token(""), "")
