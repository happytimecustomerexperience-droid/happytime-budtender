"""engine.why — the one-line reason on a pick (website cards AND the in-store POS 'For You').

The owner's screenshot showed the card repeating the product name three times: title, a strain
chip, and a why-line that appended the full name and the strain again. The reason is now name-free:
it never contains the product name and never repeats a strain/terpene/brand word already in it.
"""
from django.test import SimpleTestCase

from budtender import compliance, engine, terpenes

KNOWN = {"brand_affinity": {"Phat Panda": 0.9}, "strain_type_affinity": {}, "category_affinity": {},
         "subcategory_affinity": {}, "terpene_affinity": {}, "flavor_affinity": {}, "price_tier": "mid",
         "bucket_mix": {}, "novelty_score": 0.2, "purchase_history": []}


def _feat(**kw):
    base = {"id": "1", "brand": "Acme", "category": "flower", "cat_key": "flower", "subcategory": "3.5g",
            "strain": "Blue Dream", "strain_type": "Hybrid", "terpene": "", "name": "Acme Blue Dream 3.5g",
            "thc": None, "effects": [], "flavors": [], "price": 30, "price_was": None, "qty": 20,
            "margin": 10, "margin_pct": 30, "price_z": 0, "bucket": "core", "velocity": 0}
    base.update(kw)
    return base


class NameFreeReasonTests(SimpleTestCase):
    def test_the_reason_never_contains_the_product_name(self):
        for feat, pf, desired in (
            (_feat(), None, None),
            (_feat(thc=27.0), None, None),
            (_feat(terpene="limonene"), None, None),
            (_feat(price=20, price_was=30), None, "relaxed"),
            (_feat(qty=2), None, None),
            (_feat(brand="Phat Panda", name="Phat Panda Gelato 3.5g", strain="Gelato"), KNOWN, None),
            (_feat(strain="", terpene="", brand=""), None, None),
        ):
            out = engine.why(feat, desired, pf)
            self.assertTrue(out, feat)
            self.assertNotIn(feat["name"].lower(), out.lower(), (feat["name"], out))

    def test_a_strain_already_in_the_name_is_not_repeated(self):
        out = engine.why(_feat(strain="Blue Dream", name="Acme Blue Dream 3.5g"), None, None)
        self.assertNotIn("blue dream", out.lower())

    def test_the_strain_comparison_ignores_case(self):
        out = engine.why(_feat(strain="BLUE DREAM", name="acme blue dream 3.5g"), None, None)
        self.assertNotIn("blue dream", out.lower())

    def test_a_strain_not_in_the_name_may_still_add_information(self):
        out = engine.why(_feat(strain="Gelato", name="Acme House Flower 3.5g", brand="Acme"), None, None)
        self.assertIn("Gelato", out)

    def test_a_terpene_already_in_the_name_is_not_repeated(self):
        out = engine.why(_feat(terpene="limonene", name="Limonene Haze 3.5g", strain=""), None, None)
        self.assertNotIn("limonene", out.lower())

    def test_a_brand_already_in_the_name_is_not_repeated_in_the_hook_or_the_fallback(self):
        out = engine.why(_feat(brand="Phat Panda", name="Phat Panda Gelato 3.5g", strain="Gelato"), None, KNOWN)
        self.assertNotIn("phat panda", out.lower())
        self.assertIn("go-to", out)  # the personal hook survives, name-free
        bare = engine.why(_feat(brand="Acme", name="Acme Blue Dream 3.5g", strain=""), None, None)
        self.assertNotIn("acme", bare.lower())

    def test_the_fallback_is_short_name_free_and_non_empty(self):
        self.assertEqual(engine.why(_feat(strain="", name="Mystery Jar 3.5g", brand="Acme"), None, None),
                         "A standout Acme pick")
        self.assertEqual(engine.why(_feat(strain="", name="Acme Jar 3.5g", brand="Acme"), None, None),
                         "A standout pick")
        self.assertEqual(engine.why(_feat(strain="", name="Mystery Jar", brand=""), None, None), "A standout pick")

    def test_real_signals_still_lead_and_stay_name_free(self):
        out = engine.why(_feat(price=20, price_was=30, thc=27.0, name="Acme Blue Dream 3.5g"), None, None)
        self.assertEqual(out, "On sale — save $10 · hits hard at 27% THC")

    def test_pos_for_you_keeps_a_reason_for_a_known_customer_and_none_for_a_guest(self):
        items = [{"product_id": "a", "brand": "Phat Panda", "name": "Phat Panda Gelato 3.5g", "category": "flower",
                  "subcategory": "", "strain": "Gelato", "strain_type": "", "terpene": "", "thc": 0, "price": 30,
                  "price_was": 0, "margin_pct": 50, "price_z": 0, "bucket": "core", "velocity": 0, "qty": 10,
                  "effects": []}]
        known = engine.rank(items, KNOWN)
        self.assertTrue(known[0]["why"])
        self.assertNotIn("phat panda gelato", known[0]["why"].lower())
        self.assertEqual(engine.rank(items, None)[0]["why"], "")


