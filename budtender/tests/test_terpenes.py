"""terpenes.canonical / terpenes.profile — the ONE place a terpene % becomes words.

Only six terpenes carry notes and a lean, taken from the site's approved education text
(happytimeweed/prompts/education-knowledge.md). Every other terpene shows by name and %
with NO invented description. Effect wording is bound by LCB-CONTENT-COMPLIANCE.md §2.1:
experiential words only, hedged, never therapeutic.
"""
import functools
import itertools
import json
import re
from pathlib import Path

from django.test import SimpleTestCase

from budtender import compliance, new_drops, terpenes

CAPTURE = Path(__file__).parent / "data" / "backoffice_capture_2026-10-05" / "batch_lab_results.json"
APPROVED = Path(r"C:\Users\vladi\OneDrive\Desktop\happytimeweed\prompts\education-knowledge.md")

# Words that would turn an experiential line into a therapeutic claim (WAC 314-55-155).
BANNED = ("sleep", "insomnia", "anxiety", "anxious", "pain", "medic", "therap", "heal", "cure",
          "treat", "relief", "relieve", "remed", "symptom", "depress", "inflam", "disease",
          "condition", "doctor", "prescri", "dose", "stress")
# LCB 2.5 (alcohol, tobacco, vehicles), the pharmacological "sedative", and the word 'beta' (the surface adds
# the beta disclaimer; the explanation itself never carries it).
BANNED_MORE = ("wine", "beer", "alcohol", "tobacco", "drive", "driving", "vehicle", "sedat", "beta")

# Local copy of the rules in happytimeweed/scripts/compliance-check.mjs (~L100-121) that can apply to generated
# copy: its ERROR rules for 2.1 / 2.5 / profanity and its two WARNs (sedative, vehicle). budtender.compliance
# covers the 2.1 words but not alcohol, vehicle, sedative or profanity, so the website's own patterns run too.
WEBSITE_RULES = {
    "condition-claim": r"\b(treats?|cures?|relieves?|heals?|alleviates?|manages?)\s+(\w+\s+){0,3}?"
                       r"(pain|anxiety|insomnia|depression|nausea|inflammation|ptsd|arthritis|migraines?|seizures?"
                       r"|glaucoma|symptoms?)\b",
    "relief-claim": r"\b(pain|anxiety|stress|symptom)[ -]relief\b",
    "anti-claim": r"\banti[- ](anxiety|inflammatory|depressant)\b",
    "therapeutic": r"\btherapeutic|medicinal\s+benefits?\b",
    "sedative": r"\bsedat(ive|ing|ion)\b",
    "alcohol-pairing": r"\bpairs?\s+(well\s+)?with\s+(a\s+)?(beer|wine|whiskey|cocktails?|drinks?)\b"
                       r"|\b(wine|beer)\s+pairing\b",
    "vehicle-marketing": r"\broad[- ]trip\b|\bdrive[- ]thru\b|\bfor the road\b",
    "profanity": r"(fuck|shit|bitch|cunt)\w*|\bpussy\b",
}


def _t(name, pct):
    return {"name": name, "pct": pct}


class CanonicalTests(SimpleTestCase):
    def test_strips_the_greek_prefix_and_lowercases(self):
        self.assertEqual(terpenes.canonical("Beta-Myrcene"), "myrcene")
        self.assertEqual(terpenes.canonical("Limonene"), "limonene")
        self.assertEqual(terpenes.canonical("Beta-Caryophyllene"), "caryophyllene")
        self.assertEqual(terpenes.canonical("Alpha-Pinene"), "pinene")
        self.assertEqual(terpenes.canonical("Beta-Pinene"), "pinene")
        self.assertEqual(terpenes.canonical("Delta3 Carene"), "carene")
        self.assertEqual(terpenes.canonical("  Terpinolene "), "terpinolene")

    def test_a_different_compound_is_not_folded_into_a_known_one(self):
        # Oxide / terpineol are not caryophyllene / terpinolene.
        self.assertEqual(terpenes.canonical("Caryophyllene Oxide"), "caryophyllene oxide")
        self.assertEqual(terpenes.canonical("Alpha-Terpineol"), "terpineol")
        self.assertNotIn(terpenes.canonical("Caryophyllene Oxide"), terpenes.NOTES)

    def test_junk_in_junk_out_is_empty(self):
        self.assertEqual(terpenes.canonical(None), "")
        self.assertEqual(terpenes.canonical(""), "")
        self.assertEqual(terpenes.canonical(42), "")


