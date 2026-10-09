"""voice/tools/suggest.py — the lab-aware speakable pick (2026-10-05).

budtender now returns, on every pick, real lab data (``lab``), allowlisted product facts (``info``)
and a ``size`` label. The handler must turn them into code-built SPEAKABLE fields (same pattern as
``price_spoken``) without copying the raw ``lab``/``info`` through, and must never invent a figure:
every number the agent can read out is a tool value, ``lab: null`` yields no lab fields at all, and
cost/margin/vendor keys — top-level or nested inside ``lab``/``info`` — never reach the pick.
Offline, budtender stubbed.
"""

from __future__ import annotations

import json
import re

import pytest

from voice import budtender_client
from voice.constants import TOOL_SPECS
from voice.tools import _sanitize_args, dispatch, suggest

COA = (
    "https://storage.googleapis.com/storage.getbamboo.com/public/qa-results/"
    "d37e2558-fe7e-41b9-a96a-8b5c5b990670/y4Bgyn6e6wHYbQZmsjhN6upF56FhM7cGTzFbUBkK.pdf"
)

# The owner's real Dutchie backoffice capture, as budtender's public_product now serializes it.
LAB = {
    "total_terpenes": 10.0,
    "terpenes": [
        {"name": "Terpinolene", "pct": 2.7},
        {"name": "Beta-Myrcene", "pct": 2.0},
        {"name": "Beta-Caryophyllene", "pct": 0.93},
        {"name": "Limonene", "pct": 0.65},
        {"name": "Humulene", "pct": 0.32},
    ],
    "cbd_total": None,
    "thc_total": 74.5,
    "minor_cannabinoids": [
        {"name": "CBG", "pct": 1.5},
        {"name": "CBC", "pct": 0.54},
        {"name": "THCV", "pct": 0.47},
    ],
    "tested_date": "2026-08-27",
    "lab_name": None,
    "coa_url": COA,
    "contaminants": {
        "pesticides": "pass",
        "heavy_metals": "pass",
        "mycotoxin": "pass",
        "solvents": "pass",
    },
    "profile": {"lean": None, "notes": ["floral", "earthy", "pepper"],
                "line": "Terpinolene-led (2.7%) — floral."},
}
INFO = {
    "strain_type": "Hybrid",
    "brand": "Dabstract",
    "doh_approved": True,
    "tags": ["1g", "Live Resin"],
    "ecom_category": "Vaporizers",
    "ecom_subcategory": "disposables",
}


def _row(**over) -> dict:
    row = {
        "rank": 1, "sku": "DAB-LR-1G", "name": "Dabstract Live Resin Cart 1g", "brand": "Dabstract",
        "strain": "Super Lemon Haze", "price": 40.0, "price_was": None, "thc_percent": 74.5,
        "dominant_terpene": "Terpinolene", "stock_on_hand": 12, "dutchie_link": "/catalog/x",
        "image_url": None, "why_this": "Uplifting daytime cart", "coa_url": COA,
        "menu_slug": "dabstract-live-resin-cart-1g", "size": "1g", "lab": LAB, "info": INFO,
    }
    row.update(over)
    return row


def _numbers(text) -> set[float]:
    return {float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(text or ""))}


class FakeBudtender:
    def __init__(self, results=None, check=None):
        self.results = results or []
        self.check = check
        self.search_calls: list[dict] = []

    def search(self, slots, *, limit=3, phone=None, session_token=None, exclude_skus=None,
               location=None, source=None, record=True):
        self.search_calls.append({"slots": dict(slots), "exclude_skus": exclude_skus})
        return {"results": self.results[:limit]}

    def suggestions_shown(self, store, picks, **kw):
        return {"ok": True, "recorded": len(picks)}

    def check_sku(self, store, sku, *, category=None):
        return self.check or {"in_stock": False}

    def pair_for_sku(self, store, anchor_sku, **kw):
        return {"pairing": None, "strength": 0.0}


@pytest.fixture
def fake_bt(monkeypatch):
    fb = FakeBudtender()
    monkeypatch.setattr(budtender_client, "budtender", lambda: fb)
    monkeypatch.setattr(suggest, "budtender", lambda: fb)
    return fb


