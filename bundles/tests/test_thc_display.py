"""Potency is a regulated claim — never print one we can't stand behind.

The register's THCContent field is not a consistent unit. Live Yakima inventory
(2026-08-02) had "Athenry Flower Rainbow Runtz 14g" at 0.15 and the 28g of the same
strain at 22.0. Across all flower the median was 0.49 and 98% of rows sat under 5%,
so the public storefront was advertising ~0.5% THC flower.

`resolver.public_thc` therefore shows a figure only where the number reads as a
credible percentage for its category, and omits it otherwise. These tests pin that,
including the "would this guard actually catch it" case — a permissive band would
let the original bug straight back in.
"""
from django.test import SimpleTestCase, TestCase

from bundles import resolver
from bundles.resolver import _public, public_thc
from bundles.tests.test_resolver import live


class ImplausibleValuesAreSuppressed(TestCase):
    """Superseded in part by the 2026-08-06 register capture (see resolver.py
    public_thc). Sub-1.0 readings were assumed to be unrecoverable garbage and
    hidden; the capture proved most of them are a real value written as a fraction
    — the SAME strain, SAME batch, agreeing across package sizes once scaled:

        Happy Buds "Syrup Soaked"  batch 7577655/6 -> 0.4187
                                   batch 7571073/4 -> 18.85

    So these are rewritten to assert the scaled value rather than a hidden one. What
    is UNCHANGED and still tested here: genuinely impossible values (negative, over
    100% once scaled, mg-dosed categories) are still dropped, not guessed.
    """

    def test_the_exact_live_values_that_were_wrong_are_now_scaled_up(self):
        # Real THCContent readings from the Yakima floor, all sub-1.0 on flower —
        # fractions, scaled to the percentages they actually represent.
        cases = {0.154: 15.4, 0.271: 27.1, 0.15: 15.0, 0.411: 41.1,
                 # 0.459 and 0.49 scale to 45.9% / 49.0% — over the flower band's 45%
                 # ceiling, so still hidden. Scaling doesn't mean every value survives.
                 0.459: None, 0.371: 37.1, 0.556: None, 0.49: None}
        for raw, want in cases.items():
            row = live(cat_key="flower", thc=raw)
            self.assertEqual(public_thc(row), want, f"{raw} -> {want}")

    def test_a_credible_flower_percentage_is_kept(self):
        for raw in (20.1256, 22.0, 18.5, 31.0):
            self.assertEqual(public_thc(live(cat_key="flower", thc=raw)), raw)

    def test_the_same_strain_in_two_sizes_now_agrees_once_scaled(self):
        # 14g read 0.15 (a fraction), 28g read 22.0 (already whole percent). Both
        # are now shown; they no longer look like one right answer and one hidden.
        self.assertEqual(public_thc(live(cat_key="flower", thc=0.15)), 15.0)
        self.assertEqual(public_thc(live(cat_key="flower", thc=22.0)), 22.0)

    def test_vapes_and_concentrates_keep_their_reliable_readings(self):
        self.assertEqual(public_thc(live(cat_key="vapes", thc=80.91)), 80.91)
        self.assertEqual(public_thc(live(cat_key="concentrate", thc=93.0)), 93.0)

    def test_a_vape_fraction_is_scaled_like_flower(self):
        self.assertEqual(public_thc(live(cat_key="vapes", thc=0.32)), 32.0)

    def test_mg_dosed_categories_never_show_a_percentage(self):
        # Edibles/tinctures/topicals carry mg. Live medians were 10, 240 and 240 —
        # "240% THC" is not a thing.
        for cat, raw in (("edibles", 10.0), ("edibles", 110.0),
                         ("tinctures", 240.0), ("topicals", 1250.0)):
            self.assertIsNone(public_thc(live(cat_key=cat, thc=raw)),
                              f"{cat} must not render {raw} as a percentage")

    def test_missing_or_junk_values_are_safe(self):
        for raw in (None, "", "n/a", 0, -3.0):
            self.assertIsNone(public_thc(live(cat_key="flower", thc=raw)))

    def test_an_unknown_category_shows_nothing(self):
        self.assertIsNone(public_thc(live(cat_key="other", thc=25.0)))


class ProjectionCarriesTheSuppression(TestCase):
    def test_public_projection_uses_the_guard(self):
        self.assertIsNone(_public(live(cat_key="flower", thc=0.49))["thc"])
        self.assertEqual(_public(live(cat_key="flower", thc=24.0))["thc"], 24.0)

    def test_suppressed_thc_means_the_card_prints_no_percentage(self):
        # The template guards on truthiness (`{% if p.thc %}`), so None is what
        # actually removes the "· X% THC" fragment from the card.
        self.assertFalse(_public(live(cat_key="flower", thc=0.49))["thc"])

    def test_the_guard_would_catch_a_regression(self):
        # If someone widens the flower band down to zero, the original bug returns.
        # This asserts the band is genuinely restrictive rather than decorative.
        from bundles.resolver import _THC_PERCENT_BANDS
        lo, _ = _THC_PERCENT_BANDS["flower"]
        self.assertGreaterEqual(lo, 5.0,
                                "a flower floor below 5% re-admits the 0.49% readings")


class UnitConventionTests(SimpleTestCase):
    """`THCContent` arrives in two conventions under the same unit id.

    From the 2026-08-06 register capture, one strain, two batches, both internally
    consistent across package sizes:

        Happy Buds "Syrup Soaked"  batch 7577655/6 -> 0.4187
                                   batch 7571073/4 -> 18.85

    A sub-1.0 THC figure in a THC-bearing category is a fraction — nobody sells
    0.42%-THC flower as flower. Before this, the band hid 1,097 products with usable
    numbers; flower showed potency on 16 of 645 rows.
    """

    def _thc(self, value, cat="flower"):
        return resolver.public_thc({"cat_key": cat, "thc": value})

    def test_a_fraction_is_scaled_to_a_percentage(self):
        self.assertEqual(self._thc(0.4187), 41.87)
        self.assertEqual(self._thc(0.15), 15.0)

    def test_a_whole_percentage_is_left_alone(self):
        self.assertEqual(self._thc(18.85), 18.85)
        self.assertEqual(self._thc(22), 22.0)

    def test_both_batches_of_one_strain_now_agree_within_a_few_points(self):
        # The two conventions should land in the same neighbourhood once normalised;
        # if they ever diverge wildly again, the convention has changed.
        self.assertLess(abs(self._thc(0.4187) - 41.87), 0.01)
        self.assertLess(abs(self._thc(18.85) - 18.85), 0.01)

    def test_frank_nonsense_is_still_dropped(self):
        # A 1g rosin at 1.258: 125.8% as a fraction, 1.258% as a percentage. The
        # capture contains rows like this and neither reading is showable.
        self.assertIsNone(self._thc(1.258, "concentrate"))

    def test_zero_and_missing_stay_none(self):
        for v in (0, 0.0, None, "", "abc"):
            self.assertIsNone(self._thc(v), v)

    def test_a_vape_in_whole_percent_survives(self):
        # 845 of 900 vape rows were already whole percent and must not be scaled.
        self.assertEqual(self._thc(80.32, "vapes"), 80.32)
