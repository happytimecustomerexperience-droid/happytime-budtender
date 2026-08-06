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
    """CORRECTED TWICE. First version: hide any sub-1.0 reading as garbage. Second
    version (one turn of this session): scale it x100 as a mis-entered fraction,
    on the theory that 0.15 meant "15%". THAT theory was tested against a real lab
    panel for the SAME batch (2026-08-06 capture) and failed — the menu-row
    THCContent (2.9) matched the lab's decarbed **THC** field (2.9) exactly, not
    THCA (48) and not TotalCannabinoids (44.996). So a small THCContent is very
    often just a small amount of ALREADY-ACTIVE THC in a raw, undecarbed sample —
    a real number, not a scaling bug. Multiplying it by 100 was inventing a false
    potency figure, on a regulated claim, which is worse than showing none.

    Back to showing the raw value only where it reads as credible for its
    category; nothing is transformed. The number people actually mean by
    "25% THC" is THCA, which does not exist on a menu row — see
    `public_potency()` and `dutchie/lab.py` for that, sourced per-batch from the
    register's own lab panel, not guessed from this field.
    """

    def test_the_exact_live_values_that_were_wrong_stay_hidden(self):
        # Real THCContent readings from the Yakima floor, all under 5% on flower.
        # No longer scaled — shown to be decarbed-THC, not a fraction of THCA.
        for raw in (0.154, 0.271, 0.15, 0.411, 0.459, 0.371, 0.556, 0.49):
            row = live(cat_key="flower", thc=raw)
            self.assertIsNone(public_thc(row),
                              f"{raw} would render as '{raw}% THC' on a flower card")

    def test_a_credible_flower_percentage_is_kept(self):
        for raw in (20.1256, 22.0, 18.5, 31.0):
            self.assertEqual(public_thc(live(cat_key="flower", thc=raw)), raw)

    def test_the_same_strain_in_two_sizes_disagreeing_is_visible_not_averaged(self):
        # 14g read 0.15, 28g read 22.0. Shown as-is: the low reading is plausibly
        # correct (little active THC in a raw sample) and is not inflated to match.
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


class UnitConventionTests(SimpleTestCase):
    """`THCContent` on a menu row is NOT a THCA-style potency figure.

    Ground truth, not theory: the one batch present in BOTH the live inventory pull
    and a real lab panel (2026-08-06 capture, `dutchie/fixtures/`) shows the
    menu-row THCContent (2.9) matching the lab's **decarbed THC** field (2.9)
    exactly — not THCA (48), not TotalCannabinoids (44.996). So `public_thc`
    applies no transform: a small reading is often a real, small amount of
    already-active THC in a raw sample, not a mis-entered fraction.

    `public_potency()` is the function that answers "how strong is it" the way a
    shopper means it — THCA and Total, sourced per-batch from the lab endpoint,
    never guessed from this field.
    """

    def _thc(self, value, cat="flower"):
        return resolver.public_thc({"cat_key": cat, "thc": value})

    def test_no_transform_is_applied(self):
        # A raw value is shown exactly, or not at all — never rescaled.
        self.assertEqual(self._thc(18.85), 18.85)
        self.assertEqual(self._thc(22), 22.0)
        self.assertIsNone(self._thc(0.15))   # plausible, small, real — and hidden

    def test_a_small_reading_below_the_category_floor_is_hidden_not_inflated(self):
        # The one product present in both the search pull and the lab fixture reads
        # THCContent 2.9 — the SAME value as the lab's decarbed THC field, not THCA
        # (48) or Total (44.996); see dutchie/tests/test_lab.py for that ground
        # truth. 2.9% is below the concentrate floor and correctly hidden here
        # rather than scaled up to look like the 48% the label actually claims.
        self.assertIsNone(self._thc(2.9, "concentrate"))

    def test_frank_nonsense_is_still_dropped(self):
        # A 1g rosin at 1.258%: not a believable concentrate reading either way.
        self.assertIsNone(self._thc(1.258, "concentrate"))

    def test_zero_and_missing_stay_none(self):
        for v in (0, 0.0, None, "", "abc"):
            self.assertIsNone(self._thc(v), v)

    def test_a_vape_in_whole_percent_is_shown_as_is(self):
        self.assertEqual(self._thc(80.32, "vapes"), 80.32)


class PublicPotencyTests(SimpleTestCase):
    """THCA and Total, from a dutchie.lab.lab_result() dict. Both, never one."""

    def test_both_figures_surface_when_lab_data_has_them(self):
        lab = {"cannabinoids": [{"name": "THCA", "value": 48.0, "unit": "%"},
                                {"name": "THC", "value": 2.9, "unit": "%"}],
              "total_cannabinoids": {"name": "Total Cannabinoids", "value": 44.996, "unit": "%"}}
        got = resolver.public_potency(lab)
        self.assertEqual(got["thca"], 48.0)
        self.assertEqual(got["total"], 45.0)  # round(44.996, 2)

    def test_no_lab_data_is_both_none(self):
        self.assertEqual(resolver.public_potency(None), {"thca": None, "total": None})
        self.assertEqual(resolver.public_potency({}), {"thca": None, "total": None})

    def test_missing_thca_in_the_cannabinoid_list_is_none_not_zero(self):
        lab = {"cannabinoids": [{"name": "CBD", "value": 1.0, "unit": "%"}],
              "total_cannabinoids": {"value": 12.0}}
        got = resolver.public_potency(lab)
        self.assertIsNone(got["thca"])
        self.assertEqual(got["total"], 12.0)

    def test_a_garbage_total_value_does_not_raise(self):
        got = resolver.public_potency({"total_cannabinoids": {"value": "n/a"}})
        self.assertIsNone(got["total"])