class ProfileForTheSixTests(SimpleTestCase):
    def test_myrcene_is_earthy_and_relaxing(self):
        self.assertEqual(terpenes.profile([_t("Beta-Myrcene", 0.82)]), {
            "lean": "relaxing", "notes": ["earthy"],
            "line": "Myrcene-led (0.82%) — earthy; often described as relaxing.",
            "explain": "Smells earthy (myrcene). Customers often describe profiles like this as relaxing"
                       " — everyone is different.",
            "aroma": ["earthy"]})

    def test_limonene_is_citrus_and_uplifting(self):
        self.assertEqual(terpenes.profile([_t("Limonene", 0.5)]), {
            "lean": "uplifting", "notes": ["citrus"],
            "line": "Limonene-led (0.5%) — citrus; often described as uplifting.",
            "explain": "Smells citrusy (limonene). Customers often describe profiles like this as uplifting"
                       " — everyone is different.",
            "aroma": ["citrus"]})

    def test_pinene_is_pine_and_alert_for_alpha_or_beta(self):
        # The approved text says "pinene (pine -> alert)": the source's own word, not a paraphrase.
        for raw in ("Alpha-Pinene", "Beta-Pinene"):
            self.assertEqual(terpenes.profile([_t(raw, 0.3)]), {
                "lean": "alert", "notes": ["pine"],
                "line": "Pinene-led (0.3%) — pine; often described as alert.",
                "explain": "Smells piney (pinene). Customers often describe profiles like this as alert"
                           " — everyone is different.",
                "aroma": ["pine"]})

    def test_linalool_is_lavender_only_and_calm(self):
        # The approved text says "linalool (lavender -> calm)": "floral" is not in it, so it is not a NOTE
        # (the questionnaire's aroma bucket for it is floral, which is `aroma`, not `notes`).
        self.assertEqual(terpenes.profile([_t("Linalool", 0.41)]), {
            "lean": "calm", "notes": ["lavender"],
            "line": "Linalool-led (0.41%) — lavender; often described as calm.",
            "explain": "Smells floral (linalool). Customers often describe profiles like this as calm"
                       " — everyone is different.",
            "aroma": ["floral"]})

    def test_caryophyllene_is_pepper_with_no_lean(self):
        self.assertEqual(terpenes.profile([_t("Beta-Caryophyllene", 1.56)]), {
            "lean": None, "notes": ["pepper"], "line": "Caryophyllene-led (1.56%) — pepper.",
            "explain": "Smells spicy (caryophyllene).", "aroma": ["spicy"]})

    def test_terpinolene_is_floral_with_no_lean(self):
        # The site text says only "terpinolene (floral)": no effect is claimed for it.
        self.assertEqual(terpenes.profile([_t("Terpinolene", 2)]), {
            "lean": None, "notes": ["floral"], "line": "Terpinolene-led (2%) — floral.",
            "explain": "Smells floral (terpinolene).", "aroma": ["floral"]})