# ── the owner's sample, end to end ──────────────────────────────────────────────
def test_sample_pick_carries_code_built_speakable_lab_fields():
    pick = suggest._speakable_pick(_row(), "yakima")

    assert pick["size"] == "1g"
    assert pick["thc_percent"] == 74.5
    assert pick["thc_spoken"] == "74.5 percent THC"
    assert "cbd_spoken" not in pick  # cbd_total is null — no number, no field
    assert pick["total_terpenes_spoken"] == "10 percent total terpenes"
    assert pick["terpenes_spoken"] == (
        "terpinolene at 2.7 percent, myrcene at 2 percent, caryophyllene at 0.93 percent"
    )  # top three only, plain names, tool numbers
    assert pick["terpenes"] == [  # the five a "which has more myrcene" answer is read from
        {"name": "terpinolene", "pct": 2.7}, {"name": "myrcene", "pct": 2.0},
        {"name": "caryophyllene", "pct": 0.93}, {"name": "limonene", "pct": 0.65},
        {"name": "humulene", "pct": 0.32},
    ]
    assert pick["minor_cannabinoids_spoken"] == (
        "CBG at 1.5 percent, CBC at 0.54 percent, THCV at 0.47 percent"
    )
    assert pick["profile_line"] == "Terpinolene-led (2.7%) — floral."  # verbatim
    assert pick["lab_screens_passed"] == [
        "pesticides", "heavy metals", "mycotoxins", "residual solvents",
    ]
    assert pick["tested_date"] == "2026-08-27"
    assert pick["coa_url"] == COA
    assert "allergens" not in pick  # info carries none — never inferred
    # The raw payloads are never copied through.
    assert "lab" not in pick and "info" not in pick


def test_every_spoken_number_is_a_number_the_tool_returned():
    """The Numbers-Guard door: a number the agent can read is one that sits in the tool payload."""
    row = _row()
    pick = suggest._speakable_pick(row, "yakima")
    source = _numbers(json.dumps(row))
    for key in ("thc_spoken", "total_terpenes_spoken", "terpenes_spoken", "minor_cannabinoids_spoken"):
        assert _numbers(pick[key]) <= source, (key, pick[key])
    # ...and the "plain name" rewrite never turns one terpene into another.
    assert {t["name"] for t in pick["terpenes"]} == {
        "terpinolene", "myrcene", "caryophyllene", "limonene", "humulene",
    }


def test_handler_emits_the_same_pick_and_the_summary_speaks_potency_and_terpenes(fake_bt):
    fake_bt.results = [_row()]
    # "size" given: without one the price gate (suggest.needs_size) withholds the price this summary ends on.
    out = suggest.handle_suggest_products(
        {"store": "yakima", "category": "cartridge", "size": "1g"}, {"call_id": "", "store": "yakima"}
    )
    pick = out["picks"][0]
    assert pick["terpenes_spoken"].startswith("terpinolene at 2.7 percent")
    assert out["spoken_summary"] == (
        "My top pick is the Dabstract Live Resin Cart 1g — Uplifting daytime cart, "
        "74.5 percent THC, with terpinolene at 2.7 percent, myrcene at 2 percent, "
        "caryophyllene at 0.93 percent, and it's 40 dollars out the door."
    )


# ── lab: null is normal and must never produce a number ─────────────────────────
LAB_FIELDS = (
    "terpenes", "terpenes_spoken", "total_terpenes_spoken", "minor_cannabinoids_spoken",
    "profile_line", "lab_screens_passed", "tested_date", "cbd_spoken",
)


def test_lab_null_pick_carries_no_lab_fields_at_all():
    # dominant_terpene is the OLD inventory field; with no lab on file it must not become a spoken
    # terpene (one source of truth — never a silent fallback to a second one).
    row = _row(lab=None, info=None, coa_url=None, dominant_terpene="Pinene")
    pick = suggest._speakable_pick(row, "yakima")
    for key in LAB_FIELDS:
        assert key not in pick, key
    assert "coa_url" not in pick and "allergens" not in pick
    assert "pinene" not in json.dumps(pick).lower()  # the legacy dominant_terpene is not spoken
    assert pick["thc_spoken"] == "74.5 percent THC"  # potency is the inventory's, still real
    assert pick["size"] == "1g"


def test_a_lab_with_nothing_in_it_is_not_zero_filled():
    pick = suggest._speakable_pick(
        _row(lab={"terpenes": [], "total_terpenes": None, "cbd_total": None, "thc_total": None,
                  "minor_cannabinoids": [], "contaminants": {}, "coa_url": None,
                  "tested_date": None, "profile": {"lean": None, "notes": [], "line": ""}}),
        "yakima",
    )
    for key in LAB_FIELDS:
        assert key not in pick, key


