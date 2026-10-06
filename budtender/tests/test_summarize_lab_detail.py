"""summarize_lab(detail=True): the additive lab outputs the chat picks carry.

The default call is the New Drops contract and must not move (test_new_drops.py pins it);
everything new is opt-in, and only UnitId 2 (= %) is ever trusted.
"""
from django.test import SimpleTestCase

from budtender import new_drops
from budtender.tests.test_new_drops import LAB_BEVERAGE, LAB_FLOWER

OLD_KEYS = {"thc", "cbd", "potency_unit", "terpenes", "coa_url"}


class AdditiveTests(SimpleTestCase):
    def test_default_call_keeps_exactly_the_existing_keys_and_top_three(self):
        out = new_drops.summarize_lab(LAB_FLOWER, "Flower")
        self.assertEqual(set(out), OLD_KEYS)
        self.assertEqual(len(out["terpenes"]), 3)

    def test_top_widens_the_list_without_adding_keys(self):
        out = new_drops.summarize_lab(LAB_FLOWER, "Flower", top=5)
        self.assertEqual(set(out), OLD_KEYS)
        self.assertEqual([t["name"] for t in out["terpenes"]],
                         ["Beta-Caryophyllene", "Beta-Myrcene", "Alpha-Pinene", "Limonene"])

    def test_detail_adds_keys_and_leaves_every_existing_value_alone(self):
        plain = new_drops.summarize_lab(LAB_FLOWER, "Flower", top=5)
        rich = new_drops.summarize_lab(LAB_FLOWER, "Flower", top=5, detail=True)
        self.assertEqual({k: rich[k] for k in OLD_KEYS}, plain)
        self.assertEqual(set(rich) - OLD_KEYS, {"total_terpenes", "tested_date", "lab_name",
                                                "minor_cannabinoids", "contaminants"})


class TotalTerpenesTests(SimpleTestCase):
    def test_total_is_the_sum_of_every_reported_percent_terpene_not_just_the_top_n(self):
        out = new_drops.summarize_lab(LAB_FLOWER, "Flower", top=1, detail=True)
        self.assertEqual(len(out["terpenes"]), 1)
        self.assertEqual(out["total_terpenes"], round(1.264 + 1.558 + 0.2399 + 0.11, 2))  # 3.17

    def test_the_labs_own_total_wins_when_the_payload_has_one(self):
        lab = {**LAB_FLOWER, "TotalTerpenes": {"Value": 3.5, "UnitId": 2}}
        self.assertEqual(new_drops.summarize_lab(lab, "Flower", detail=True)["total_terpenes"], 3.5)

    def test_a_lab_total_in_another_unit_is_not_used(self):
        lab = {**LAB_FLOWER, "TotalTerpenes": {"Value": 35, "UnitId": 7}}
        self.assertEqual(new_drops.summarize_lab(lab, "Flower", detail=True)["total_terpenes"], 3.17)

    def test_no_terpenes_means_no_total_not_zero(self):
        self.assertIsNone(new_drops.summarize_lab(LAB_BEVERAGE, "Liquid Edible", detail=True)["total_terpenes"])
        self.assertIsNone(new_drops.summarize_lab({}, "Flower", detail=True)["total_terpenes"])