class ProfileBoundaryTests(SimpleTestCase):
    def test_an_unknown_terpene_shows_by_name_and_percent_with_no_invented_description(self):
        out = terpenes.profile([_t("Alpha-Bisabolol", 0.3)])
        self.assertEqual(out, {"lean": None, "notes": [], "line": "Bisabolol-led (0.3%).",
                               "explain": "Top terpenes: bisabolol.", "aroma": []})
        self.assertNotIn("described", out["line"])
        self.assertNotIn("—", out["line"])
        self.assertNotIn("Customers", out["explain"])

    def test_lean_comes_from_the_single_top_terpene_only(self):
        # Caryophyllene leads; myrcene (relaxing) is second and must NOT lend its lean.
        out = terpenes.profile([_t("Beta-Myrcene", 1.2), _t("Beta-Caryophyllene", 1.5)])
        self.assertIsNone(out["lean"])
        self.assertTrue(out["line"].startswith("Caryophyllene-led (1.5%)"))

    def test_unsorted_input_is_ranked_by_pct_not_by_position(self):
        out = terpenes.profile([_t("Limonene", 0.5), _t("Beta-Myrcene", 1.0)])
        self.assertEqual(out["lean"], "relaxing")
        self.assertTrue(out["line"].startswith("Myrcene-led (1%)"))

    def test_notes_cover_the_top_three_known_terpenes_in_order_and_skip_unknown(self):
        out = terpenes.profile([_t("Beta-Caryophyllene", 1.56), _t("Alpha-Bisabolol", 1.3),
                                _t("Beta-Myrcene", 1.26), _t("Alpha-Pinene", 0.24), _t("Limonene", 0.11)])
        self.assertEqual(out["notes"], ["pepper", "earthy"])  # bisabolol invents nothing; pinene is 4th

    def test_nothing_to_describe_is_an_empty_profile_not_a_guess(self):
        for empty in ([], None, [_t("Limonene", 0)], [_t("Limonene", None)], ["junk"], [{"pct": 1}]):
            self.assertEqual(terpenes.profile(empty), {"lean": None, "notes": [], "line": "", "explain": "",
                                                       "aroma": []}, empty)

    def test_lean_follows_the_source_text_exactly(self):
        got = {raw: terpenes.profile([_t(raw, 1)])["lean"]
               for raw in ("Beta-Myrcene", "Linalool", "Limonene", "Alpha-Pinene", "Beta-Caryophyllene",
                           "Terpinolene", "Humulene")}
        self.assertEqual(got, {"Beta-Myrcene": "relaxing", "Linalool": "calm", "Limonene": "uplifting",
                               "Alpha-Pinene": "alert", "Beta-Caryophyllene": None,
                               "Terpinolene": None, "Humulene": None})

    def test_lean_is_only_ever_one_of_the_four_words_or_null(self):
        for entry in terpenes.NOTES.values():
            self.assertIn(entry["lean"], (None, "relaxing", "calm", "uplifting", "alert"))


class ComplianceWordingTests(SimpleTestCase):
    def test_no_generated_string_is_therapeutic(self):
        samples = [[_t(n, p)] for n in ("Beta-Myrcene", "Limonene", "Alpha-Pinene", "Beta-Pinene",
                                         "Linalool", "Beta-Caryophyllene", "Terpinolene",
                                         "Alpha-Bisabolol", "Humulene", "Camphene")
                   for p in (0.05, 0.82, 3.4)]
        samples.append([_t("Beta-Myrcene", 1), _t("Limonene", .9), _t("Linalool", .8), _t("Terpinolene", .7)])
        for terps in samples:
            out = terpenes.profile(terps)
            text = " ".join([out["line"], *out["notes"], out["lean"] or ""]).lower()
            for word in BANNED:
                self.assertIsNone(re.search(word, text), f"{word!r} in {text!r}")

    def test_every_note_table_entry_uses_only_approved_experiential_words(self):
        for key, entry in terpenes.NOTES.items():
            text = " ".join(entry["notes"] + [entry["lean"] or ""]).lower()
            for word in BANNED:
                self.assertIsNone(re.search(word, text), f"{word!r} in the {key} entry")
        self.assertEqual(set(terpenes.NOTES), {"myrcene", "limonene", "pinene", "linalool",
                                                "caryophyllene", "terpinolene"})

    def test_every_customer_visible_string_passes_the_shared_compliance_screen(self):
        """Sweeps EVERYTHING the profile code can say (lines, notes, leans, for every terpene in the real
        lab payload at several strengths, alone and in mixes) against budtender.compliance, the same module
        that screens operator free text."""
        names = ["Beta-Myrcene", "Limonene", "Alpha-Pinene", "Beta-Pinene", "Linalool", "Beta-Caryophyllene",
                 "Terpinolene", "Humulene", "Camphene", "Alpha-Bisabolol", "3-Carene", "Ocimene"]
        if CAPTURE.exists():  # every terpene key the real lab payload carries, through the real name mapper
            names += [new_drops._pretty_terpene(k) for k in
                      json.loads(CAPTURE.read_text(encoding="utf-8"))["Data"]["Terpenes"]]
        strings = set()
        for name in names:
            for pct in (0.01, 0.82, 3.4, 12):
                out = terpenes.profile([_t(name, pct)])
                strings.update([out["line"], out["lean"] or "", out["explain"], *out["notes"], *out["aroma"]])
        for entry in terpenes.NOTES.values():
            strings.update([entry["label"], entry["lean"] or "", *entry["notes"]])
        strings.update(terpenes.profile([_t(n, 1 + i / 10) for i, n in enumerate(names[:6])])["notes"])
        self.assertGreater(len(strings), 20)
        for text in strings:
            self.assertEqual(compliance.therapeutic_hits(text), [], text)
            self.assertEqual(compliance.injection_hits(text), [], text)

    def test_the_wording_matches_the_approved_site_text(self):
        """education-knowledge.md names each terpene's aroma and, for four of them, one effect word."""
        if not APPROVED.exists():
            self.skipTest(f"approved text not found: {APPROVED}")
        text = next(line for line in APPROVED.read_text(encoding="utf-8").splitlines()
                    if "**myrcene**" in line and "**terpinolene**" in line).lower()
        for key, entry in terpenes.NOTES.items():
            segment = text.split(f"**{key}**", 1)[1].split("**", 1)[0]       # e.g. " (earthy → relaxed), "
            for note in entry["notes"]:
                self.assertIn(note, segment, (key, note))                    # no aroma the source does not name
        for key, lean in (("pinene", "alert"), ("linalool", "calm")):        # the words used verbatim
            self.assertEqual(terpenes.NOTES[key]["lean"], lean)
            self.assertIn(lean, text.split(f"**{key}**", 1)[1].split("**", 1)[0])
        for key in ("caryophyllene", "terpinolene"):                         # the source claims no effect
            self.assertIsNone(terpenes.NOTES[key]["lean"])

    def test_an_effect_is_always_hedged(self):
        for raw in ("Beta-Myrcene", "Limonene", "Alpha-Pinene", "Linalool"):
            line = terpenes.profile([_t(raw, 1)])["line"]
            self.assertIn("often described as", line)
        for raw in ("Beta-Caryophyllene", "Terpinolene", "Humulene"):  # no lean -> no effect wording at all
            self.assertNotIn("described", terpenes.profile([_t(raw, 1)])["line"])