class EffectMatchingTests(SimpleTestCase):
    """EFFECT_HINTS vocabulary: a terpene hint matches the terpene by EQUALITY, not as a substring of
    anything. "pinene" is a substring of "terpinene", so a terpinene-led product was scored and explained
    as "uplifted"."""

    def _f(self, **kw):
        return _feat(strain="", strain_type="", name="House Flower", **kw)

    def test_terpinene_is_not_pinene(self):
        self.assertEqual(engine._effect_score(self._f(terpene="terpinene"), "uplifted"), 0.0)
        self.assertEqual(engine._effect_score(self._f(terpene="terpineol"), "uplifted"), 0.0)
        self.assertEqual(engine._effect_score(self._f(terpene="alpha-terpinene"), "uplifted"), 0.0)

    def test_the_real_terpenes_still_match_by_equality(self):
        for terpene, desired in (("pinene", "uplifted"), ("limonene", "uplifted"), ("myrcene", "relaxed"),
                                 ("linalool", "relaxed")):
            self.assertEqual(engine._effect_score(self._f(terpene=terpene), desired), 1.0, terpene)

    def test_a_raw_dutchie_style_name_is_canonicalized_before_the_comparison(self):
        self.assertEqual(engine._effect_score(self._f(terpene="Beta-Myrcene"), "relaxed"), 1.0)
        self.assertEqual(engine._effect_score(self._f(terpene="Alpha-Pinene"), "uplifted"), 1.0)
        self.assertEqual(engine._effect_score(self._f(terpene="Alpha-Terpinene"), "uplifted"), 0.0)

    def test_the_wrong_effect_does_not_match(self):
        self.assertEqual(engine._effect_score(self._f(terpene="myrcene"), "uplifted"), 0.0)
        self.assertEqual(engine._effect_score(self._f(terpene="pinene"), "relaxed"), 0.0)
        self.assertEqual(engine._effect_score(self._f(terpene="pinene"), None), 0.0)

    def test_strain_type_and_name_cues_keep_working_at_a_word_start(self):
        for kw, desired in ((dict(strain_type="Indica"), "relaxed"), (dict(strain_type="Indica-Hybrid"), "relaxed"),
                            (dict(strain_type="Sativa"), "uplifted"), (dict(name="OG Kush 3.5g"), "relaxed"),
                            (dict(name="Lemon Haze"), "uplifted"), (dict(strain="Purple Kush"), "relaxed")):
            feat = _feat(**{"strain": "", "strain_type": "", "name": "House Flower", "terpene": "", **kw})
            self.assertEqual(engine._effect_score(feat, desired), 1.0, kw)

    def test_a_hint_buried_inside_a_longer_word_is_not_a_cue(self):
        feat = _feat(strain="", strain_type="", terpene="", name="Terpinene Dream 3.5g")
        self.assertEqual(engine._effect_score(feat, "uplifted"), 0.0)