def test_spoken_potency_comes_from_thc_percent_alone_never_from_the_lab_total():
    """One number per fact: budtender folds the lab total into ``thc_percent`` itself, so a row with
    no ``thc_percent`` speaks no potency — even if a (legacy) ``lab.thc_total`` is sitting there."""
    assert "thc_spoken" not in suggest._speakable_pick(_row(thc_percent=None), "yakima")
    assert "thc_spoken" not in suggest._speakable_pick(_row(thc_percent=None, lab=None), "yakima")
    assert "thc_spoken" not in suggest._speakable_pick(_row(thc_percent=0), "yakima")
    lab = {k: v for k, v in LAB.items() if k != "thc_total"}  # the new backend shape
    assert suggest._speakable_pick(_row(lab=lab), "yakima")["thc_spoken"] == "74.5 percent THC"


def test_a_pick_without_a_size_mentions_none_and_a_free_string_lean_is_ignored():
    pick = suggest._speakable_pick(
        _row(size=None, lab=dict(LAB, profile=dict(LAB["profile"], lean="alert"))), "yakima"
    )
    assert "size" not in pick
    assert "lean" not in json.dumps(pick)  # only profile.line is spoken; lean is never read or enum-checked
    assert pick["profile_line"] == "Terpinolene-led (2.7%) — floral."


def test_cbd_is_spoken_only_when_the_lab_has_a_real_number():
    lab = dict(LAB, cbd_total=0.3)
    assert suggest._speakable_pick(_row(lab=lab), "yakima")["cbd_spoken"] == "0.3 percent CBD"


# ── profile_explain: budtender's hedged aroma/experience sentence, verbatim or absent ─────────
EXPLAIN = (
    "Smells floral and earthy (terpinolene, myrcene, caryophyllene). Customers often describe "
    "profiles like this as relaxing — everyone is different."
)


def _lab_with_explain(explain):
    return dict(LAB, profile=dict(LAB["profile"], explain=explain, aroma=["floral", "earthy"]))


def test_profile_explain_is_carried_verbatim_from_lab_profile():
    pick = suggest._speakable_pick(_row(lab=_lab_with_explain(EXPLAIN)), "yakima")
    assert pick["profile_explain"] == EXPLAIN
    assert pick["profile_line"] == "Terpinolene-led (2.7%) — floral."  # the older line is untouched
    assert "lab" not in pick and "info" not in pick and "aroma" not in pick  # still no raw copy-through


@pytest.mark.parametrize("junk", [None, "", "   ", 5, ["Smells floral."], {"a": 1}, "x" * 301])
def test_a_missing_or_malformed_explain_yields_no_field(junk):
    pick = suggest._speakable_pick(_row(lab=_lab_with_explain(junk)), "yakima")
    assert "profile_explain" not in pick


def test_no_lab_means_no_explain_and_the_aroma_list_is_never_spoken_text():
    assert "profile_explain" not in suggest._speakable_pick(_row(lab=None), "yakima")
    # An explain sitting on a row with no lab at all (a malformed payload) is not read from anywhere else.
    assert "profile_explain" not in suggest._speakable_pick(_row(lab=None, profile={"explain": EXPLAIN}), "yakima")


def test_explain_adds_no_number_the_tool_did_not_return():
    row = _row(lab=_lab_with_explain(EXPLAIN))
    pick = suggest._speakable_pick(row, "yakima")
    assert _numbers(pick["profile_explain"]) <= _numbers(json.dumps(row))


def test_check_inventory_carries_explain_too(fake_bt):
    fake_bt.check = {"in_stock": True, "price_otd": 40.0, "stock_on_hand": 12, "name": "X", "size": "1g",
                     "lab": _lab_with_explain(EXPLAIN), "info": None}
    out = suggest.handle_check_inventory(
        {"store": "yakima", "sku": "X", "category": "cartridge", "size": "1g"}, {}
    )
    assert out["profile_explain"] == EXPLAIN
    assert "lab" not in out


# ── malformed lab values are dropped, never repaired into a number ───────────────
@pytest.mark.parametrize("bad", [None, "2.7", True, False, -1, 0, float("nan"), float("inf"), 120])
def test_a_terpene_without_a_usable_percentage_is_dropped(bad):
    lab = dict(LAB, terpenes=[{"name": "Terpinolene", "pct": bad}, {"name": "Limonene", "pct": 0.65}])
    pick = suggest._speakable_pick(_row(lab=lab), "yakima")
    assert pick["terpenes"] == [{"name": "limonene", "pct": 0.65}]
    assert pick["terpenes_spoken"] == "limonene at 0.65 percent"