EXPERIENCE = " Customers often describe profiles like this as {} — everyone is different."


class ExplainTests(SimpleTestCase):
    """profile()["explain"] / ["aroma"]: the plain-words reading of the strongest three terpenes. Deterministic,
    no model: one aroma sentence, then one hedged experience sentence only when the STRONGEST terpene has a lean."""

    def _p(self, *pairs):
        return terpenes.profile([_t(n, p) for n, p in pairs])

    def test_every_single_terpene_has_its_aroma_and_the_experience_only_when_it_has_a_lean(self):
        want = {"Beta-Myrcene": ("earthy", "earthy", "relaxing"), "Limonene": ("citrusy", "citrus", "uplifting"),
                "Alpha-Pinene": ("piney", "pine", "alert"), "Linalool": ("floral", "floral", "calm"),
                "Beta-Caryophyllene": ("spicy", "spicy", None), "Terpinolene": ("floral", "floral", None)}
        for raw, (word, aroma, lean) in want.items():
            out = self._p((raw, 1.0))
            name = terpenes.canonical(raw)
            self.assertEqual(out["explain"], f"Smells {word} ({name})." + (EXPERIENCE.format(lean) if lean else ""),
                             raw)
            self.assertEqual(out["aroma"], [aroma], raw)

    def test_a_mix_names_up_to_three_terpenes_in_strength_order_and_ignores_the_rest(self):
        out = self._p(("Pinene", 0.2), ("Limonene", 0.9), ("Beta-Myrcene", 1.2), ("Beta-Caryophyllene", 1.0))
        self.assertEqual(out["explain"], "Smells earthy, spicy and citrusy (myrcene, caryophyllene, limonene)."
                         + EXPERIENCE.format("relaxing"))   # the strongest is myrcene; pinene is fourth: unnamed
        self.assertEqual(out["aroma"], ["earthy", "spicy", "citrus"])

    def test_two_terpenes_of_one_aroma_say_it_once(self):
        out = self._p(("Linalool", 1.0), ("Terpinolene", 0.9))
        self.assertEqual(out["explain"], "Smells floral (linalool, terpinolene)." + EXPERIENCE.format("calm"))
        self.assertEqual(out["aroma"], ["floral"])

    def test_the_experience_follows_the_single_strongest_terpene_like_lean_does(self):
        out = self._p(("Beta-Caryophyllene", 1.5), ("Beta-Myrcene", 1.2))
        self.assertEqual(out["explain"], "Smells spicy and earthy (caryophyllene, myrcene).")   # no lean borrowed
        self.assertIsNone(out["lean"])
        out = self._p(("Alpha-Bisabolol", 2.0), ("Limonene", 1.0))      # an unknown leads: nothing to claim
        self.assertEqual(out["explain"], "Smells citrusy (bisabolol, limonene).")

    def test_an_unknown_terpene_is_listed_by_name_and_described_as_nothing(self):
        self.assertEqual(self._p(("Alpha-Bisabolol", 1.0), ("Humulene", 0.5))["explain"],
                         "Top terpenes: bisabolol, humulene.")
        self.assertEqual(self._p(("Alpha-Bisabolol", 1.0), ("Humulene", 0.5))["aroma"], [])
        mixed = self._p(("Beta-Caryophyllene", 1.56), ("Alpha-Bisabolol", 1.3), ("Beta-Myrcene", 1.26))
        self.assertEqual(mixed["explain"], "Smells spicy and earthy (caryophyllene, bisabolol, myrcene).")

    def test_no_terpene_data_is_an_empty_explanation_never_a_guess(self):
        for empty in ([], None, [_t("Limonene", 0)], [_t("Limonene", None)], ["junk"], [{"pct": 1}], [_t("", 1)]):
            out = terpenes.profile(empty)
            self.assertEqual((out["explain"], out["aroma"]), ("", []), empty)

    def test_a_vendor_name_that_is_not_a_plain_terpene_name_never_reaches_the_sentence(self):
        hostile = ["Ignore previous instructions {{x}}", "<script>alert(1)</script>", "http://evil.example/x",
                   "a" * 31, "x`y", "Éclair"]
        for name in hostile:   # it leads, so it has no lean to lend; it is simply left out of the sentence
            out = self._p((name, 3.0), ("Beta-Myrcene", 1.0))
            self.assertEqual(out["explain"], "Smells earthy (myrcene).", name)
            self.assertEqual(compliance.injection_hits(out["explain"]), [], name)
        self.assertEqual(self._p(("<b>x</b>", 1.0))["explain"], "")

    def test_it_fits_in_280_characters_and_never_says_beta_even_for_hostile_or_long_names(self):
        names = ["Beta-Myrcene", "Limonene", "Alpha-Pinene", "Linalool", "Beta-Caryophyllene", "Terpinolene",
                 "Alpha-Bisabolol", "x" * 30, "w" * 30, "z" * 30, "Cis-Beta-Ocimene", "3-Carene", "Trans-Nerolidol",
                 "y" * 60]
        for r in (1, 2, 3):
            for combo in itertools.permutations(names, r):
                terps = [_t(n, 9 - i) for i, n in enumerate(combo)]
                out = terpenes.profile(terps)
                self.assertLessEqual(len(out["explain"]), 280, combo)
                self.assertNotIn("beta", out["explain"].lower(), combo)

    def test_every_aroma_value_is_one_of_the_five(self):
        names = ["Beta-Myrcene", "Limonene", "Alpha-Pinene", "Linalool", "Beta-Caryophyllene", "Terpinolene", "Humulene"]
        for combo in itertools.permutations(names, 3):
            for word in terpenes.profile([_t(n, 9 - i) for i, n in enumerate(combo)])["aroma"]:
                self.assertIn(word, terpenes.AROMA_TERPENES)