class OneTerpeneMapTests(SimpleTestCase):
    """A terpene's effect is defined once, in terpenes.NOTES (its lean). engine.EFFECT_HINTS reads it, so the
    score and the card's wording cannot disagree again (pinene was 'uplifted' here and 'alert' there)."""

    def test_the_hint_sets_are_exactly_what_they_were_before_they_were_derived(self):
        self.assertEqual(engine.EFFECT_HINTS, {
            "relaxed": {"indica", "kush", "myrcene", "linalool"},
            "uplifted": {"sativa", "haze", "limonene", "pinene"},
            "middle": {"hybrid"}})

    def test_every_terpene_that_has_a_lean_is_in_exactly_one_ask_and_the_others_in_none(self):
        asks = [(ask, hints & set(terpenes.NOTES)) for ask, hints in engine.EFFECT_HINTS.items()]
        in_asks = [t for _, ts in asks for t in ts]
        self.assertEqual(sorted(in_asks), sorted(set(in_asks)))
        self.assertEqual(set(in_asks), {k for k, v in terpenes.NOTES.items() if v["lean"]})
        for key, entry in terpenes.NOTES.items():
            if not entry["lean"]:   # caryophyllene / terpinolene: no effect claimed, so no ask matches them
                self.assertFalse(any(key in ts for _, ts in asks), key)

    def test_the_card_words_and_the_score_agree_per_terpene(self):
        for ask, leans in (("relaxed", {"relaxing", "calm"}), ("uplifted", {"uplifting", "alert"})):
            for key in engine.EFFECT_HINTS[ask] & set(terpenes.NOTES):
                self.assertIn(terpenes.profile([{"name": key, "pct": 1}])["lean"], leans, (ask, key))
                self.assertEqual(engine._effect_score(_feat(strain="", strain_type="", name="X", terpene=key), ask),
                                 1.0, (ask, key))


class AromaReasonTests(SimpleTestCase):
    """why(..., aroma_hit): said factually, only when a real batch lab matched, and never twice."""

    def test_no_hit_is_exactly_the_old_reason(self):
        for feat, desired, pf in ((_feat(), None, None), (_feat(thc=27.0, qty=2), None, None),
                                  (_feat(price=20, price_was=30), "relaxed", None),
                                  (_feat(terpene="limonene", name="House Flower"), None, KNOWN)):
            self.assertEqual(engine.why(feat, desired, pf, None), engine.why(feat, desired, pf))

    def test_a_leading_terpene_and_a_lesser_top_terpene_are_worded_differently(self):
        self.assertEqual(engine.why(_feat(), None, None, ("citrus", "limonene", True)),
                         "Citrus-forward — limonene leads")
        self.assertEqual(engine.why(_feat(), None, None, ("earthy", "myrcene", False)),
                         "Earthy notes — myrcene is a top terpene")

    def test_it_comes_after_the_deal_and_the_requested_effect_and_before_potency(self):
        # the two-reason cap: deal, then aroma; the potency line is the one cut
        out = engine.why(_feat(thc=27.0, price=20, price_was=30), "relaxed", None, ("pine", "pinene", True))
        self.assertEqual(out, "On sale — save $10 · pine-forward — pinene leads")
        # a matched effect ask (an Indica for "relaxed") leads the aroma
        out = engine.why(_feat(strain_type="Indica", thc=27.0), "relaxed", None, ("citrus", "limonene", True))
        self.assertEqual(out, "Dialed in for relaxed · citrus-forward — limonene leads")

    def test_a_terpene_the_name_already_says_is_not_repeated(self):
        out = engine.why(_feat(name="Limonene Haze 3.5g", strain=""), None, None, ("citrus", "limonene", True))
        self.assertEqual(out, "Citrus-forward")

    def test_the_terpene_forward_fallback_does_not_repeat_the_aroma_terpene(self):
        out = engine.why(_feat(terpene="limonene", name="House Flower", strain=""), None, None,
                         ("citrus", "limonene", True))
        self.assertEqual(out, "Citrus-forward — limonene leads")
        self.assertNotIn("limonene-forward", out)

    def test_every_aroma_wording_passes_the_compliance_screen_and_never_carries_the_name(self):
        for aroma, terps in terpenes.AROMA_TERPENES.items():
            for terpene in terps:
                for leads in (True, False):
                    out = engine.why(_feat(), None, None, (aroma, terpene, leads))
                    self.assertEqual(compliance.therapeutic_hits(out), [], out)
                    self.assertEqual(compliance.injection_hits(out), [], out)
                    self.assertNotIn(_feat()["name"].lower(), out.lower())