class UnitTests(SimpleTestCase):
    def test_a_terpene_in_another_unit_is_omitted_from_the_list_and_the_sum(self):
        lab = {"Terpenes": {"BetaMyrcene": {"Value": 0.8, "UnitId": 2}, "Limonene": {"Value": 9.9, "UnitId": 7}}}
        out = new_drops.summarize_lab(lab, "Flower", top=5, detail=True)
        self.assertEqual([t["name"] for t in out["terpenes"]], ["Beta-Myrcene"])
        self.assertEqual(out["total_terpenes"], 0.8)

    def test_a_terpene_value_with_no_unit_is_not_guessed_in_detail_mode(self):
        lab = {"Terpenes": {"BetaMyrcene": {"Value": 0.8, "UnitId": None}}}
        self.assertEqual(new_drops.summarize_lab(lab, "Flower", detail=True)["terpenes"], [])
        self.assertIsNone(new_drops.summarize_lab(lab, "Flower", detail=True)["total_terpenes"])
        # ...while the default New Drops reading is untouched.
        self.assertEqual(len(new_drops.summarize_lab(lab, "Flower")["terpenes"]), 1)

    def test_potency_with_no_unit_is_not_guessed_in_detail_mode(self):
        lab = {"Cannabinoids": {"Thca": {"Value": 20, "UnitId": None}}}
        self.assertIsNone(new_drops.summarize_lab(lab, "Flower", detail=True)["thc"])
        self.assertEqual(new_drops.summarize_lab(lab, "Flower")["thc"], 17.5)  # default unchanged

    def test_potency_in_another_unit_is_omitted_in_both_modes(self):
        lab = {"Cannabinoids": {"Thc": {"Value": 80, "UnitId": 7}}}
        for detail in (False, True):
            self.assertIsNone(new_drops.summarize_lab(lab, "Vape Cartridge", detail=detail)["thc"])

    def test_edible_slugs_never_show_weight_percent_potency(self):
        # Product.category is the catalog slug (plural); the potency rule must catch it too. The fixture's
        # THC is >= 1, because below 1 the "t >= 1" rule would hide it anyway and this could never fail.
        lab = {"Cannabinoids": {"Thc": {"Value": 12.0, "UnitId": 2}, "Cbd": {"Value": 3.0, "UnitId": 2},
                                "Cbg": {"Value": 0.5, "UnitId": 2}}}
        shown = new_drops.summarize_lab(lab, "flower", detail=True)
        self.assertEqual((shown["thc"], shown["cbd"], shown["minor_cannabinoids"]),
                         (12.0, 3.0, [{"name": "CBG", "pct": 0.5}]))           # the control: flower shows it
        for slug in ("edibles", "beverages", "tinctures", "topicals", "capsules"):
            out = new_drops.summarize_lab(lab, slug, detail=True)
            self.assertEqual((out["thc"], out["cbd"], out["minor_cannabinoids"]), (None, None, []), slug)


class DateAndLabTests(SimpleTestCase):
    def test_tested_date_is_a_plain_date_or_nothing(self):
        def date_of(raw):
            lab = {**LAB_FLOWER, "TestDetails": {"CoaUrl": "https://x.example/a.pdf", "TestedDate": raw}}
            return new_drops.summarize_lab(lab, "Flower", detail=True)["tested_date"]

        self.assertEqual(date_of("2026-09-14T00:00:00"), "2026-09-14")
        self.assertEqual(date_of("2026-09-14"), "2026-09-14")
        self.assertIsNone(date_of("last tuesday"))
        self.assertIsNone(date_of(None))

    def test_lab_name_is_trimmed_text_or_nothing(self):
        def name_of(raw):
            lab = {**LAB_FLOWER, "TestDetails": {"LabName": raw}}
            return new_drops.summarize_lab(lab, "Flower", detail=True)["lab_name"]

        self.assertEqual(name_of("  Confidence Analytics "), "Confidence Analytics")
        self.assertIsNone(name_of(""))
        self.assertIsNone(name_of(None))
        self.assertIsNone(new_drops.summarize_lab(LAB_FLOWER, "Flower", detail=True)["lab_name"])


class DeltaIsomerAndMinorTests(SimpleTestCase):
    """Thc8 / Thc9 / Thc10 are other THC isomers (the capture's delta-9 lives under plain `Thc`), so they are
    neither minors nor added into the THC total: adding Thc9 would count delta-9 twice."""

    CANN = {"Thc": {"Value": 20, "UnitId": 2}, "Thca": {"Value": 5, "UnitId": 2},
            "Thc8": {"Value": 0.9, "UnitId": 2}, "Thc9": {"Value": 0.8, "UnitId": 2},
            "Thc10": {"Value": 0.7, "UnitId": 2}, "Cbg": {"Value": 0.4, "UnitId": 2},
            "Tac": {"Value": 30, "UnitId": 2}}

    def test_the_delta_isomers_and_tac_are_not_minor_cannabinoids(self):
        out = new_drops.summarize_lab({"Cannabinoids": self.CANN}, "flower", detail=True)
        self.assertEqual(out["minor_cannabinoids"], [{"name": "CBG", "pct": 0.4}])

    def test_thc9_is_not_added_into_the_thc_total(self):
        with_isomers = new_drops.summarize_lab({"Cannabinoids": self.CANN}, "flower", detail=True)["thc"]
        plain = new_drops.summarize_lab({"Cannabinoids": {"Thc": self.CANN["Thc"], "Thca": self.CANN["Thca"]}},
                                        "flower", detail=True)["thc"]
        self.assertEqual(with_isomers, plain)
        self.assertEqual(plain, round(20 + 0.877 * 5, 1))