class AromaMapTests(SimpleTestCase):
    def test_the_five_aromas_and_their_terpenes(self):
        self.assertEqual(terpenes.AROMA_TERPENES, {
            "citrus": {"limonene"}, "earthy": {"myrcene"}, "pine": {"pinene"},
            "floral": {"linalool", "terpinolene"}, "spicy": {"caryophyllene"}})

    def test_it_is_consistent_with_the_notes_table_so_there_is_one_set_of_known_terpenes(self):
        everywhere = [t for ts in terpenes.AROMA_TERPENES.values() for t in ts]
        self.assertEqual(sorted(everywhere), sorted(set(everywhere)))   # a terpene sits under ONE aroma
        self.assertEqual(set(everywhere), set(terpenes.NOTES))          # the same six, no more, no fewer

    def test_aroma_hit_names_the_strongest_carrier_and_whether_it_leads(self):
        top = [_t("Limonene", 1.5), _t("Beta-Myrcene", 1.0), _t("Beta-Caryophyllene", 0.5)]
        self.assertEqual(terpenes.aroma_hit(top, "citrus"), ("limonene", True))
        self.assertEqual(terpenes.aroma_hit(top, "earthy"), ("myrcene", False))
        self.assertEqual(terpenes.aroma_hit(top, "spicy"), ("caryophyllene", False))
        self.assertIsNone(terpenes.aroma_hit(top, "pine"))
        self.assertEqual(terpenes.aroma_hit(list(reversed(top)), "citrus"), ("limonene", True))  # by pct, not position

    def test_aroma_hit_only_looks_at_the_strongest_three_and_canonicalizes(self):
        fourth = [_t("Beta-Myrcene", 4), _t("Beta-Caryophyllene", 3), _t("Humulene", 2), _t("Limonene", 1)]
        self.assertIsNone(terpenes.aroma_hit(fourth, "citrus"))
        self.assertEqual(terpenes.aroma_hit([_t("Alpha-Pinene", 1)], "pine"), ("pinene", True))
        self.assertIsNone(terpenes.aroma_hit([_t("Caryophyllene Oxide", 1)], "spicy"))   # not caryophyllene

    def test_aroma_hit_is_none_for_no_data_or_an_aroma_that_is_not_one_of_the_five(self):
        top = [_t("Limonene", 1.5)]
        for junk in (None, "", "banana", "Citrus", ["citrus"], {"citrus": 1}, 7):
            self.assertIsNone(terpenes.aroma_hit(top, junk), junk)
        for empty in (None, [], [_t("Limonene", 0)], ["junk"]):
            self.assertIsNone(terpenes.aroma_hit(empty, "citrus"), empty)

    def test_leaning_is_the_one_source_of_the_engines_terpene_hints(self):
        self.assertEqual(terpenes.leaning("relaxing", "calm"), {"myrcene", "linalool"})
        self.assertEqual(terpenes.leaning("uplifting", "alert"), {"limonene", "pinene"})
        self.assertEqual(terpenes.leaning("nothing"), set())


