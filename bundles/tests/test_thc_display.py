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
from django.test import TestCase

from bundles.resolver import _public, public_thc
from bundles.tests.test_resolver import live


class ImplausibleValuesAreSuppressed(TestCase):
    def test_the_exact_live_values_that_were_wrong_are_now_hidden(self):
        # Real THCContent readings from the Yakima floor, all under 5% on flower.
        for raw in (0.154, 0.271, 0.15, 0.411, 0.459, 0.371, 0.556, 0.49):
            row = live(cat_key="flower", thc=raw)
            self.assertIsNone(public_thc(row),
                              f"{raw} would render as '{raw}% THC' on a flower card")

    def test_a_credible_flower_percentage_is_kept(self):
        for raw in (20.1256, 22.0, 18.5, 31.0):
            self.assertEqual(public_thc(live(cat_key="flower", thc=raw)), raw)

    def test_the_same_strain_in_two_sizes_disagreeing_is_visible_not_averaged(self):
        # 14g read 0.15, 28g read 22.0. We show the one we trust and hide the other
        # rather than inventing a reconciliation.
        self.assertIsNone(public_thc(live(cat_key="flower", thc=0.15)))
        self.assertEqual(public_thc(live(cat_key="flower", thc=22.0)), 22.0)

    def test_vapes_and_concentrates_keep_their_reliable_readings(self):
        self.assertEqual(public_thc(live(cat_key="vapes", thc=80.91)), 80.91)
        self.assertEqual(public_thc(live(cat_key="concentrate", thc=93.0)), 93.0)

    def test_a_vape_reading_that_looks_like_a_fraction_is_suppressed(self):
        self.assertIsNone(public_thc(live(cat_key="vapes", thc=0.32)))

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
