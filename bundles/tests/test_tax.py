"""The menu price is the price — nothing is added at checkout.

Anchored to a real capture of Dutchie's own pre-submit checkout for this dispensary
(3-item Yakima pickup cart, read from its `computeWithPriceCartV2` response):

    menu $27.00 + $25.00 + $15.00 = $67.00
    Subtotal $54.05 / Discount -$8.00 / Taxes $20.95 / ORDER TOTAL $67.00
    taxInclusivePricing: true

The order total equals the menu prices exactly. The single property worth defending is
therefore `total == menu total`, and the test named for it is the one that must never
be "fixed" by adding tax.
"""
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from bundles import tax


class TheMenuPriceIsThePriceTests(SimpleTestCase):
    def test_nothing_is_added_to_the_menu_total(self):
        """The whole point. Dutchie's ORDER TOTAL equalled the sum of menu prices."""
        for shelf in (0, 5, 15, 27, 67, 95.50, 1234.56):
            self.assertEqual(tax.quote(shelf, "yakima")["total"], float(tax._money(shelf)),
                             f"tax was added to a menu price of {shelf}")

    def test_it_matches_the_captured_dutchie_cart(self):
        # $27 + $25 + $15, the exact cart that was walked to the pre-submit screen.
        self.assertEqual(tax.quote(27 + 25 + 15, "yakima")["total"], 67.00)

    def test_the_tax_estimate_lands_near_what_dutchie_reported(self):
        # Dutchie showed Taxes $20.95 on that $67 order. We model it as 37% excise plus
        # the local sales rate on the price net of both, which is close but not their
        # per-item rounding — hence "estimate", asserted as a tolerance not an equality.
        q = tax.quote(67, "yakima")
        self.assertAlmostEqual(q["tax_included"], 20.95, delta=0.10)
        self.assertTrue(q["tax_is_estimate"])

    def test_the_split_always_reconstitutes_the_total(self):
        # A breakdown that does not add up is the fastest way to lose someone's trust
        # at the moment they are deciding to buy.
        for shelf in (13.37, 67, 95.50, 210.05):
            q = tax.quote(shelf, "yakima")
            self.assertAlmostEqual(q["pre_tax"] + q["tax_included"], q["total"], places=2)

    def test_a_bigger_local_rate_means_more_of_the_price_is_tax_not_a_bigger_price(self):
        yak, mv = tax.quote(100, "yakima"), tax.quote(100, "mount-vernon")
        self.assertEqual(yak["total"], mv["total"], "the store changed the price")
        self.assertGreater(mv["tax_included"], yak["tax_included"])


class RateTableTests(SimpleTestCase):
    def test_each_store_carries_the_rate_dor_returned(self):
        self.assertEqual(tax.SALES_TAX["yakima"][0], Decimal("0.086"))
        self.assertEqual(tax.SALES_TAX["mount-vernon"][0], Decimal("0.090"))
        self.assertEqual(tax.SALES_TAX["pullman"][0], Decimal("0.080"))

    def test_pullman_is_filed_under_unincorporated_whitman_county(self):
        """The store sits outside Pullman city limits — code 3800, not the city's.

        Its address says "Pullman", so deriving a rate from the city name would quietly
        use the wrong one.
        """
        self.assertEqual(tax.SALES_TAX["pullman"][1], "3800")

    def test_an_unknown_store_never_understates_the_tax_share(self):
        self.assertEqual(tax.rate_for("mars"), max(r for r, _, _ in tax.SALES_TAX.values()))

    def test_a_stale_rate_cannot_change_what_anyone_is_charged(self):
        # The rate feeds only the informational split. Proving that here means a
        # forgotten quarterly refresh is a cosmetic problem, never a billing one.
        cheap = tax.quote(100, "pullman")["total"]
        dear = tax.quote(100, "mount-vernon")["total"]
        self.assertEqual(cheap, dear, "the sales-tax rate leaked into the total")

    def test_the_rate_table_has_not_gone_stale(self):
        """DOR republishes quarterly; fail once we are two quarters behind.

        A test rather than an auto-refresh: a rate that updates itself changes what is
        displayed with nobody looking.
        """
        quarters = {q for _, _, q in tax.SALES_TAX.values()}
        self.assertEqual(len(quarters), 1, f"rates come from different quarters: {quarters}")
        recorded = quarters.pop()
        rq, ry = int(recorded[1]), int(recorded.split()[1])
        today = date.today()
        age = (today.year - ry) * 4 + ((today.month - 1) // 3 + 1) - rq
        self.assertLess(age, 2, (
            f"tax rates are from {recorded}, {age} quarters ago. Re-query DOR and update "
            "bundles/tax.py:SALES_TAX — codes 3913 (Yakima), 2907 (Mount Vernon), "
            "3800 (Whitman County, NOT Pullman city):\n"
            "  https://webgis.dor.wa.gov/webapi/AddressRates.aspx?output=xml"
            "&addr=1315+N+1st+St&city=Yakima&zip=98901"))


class EdgeTests(SimpleTestCase):
    def test_an_empty_cart_is_free(self):
        q = tax.quote(0, "yakima")
        self.assertEqual((q["total"], q["tax_included"]), (0.0, 0.0))

    def test_cents_round_half_up_like_a_till(self):
        self.assertEqual(tax._money("0.005"), Decimal("0.01"))
        self.assertEqual(tax._money("0.015"), Decimal("0.02"))

    def test_binary_float_error_never_reaches_a_displayed_price(self):
        self.assertEqual(tax.quote(0.1 + 0.2, "yakima")["total"], 0.30)


class TaxRidesOnWhatTheyActuallyPayTests(SimpleTestCase):
    """The bundle discount is applied at the register, so `quote["total"]` is the
    PRE-discount subtotal. The displayed tax must still come off the discounted
    price — otherwise a $95 cart at 30% off reported $29.75 of tax against a $66.50
    price, which is what shipped for one deploy."""

    def _totals(self, subtotal, pct=0):
        from bundles.views import _totals
        return _totals({"quote": {"total": subtotal, "bundle_discount_pct": pct}}, "yakima")

    def test_without_a_bundle_the_tax_rides_on_the_subtotal(self):
        self.assertEqual(self._totals(95)["total"], 95.00)

    def test_with_a_bundle_it_rides_on_the_discounted_price(self):
        t = self._totals(95, 30)
        self.assertEqual(t["total"], 66.50)
        # The bug: $29.75 was the tax on the undiscounted $95.
        self.assertLess(t["tax_included"], 29.75)
        self.assertAlmostEqual(t["tax_included"], 66.50 - t["pre_tax"], places=2)

    def test_the_tax_share_never_exceeds_the_price(self):
        for subtotal, pct in ((95, 30), (78, 20), (55, 25), (13.37, 0)):
            t = self._totals(subtotal, pct)
            self.assertLess(t["tax_included"], t["total"], f"{subtotal}/{pct}%")