@functools.lru_cache(maxsize=1)
def _every_explanation() -> frozenset:
    """Every `explain` the code can produce. It is a function of the strongest three terpene names, so every
    ordered selection (1-3) of the six known terpenes, two unknown ones and every real name in the lab
    capture covers each fixed phrase there is."""
    names = ["Beta-Myrcene", "Limonene", "Alpha-Pinene", "Linalool", "Beta-Caryophyllene", "Terpinolene",
             "Alpha-Bisabolol", "Humulene"]
    if CAPTURE.exists():
        names += [new_drops._pretty_terpene(k) for k in
                  json.loads(CAPTURE.read_text(encoding="utf-8"))["Data"]["Terpenes"]]
    seen = set()
    for r in (1, 2, 3):
        for combo in itertools.permutations(dict.fromkeys(names), r):
            seen.add(terpenes.profile([_t(n, 9 - i) for i, n in enumerate(combo)])["explain"])
    seen.discard("")
    return frozenset(seen)


class ExplainComplianceTests(SimpleTestCase):
    """EVERY string `explain` can produce, screened: by budtender.compliance (the shared screen), by a local
    copy of the website's own rules, and by the word lists above."""

    def test_the_sweep_is_big_enough_to_mean_something(self):
        self.assertGreaterEqual(len(_every_explanation()), 400)

    def test_none_is_therapeutic_or_an_injection_shape(self):
        for text in _every_explanation():
            self.assertEqual(compliance.therapeutic_hits(text), [], text)
            self.assertEqual(compliance.injection_hits(text), [], text)

    def test_none_trips_the_websites_own_compliance_rules(self):
        for text in _every_explanation():
            for rule, pattern in WEBSITE_RULES.items():
                self.assertIsNone(re.search(pattern, text, re.IGNORECASE), f"{rule}: {text!r}")

    def test_none_uses_a_banned_word_alcohol_vehicle_or_the_beta_disclaimer(self):
        for text in _every_explanation():
            lower = text.lower()
            for word in BANNED + BANNED_MORE:
                self.assertNotIn(word, lower, f"{word!r} in {text!r}")

    def test_the_experience_words_are_only_the_four_approved_leans_and_always_hedged(self):
        for text in _every_explanation():
            if "Customers" in text:
                self.assertRegex(text, r"Customers often describe profiles like this as "
                                       r"(relaxing|uplifting|alert|calm) — everyone is different\.$")

    def test_the_screens_really_fire_on_known_bad_text(self):
        # a control: the sweep above must be able to fail
        for bad in ("relieves anxiety", "pain relief", "sleepy", "pairs well with wine", "a road-trip pick",
                    "deeply sedating"):
            hit = bool(compliance.therapeutic_hits(bad)) or any(
                re.search(p, bad, re.IGNORECASE) for p in WEBSITE_RULES.values())
            self.assertTrue(hit, bad)