def test_alpha_and_beta_pinene_are_never_merged_into_one_plain_name():
    lab = dict(LAB, terpenes=[{"name": "Alpha-Pinene", "pct": 0.4}, {"name": "Beta-Pinene", "pct": 0.2}])
    pick = suggest._speakable_pick(_row(lab=lab), "yakima")
    assert [t["name"] for t in pick["terpenes"]] == ["alpha-pinene", "beta-pinene"]


@pytest.mark.parametrize("junk", [None, "pass", ["pesticides"], {"pesticides": "fail"},
                                  {"pesticides": None, "mycotoxin": "PASS "}, {"pesticides": True},
                                  {"unknown_screen": "pass"}])
def test_contaminants_never_imply_a_pass_the_lab_did_not_state(junk):
    pick = suggest._speakable_pick(_row(lab=dict(LAB, contaminants=junk)), "yakima")
    assert "lab_screens_passed" not in pick, pick.get("lab_screens_passed")


def test_contaminants_list_only_the_screens_marked_pass():
    mixed = {"pesticides": "pass", "heavy_metals": None, "mycotoxin": "fail", "solvents": "pass",
             "microbiology": "pass"}
    pick = suggest._speakable_pick(_row(lab=dict(LAB, contaminants=mixed)), "yakima")
    assert pick["lab_screens_passed"] == ["pesticides", "microbials", "residual solvents"]


def test_a_non_date_tested_date_is_dropped():
    assert "tested_date" not in suggest._speakable_pick(_row(lab=dict(LAB, tested_date="last week")), "yakima")


# ── info: allergens verbatim, only when present, never inferred, never cut ───────
def test_allergens_are_quoted_verbatim_and_only_when_present():
    text = "Contains: Milk, Soy Lecithin. May contain tree nuts (almond)."
    pick = suggest._speakable_pick(_row(info=dict(INFO, allergens=text)), "yakima")
    assert pick["allergens"] == text  # byte-for-byte
    # A product NAMED after an allergen with none on file says nothing about allergens.
    peanut = suggest._speakable_pick(_row(name="Peanut Butter Cup 10mg", info=INFO), "yakima")
    assert "allergens" not in peanut


def test_an_oversized_allergen_list_is_omitted_whole_never_cut():
    pick = suggest._speakable_pick(_row(info=dict(INFO, allergens="milk, " * 400)), "yakima")
    assert "allergens" not in pick  # a cut list could drop the one allergen that matters


# ── cost / margin / vendor never reach the pick — top level OR nested ────────────
def test_cost_margin_and_vendor_keys_never_reach_the_pick(fake_bt):
    leaky = _row(
        cost=18.0, margin=20.0, margin_pct=0.52, velocity=3.4, bucket="profit", price_z=0.7,
        vendor="Dabstract Wholesale LLC", Vendor="Dabstract Wholesale LLC", VendorId=88123,
        location_cost=17.5, unit_cost=9.1,
    )
    leaky["lab"] = dict(LAB, cost=11.0, margin=4.0, vendor="Hidden Lab Co", VendorId=7)
    leaky["info"] = dict(INFO, Cost=9.5, Vendor="Dabstract Wholesale LLC", margin=3.2,
                         allergens="Contains: none declared")
    fake_bt.results = [leaky]

    out = suggest.handle_suggest_products(
        {"store": "yakima", "category": "cartridge"}, {"call_id": "", "store": "yakima"}
    )
    blob = json.dumps(out)  # BEFORE the central scrub: the pick itself is already clean
    for needle in ("cost", "margin", "vendor", "velocity", "bucket", "price_z", "88123",
                   "Hidden Lab", "18.0", "17.5", "9.1", "Wholesale"):
        assert needle.lower() not in blob.lower(), needle
    assert out["picks"][0]["allergens"] == "Contains: none declared"  # allowlisted info still rides


@pytest.mark.django_db
def test_the_central_scrub_still_sees_nothing_to_remove(fake_bt):
    fake_bt.results = [_row(cost=18.0, margin=20.0, lab=dict(LAB, cost=1.0), info=dict(INFO, Cost=2.0))]
    out = dispatch("suggest_products", {"store": "yakima", "category": "cartridge"}, {"store": "yakima"})
    assert out["picks"] and "error" not in out  # not redacted wholesale: the pick was clean already
    assert "cost" not in json.dumps(out).lower() and "margin" not in json.dumps(out).lower()


