"""P5 #12 — mid-call back-edge / correction handling (15-P5 §6 AC-2; §7.1).

Pins the deterministic slot-correction FSM (voice/corrections.py) — code owns the transition, not
the LLM. The load-bearing invariant: a category change clears the slots that don't carry over
(subcategory/size/strain_type/price_tier) and PRESERVES the category-agnostic ones
(effect_desired/budget/store); a single-slot change overwrites just that slot; applying twice ==
once (idempotent). Expected values hand-authored (03-CONVENTIONS.md §5).
"""

from __future__ import annotations

import pytest

from voice import corrections


# ── detect_correction ──────────────────────────────────────────────────────────
def test_detect_category_change_clear_keep_matrix():
    """AC-2: 'actually make it edibles' against a flower flow → a category plan that clears the
    category-dependent slots and keeps effect/budget/store."""
    prev = {
        "category": "flower",
        "size": "3.5g",
        "effect_desired": "relaxed",
        "budget": 40,
        "store": "yakima",
    }
    plan = corrections.detect_correction(prev, "actually make it edibles")
    assert plan is not None
    assert plan.kind == corrections.KIND_CATEGORY
    assert plan.to == "edible"
    assert set(plan.clear) == {"subcategory", "size", "strain_type", "price_tier"}
    assert "effect_desired" in plan.keep
    assert "budget" in plan.keep
    assert "store" in plan.keep


def test_detect_cart_correction_never_concentrate():
    """A 'make it a cart' correction resolves to cartridge, NEVER concentrate (the #4 guard holds
    inside the correction path too)."""
    plan = corrections.detect_correction({"category": "flower"}, "no, make it a 510 cart instead")
    assert plan.kind == corrections.KIND_CATEGORY
    assert plan.to == "cartridge"
    assert plan.to != "concentrate"


def test_detect_budget_change():
    plan = corrections.detect_correction({"category": "flower"}, "actually, change my budget to 60")
    assert plan.kind == corrections.KIND_BUDGET
    assert plan.to == "60"


def test_detect_effect_change():
    plan = corrections.detect_correction(
        {"effect_desired": "relaxed"}, "actually I want something uplifting instead"
    )
    assert plan.kind == corrections.KIND_EFFECT
    assert plan.to == "uplifted"


def test_detect_size_change():
    plan = corrections.detect_correction({"category": "cartridge"}, "make it a 1g instead")
    assert plan.kind == corrections.KIND_SIZE
    assert plan.to.replace(" ", "") in ("1g",)


def test_detect_cancel_stands_alone():
    """A cancel/start-over needs no revision trigger and resets to the category-entry stage."""
    plan = corrections.detect_correction({"category": "flower", "size": "3.5g"}, "cancel that")
    assert plan.kind == corrections.KIND_CANCEL
    assert "category" in plan.clear
    assert "size" in plan.clear


def test_no_revision_trigger_is_not_a_correction():
    """A plain new statement (no 'actually/make it/instead') is normal slot-filling, not a
    correction → None."""
    assert (
        corrections.detect_correction({"category": "flower"}, "and I'd like it for sleep") is None
    )


def test_same_category_is_not_a_correction():
    """Re-stating the SAME category isn't a correction (nothing to reset)."""
    assert corrections.detect_correction({"category": "edible"}, "actually yeah, edibles") is None


def test_empty_intent_returns_none():
    assert corrections.detect_correction({"category": "flower"}, "") is None


# ── apply_correction ───────────────────────────────────────────────────────────
def test_apply_category_change_clears_and_preserves():
    """AC-2: apply clears the downstream slots and preserves the category-agnostic ones exactly as
    the plan says."""
    prev = {
        "category": "flower",
        "size": "3.5g",
        "strain_type": "indica",
        "price_tier": "mid",
        "effect_desired": "relaxed",
        "budget": 40,
        "store": "yakima",
    }
    plan = corrections.detect_correction(prev, "actually make it edibles")
    new = corrections.apply_correction(prev, plan)
    assert new["category"] == "edible"
    # downstream slots cleared
    assert "size" not in new
    assert "strain_type" not in new
    assert "price_tier" not in new
    # category-agnostic slots preserved
    assert new["effect_desired"] == "relaxed"
    assert new["budget"] == 40
    assert new["store"] == "yakima"


