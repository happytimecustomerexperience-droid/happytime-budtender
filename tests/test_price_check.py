"""`price-check` is the live price confirmation, and it silently did nothing.

The parser probed eight names for the price and six for availability. The real payload
— captured from a live register on 2026-08-06 — has neither set, and returns `Price` as
a FORMATTED STRING:

    {"Result": true, "Data": {"ProductName": "...", "ProductGrams": "28.35 g",
     "Quantity": 0, "Price": "$ 12.00", "PricingTier": {...}}}

`float("$ 12.00")` raises, the probe swallowed it, and the function returned all-None
with `ok: True`. Every caller — the POS at add-to-cart, and anything the storefront
would have built on it — believed the check succeeded and kept its cached price.

So the test that matters most here is simply: the real payload yields 12.00.
"""
from django.test import SimpleTestCase

from dutchie.pos_register_client import PosRegisterClient

parse = PosRegisterClient.parse_price_check

# Verbatim from the capture. Do not tidy these — the formatting IS the bug.
LIVE_TOPICAL = {"Result": True, "Message": None, "Data": {
    "ProductName": "6/5 DOH Approved Topical Unicorn Body Butter 1:1 100mg",
    "FlowerEquivalent": "0.00000g", "ProductGrams": "28.35 g",
    "Quantity": 0, "Price": "$ 12.00",
    "PricingTier": {"LspId": 0, "Name": None, "ChargeCodeId": 0, "PricingId": 0}}}

LIVE_FREE = {"Result": True, "Message": None, "Data": {
    "ProductName": "2k Gardens Trade Samples Mixed",
    "FlowerEquivalent": "0.00000g", "ProductGrams": "0.50 g",
    "Quantity": 4, "Price": "$ 0.00",
    "PricingTier": {"LspId": 0, "Name": None, "ChargeCodeId": 0, "PricingId": 0}}}


class TheRealPayloadTests(SimpleTestCase):
    def test_the_captured_response_yields_a_price(self):
        """The regression. This returned None for two years of deploys."""
        self.assertEqual(parse(LIVE_TOPICAL)["price"], 12.00)

    def test_a_dollar_sign_and_spaces_do_not_defeat_it(self):
        for raw, want in (("$ 12.00", 12.00), ("$12", 12.0), ("12.00", 12.0),
                          ("$ 1,234.50", 1234.50), (7.5, 7.5)):
            with self.subTest(raw=raw):
                self.assertEqual(parse({"Data": {"Price": raw}})["price"], want)

    def test_a_genuinely_free_item_is_zero_not_none(self):
        # 0.00 and "no answer" are different facts; conflating them makes a free
        # sample look like a failed lookup and silently restores the cached price.
        got = parse(LIVE_FREE)
        self.assertEqual(got["price"], 0.00)
        self.assertIsNotNone(got["price"])

    def test_quantity_is_not_treated_as_availability(self):
        """`Quantity: 0` must NOT read as out of stock.

        In the capture, a price-check returning Quantity 0 was immediately followed by
        a successful add of that item to the cart at $12.00. pos/views.py refuses the
        add when `available <= 0`, so mapping this field would block real sales.
        """
        self.assertIsNone(parse(LIVE_TOPICAL)["available"])
        self.assertIsNone(parse(LIVE_FREE)["available"])


class FallbackTests(SimpleTestCase):
    def test_the_older_speculative_names_still_resolve(self):
        # A discounted item plainly returns more than the two rows we captured; when
        # one of those names turns up it should work without another archaeology run.
        got = parse({"Data": {"DiscountedUnitPrice": 8.0, "RecUnitPrice": 10.0,
                              "TotalAvailable": 6}})
        self.assertEqual((got["price"], got["rec_price"], got["available"]), (8.0, 10.0, 6.0))

    def test_a_discount_is_derived_when_only_both_prices_come_back(self):
        self.assertEqual(parse({"Data": {"Price": 8.0, "RecUnitPrice": 10.0}})["discount"], 2.0)

    def test_garbage_stays_none_rather_than_becoming_zero(self):
        # Never invent a price. A None sends the caller to its cached value; a 0.00
        # would sell the item for nothing.
        for junk in ("", "  ", "N/A", None, {}, [], True):
            with self.subTest(junk=junk):
                self.assertIsNone(parse({"Data": {"Price": junk}})["price"])

    def test_a_failed_result_is_flagged(self):
        self.assertFalse(parse({"Result": False, "Data": {"Price": "$ 5.00"}})["ok"])

    def test_a_shapeless_response_does_not_raise(self):
        for bad in (None, {}, {"Data": None}, {"Data": []}, "nope"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse(bad)["price"])