class TerpeneDisplayNameTests(SimpleTestCase):
    """Every display name must read like a terpene name; duplicates of one compound merge to the larger value."""

    EXPECTED = {
        "ThreeCarene": "3-Carene", "AlphaBisabolol": "Alpha-Bisabolol", "AlphaPhellandrene": "Alpha-Phellandrene",
        "AlphaPinene": "Alpha-Pinene", "AlphaTerpinene": "Alpha-Terpinene", "BetaCaryophyllene": "Beta-Caryophyllene",
        "BetaEudesmol": "Beta-Eudesmol", "BetaMyrcene": "Beta-Myrcene", "BetaPinene": "Beta-Pinene",
        "Bisabolol": "Bisabolol", "Borneol": "Borneol", "Camphene": "Camphene",
        "CaryophylleneOxide": "Caryophyllene Oxide", "DeltaTerpinene": "Delta-Terpinene", "Eucalyptol": "Eucalyptol",
        "Farnesene": "Farnesene", "Fenchol": "Fenchol", "GammaTerpinene": "Gamma-Terpinene", "Geraniol": "Geraniol",
        "GeraniolAcetate": "Geraniol Acetate", "Guaiol": "Guaiol", "Humulene": "Humulene", "Isopulegol": "Isopulegol",
        "Limonene": "Limonene", "Linalool": "Linalool", "Nerol": "Nerol", "Nerolidol": "Nerolidol",
        "NerolidolTwo": "Nerolidol", "Ocimene": "Ocimene", "OcimeneOne": "Ocimene", "OcimeneTwo": "Ocimene",
        "PCymene": "p-Cymene", "PIsopropyltoluene": "p-Isopropyltoluene", "Phytol": "Phytol", "Sabinene": "Sabinene",
        "Terpinene": "Terpinene", "Terpineol": "Terpineol", "Terpinolene": "Terpinolene",
        "TransNerolidol": "Trans-Nerolidol", "Valencene": "Valencene", "YTerpinene": "Gamma-Terpinene",
    }

    def test_every_key_in_the_real_lab_payload_has_a_readable_name(self):
        import json
        from pathlib import Path
        path = Path(__file__).parent / "data" / "backoffice_capture_2026-10-05" / "batch_lab_results.json"
        if not path.exists():
            self.skipTest(f"real capture not found: {path}")
        keys = list(json.loads(path.read_text(encoding="utf-8"))["Data"]["Terpenes"])
        self.assertEqual(len(keys), 41)
        self.assertEqual(set(keys), set(self.EXPECTED))              # the table below covers the whole payload
        for key in keys:
            name = new_drops._pretty_terpene(key)
            self.assertEqual(name, self.EXPECTED[key], key)
            self.assertNotRegex(name, r"\b(Three|Two|One)\b|(^| )[A-Za-z]( |$)", key)  # no spelled digits / bare letters

    def test_duplicate_names_keep_the_larger_value_never_the_sum(self):
        # Dutchie's schema does not say whether Ocimene / OcimeneOne / OcimeneTwo are isomers of one total or
        # separate entries; summing could double-count, keeping the larger can only understate.
        terp = {"Ocimene": {"Value": 0.30, "UnitId": 2}, "OcimeneOne": {"Value": 0.20, "UnitId": 2},
                "OcimeneTwo": {"Value": 0.10, "UnitId": 2}, "Nerolidol": {"Value": 0.05, "UnitId": 2},
                "NerolidolTwo": {"Value": 0.07, "UnitId": 2}, "GammaTerpinene": {"Value": 0.02, "UnitId": 2},
                "YTerpinene": {"Value": 0.04, "UnitId": 2}}
        out = new_drops.summarize_lab({"Terpenes": terp}, "flower", top=10, detail=True)
        self.assertEqual({t["name"]: t["value"] for t in out["terpenes"]},
                         {"Ocimene": 0.3, "Nerolidol": 0.07, "Gamma-Terpinene": 0.04})
        self.assertEqual(out["total_terpenes"], round(0.30 + 0.07 + 0.04, 2))   # merged, so no double count
        self.assertEqual(len(new_drops.summarize_lab({"Terpenes": terp}, "flower", top=10)["terpenes"]), 3)