# ── the slot wall: sort_by and exclude_skus survive _sanitize_args ───────────────
def test_sort_by_is_declared_in_the_tool_schema():
    props = TOOL_SPECS["suggest_products"]["parameters"]["properties"]
    assert props["sort_by"]["enum"] == ["potency", "price_asc"]
    assert props["exclude_skus"]["type"] == "array"


@pytest.mark.parametrize("value", ["potency", "price_asc"])
def test_sort_by_survives_the_slot_wall_and_reaches_budtender(fake_bt, value):
    args = _sanitize_args("suggest_products", {"store": "yakima", "category": "flower", "sort_by": value})
    assert args["sort_by"] == value, "the slot wall dropped sort_by"
    fake_bt.results = [_row()]
    suggest.handle_suggest_products(args, {"call_id": "", "store": "yakima"})
    assert fake_bt.search_calls[0]["slots"]["sort_by"] == value


@pytest.mark.parametrize("junk", ["strongest", "", "price_desc", None, 5, ["potency"]])
def test_a_sort_by_budtender_does_not_know_never_reaches_it(fake_bt, junk):
    args = _sanitize_args("suggest_products", {"store": "yakima", "category": "flower", "sort_by": junk})
    fake_bt.results = [_row()]
    suggest.handle_suggest_products({**args, "sort_by": junk}, {"call_id": "", "store": "yakima"})
    assert "sort_by" not in fake_bt.search_calls[0]["slots"]


def test_exclude_skus_survives_the_slot_wall_as_a_clean_list(fake_bt):
    args = _sanitize_args(
        "suggest_products", {"store": "yakima", "category": "flower", "exclude_skus": ["A", "B"]}
    )
    assert args["exclude_skus"] == ["A", "B"], "the slot wall dropped exclude_skus"
    fake_bt.results = [_row()]
    suggest.handle_suggest_products(
        {**args, "exclude_skus": ["A", "", None, {"x": 1}, "B", 7]}, {"call_id": "", "store": "yakima"}
    )
    assert fake_bt.search_calls[0]["exclude_skus"] == ["A", "B", "7"]


# ── check_inventory speaks the same lab facts for a specific SKU ─────────────────
def test_check_inventory_carries_the_same_speakable_facts(fake_bt):
    fake_bt.check = {
        "in_stock": True, "price_otd": 40.0, "stock_on_hand": 12, "name": "Dabstract Live Resin Cart 1g",
        "thc_percent": 74.5, "size": "1g", "lab": LAB, "info": dict(INFO, allergens="None declared"),
        "coa_url": COA, "menu_slug": "dabstract-live-resin-cart-1g",
    }
    out = suggest.handle_check_inventory({"store": "yakima", "sku": "DAB-LR-1G"}, {})
    assert out["thc_spoken"] == "74.5 percent THC" and out["size"] == "1g"
    assert out["terpenes_spoken"].startswith("terpinolene at 2.7 percent")
    assert out["allergens"] == "None declared"
    assert "lab" not in out and "info" not in out


# ── the persona rows say the same thing the tools build ──────────────────────────
@pytest.mark.django_db
def test_budtender_and_written_personas_carry_the_lab_rules_and_are_not_flagged_as_injection():
    from kb import models as m
    from kb.seed import seed_agent_prompts
    from voice.tools.faq import _looks_poisoned

    seed_agent_prompts()
    voice = m.AgentPrompt.objects.get(role="budtender").body
    written = m.AgentPrompt.objects.get(role="written").body

    for body in (voice, written):
        assert not _looks_poisoned(body)  # the same detector api.py refuses a persona row on
        assert "I don't have lab results on file for that one" in body
        assert "check the label" in body and "word for word" in body
        assert "never call a product 'the strongest'" in body.replace("\n", " ")
    # Every figure-bearing field the tools build is named in the phone persona (no silent drift).
    for key in suggest.SPOKEN_FACT_KEYS:
        assert key in voice, key
    assert "sort_by=potency" in voice and "sort_by=price_asc" in voice
    assert "do NOT pass exclude_skus" in voice
    assert "Never speak a web address" in voice
    assert "lab.profile.line" in written and "info.allergens" in written