def test_apply_is_pure_does_not_mutate_input():
    prev = {"category": "flower", "size": "3.5g"}
    plan = corrections.detect_correction(prev, "make it edibles")
    corrections.apply_correction(prev, plan)
    assert prev == {"category": "flower", "size": "3.5g"}  # untouched


def test_apply_budget_overwrites_price_max():
    prev = {"category": "flower", "price_max": 40}
    plan = corrections.detect_correction(prev, "actually change my budget to 75")
    new = corrections.apply_correction(prev, plan)
    assert new["price_max"] == 75


def test_apply_none_plan_is_noop():
    prev = {"category": "flower", "size": "3.5g"}
    assert corrections.apply_correction(prev, None) == prev


def test_apply_idempotent_twice_equals_once():
    """AC-2: applying the same plan twice == once."""
    prev = {"category": "flower", "size": "3.5g", "effect_desired": "relaxed", "budget": 40}
    plan = corrections.detect_correction(prev, "actually make it edibles")
    once = corrections.apply_correction(prev, plan)
    twice = corrections.apply_correction(once, plan)
    assert once == twice


def test_apply_cancel_resets_to_category_entry():
    prev = {"category": "flower", "size": "3.5g", "effect_desired": "relaxed"}
    plan = corrections.detect_correction(prev, "cancel that, start over")
    new = corrections.apply_correction(prev, plan)
    assert "category" not in new
    assert "size" not in new


# ── correction_from_signal (the budtender member → server contract, §4.3) ───────
def test_signal_category_builds_clear_keep_plan():
    prev = {"category": "flower", "size": "3.5g", "effect_desired": "relaxed", "budget": 40}
    signal = {"kind": "category", "to": "edible", "raw": "actually make it edibles"}
    plan = corrections.correction_from_signal(signal, prev)
    assert plan.kind == corrections.KIND_CATEGORY
    assert plan.to == "edible"
    assert set(plan.clear) == {"subcategory", "size", "strain_type", "price_tier"}
    assert "effect_desired" in plan.keep and "budget" in plan.keep


def test_signal_cartridge_alias_canonicalized():
    signal = {"kind": "category", "to": "510 cart", "raw": "a 510 cart"}
    plan = corrections.correction_from_signal(signal, {"category": "flower"})
    assert plan.to == "cartridge"


def test_signal_unknown_kind_is_none():
    assert corrections.correction_from_signal({"kind": "nonsense", "to": "x"}, {}) is None
    assert corrections.correction_from_signal({}, {}) is None
    assert corrections.correction_from_signal("not a dict", {}) is None


def test_signal_full_roundtrip_clears_stale_slots():
    """End-to-end §4.3: a category signal applied to a stale flower slot-state yields a clean edible
    state — the next suggest_products is internally consistent."""
    prev = {
        "category": "flower",
        "size": "3.5g",
        "strain_type": "indica",
        "effect_desired": "relaxed",
        "budget": 40,
        "store": "yakima",
    }
    signal = {"kind": "category", "to": "edible", "raw": "edibles instead"}
    plan = corrections.correction_from_signal(signal, prev)
    new = corrections.apply_correction(prev, plan)
    assert new == {
        "category": "edible",
        "effect_desired": "relaxed",
        "budget": 40,
        "store": "yakima",
    }


# ── webhook integration: _apply_correction strips the signal + resets slots ──────
@pytest.mark.django_db
def test_webhook_apply_correction_resets_and_drops_signal():
    """AC-2 (contract): a tool-call carrying a correction block routes through apply_correction; the
    corrected args carry the new category with stale slots reset and the 'correction' key gone (so
    it never reaches budtender)."""
    from voice import webhooks

    args = {
        "category": "flower",
        "size": "3.5g",
        "effect_desired": "relaxed",
        "budget": 40,
        "correction": {"kind": "category", "to": "edible", "raw": "make it edibles"},
    }
    out = webhooks._apply_correction(args)
    assert out["category"] == "edible"
    assert "size" not in out
    assert out["effect_desired"] == "relaxed"
    assert out["budget"] == 40
    assert "correction" not in out


def test_webhook_apply_correction_no_signal_passthrough():
    from voice import webhooks

    args = {"category": "cartridge", "size": "1g"}
    assert webhooks._apply_correction(args) == args
