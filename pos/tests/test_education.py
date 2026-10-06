"""The staff product page's terpene / effect / strain notes obey the LCB content rules.

pos/education.py once said "relaxing and sedating", "stress relief", "anti-inflammatory", "anti-anxiety"
(WAC 314-55-155 / LCB-CONTENT-COMPLIANCE.md 2.1: no implied treatment). Every string it can emit now runs
through the same screens as the cards: budtender.compliance and the website's own rules, and a
terpene's effect has to be the one budtender.terpenes says."""
import re

from django.test import SimpleTestCase

from budtender import compliance, terpenes
from budtender.tests.test_terpenes import BANNED_MORE, WEBSITE_RULES
from pos import education


def _every_string() -> list[str]:
    out = [s for pair in (education.terpene_info(n) for n in education._AROMA) for s in pair]
    out += education.EFFECTS.values()
    out += education.STRAIN_TYPES.values()
    return [s for s in out if s]


def _violations(text: str) -> list[str]:
    bad = compliance.therapeutic_hits(text) + compliance.injection_hits(text)
    bad += [f"rule:{name}" for name, pat in WEBSITE_RULES.items() if re.search(pat, text, re.IGNORECASE)]
    bad += [w for w in BANNED_MORE if w != "beta" and re.search(rf"\b{w}", text, re.IGNORECASE)]
    return bad


class EducationComplianceTests(SimpleTestCase):
    def test_nothing_the_page_can_say_trips_a_content_rule(self):
        strings = _every_string()
        self.assertGreater(len(strings), 30)  # the sweep actually covers the tables
        for text in strings:
            self.assertEqual(_violations(text), [], text)

    def test_the_screen_fires_on_the_old_wording(self):
        # control: the exact phrases this file used to carry must be caught, or the sweep proves nothing
        for old in ("relaxing and sedating; the classic couch-lock feel", "associated with stress relief",
                    "known for anti-inflammatory, soothing effects", "widely associated with anti-anxiety effects",
                    "Reported relief from aches and tension.", "oriented toward calm and relief"):
            self.assertNotEqual(_violations(old), [], old)

    def test_a_terpenes_effect_is_the_single_source_wording(self):
        for key, entry in terpenes.NOTES.items():
            aroma, effect = education.terpene_info(key)
            if entry["lean"]:
                self.assertIn(f"described as {entry['lean']}", effect, key)
            else:
                self.assertEqual(effect, "", key)  # no lean to claim: no effect sentence at all
            self.assertTrue(aroma)

    def test_vendor_spellings_resolve_and_unknowns_do_not(self):
        self.assertEqual(education.terpene_info("Beta-Caryophyllene"), education.terpene_info("caryophyllene"))
        self.assertEqual(education.terpene_info("alpha-pinene"), education.terpene_info("pinene"))
        self.assertIsNone(education.terpene_info("unobtainium"))
        self.assertIsNone(education.terpene_info(None))

    def test_the_condition_tags_get_no_blurb(self):
        for tag in ("pain", "pain relief", "anxiety", "stress"):
            self.assertIsNone(education.effect_info(tag), tag)
        self.assertTrue(education.effect_info("relaxed"))
