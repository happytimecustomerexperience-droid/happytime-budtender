"""voice/pricing.py — the menu price IS the out-the-door price. Offline, no network.

Rewritten 2026-08-07. The old version asserted a WA excise + local-sales-tax uplift
(Yakima ×1.48508) that made the agent quote ~48% over the real price on every call. This
dispensary's Dutchie account is ``taxInclusivePricing: true`` — see the evidence trail in
``bundles/tax.py`` (a real Yakima cart whose $27+$25+$15 menu prices checked out at exactly
$67.00). These tests now pin the corrected behaviour so the uplift cannot come back silently.
"""

from voice import pricing


def test_spoken_price_is_tts_friendly():
    """The agent voices price_spoken, never the digits — so '$16.34' can't be read 'dollar 16.34'."""
    assert pricing.spoken(16.34) == "16 dollars and 34 cents"
    assert pricing.spoken(30) == "30 dollars"
    assert pricing.spoken(30.0) == "30 dollars"
    assert pricing.spoken(1) == "1 dollar"  # singular
    assert pricing.spoken(1.05) == "1 dollar and 5 cents"
    assert pricing.spoken(0.5) == "50 cents"  # cents-only
    assert pricing.spoken(16.999) == "17 dollars"  # rounding carry, no "100 cents"
    assert pricing.spoken(0) == ""  # non-positive → say nothing (Numbers-Guard)
    assert pricing.spoken(None) == ""
    assert pricing.spoken("nope") == ""


def test_menu_price_is_spoken_unchanged():
    """The regression guard: a $38 eighth is thirty-eight dollars, not $56.43."""
    assert pricing.otd(38.0, "yakima") == 38.0
    assert pricing.spoken(pricing.otd(38.0, "yakima")) == "38 dollars"


def test_price_is_identical_at_every_store():
    """Tax-inclusive pricing is an account-level Dutchie setting, not a per-store rate."""
    for store in ("yakima", "mount-vernon", "pullman", "combined", None):
        assert pricing.otd(38.0, store) == 38.0


def test_no_uplift_multiplier_survives():
    """The old per-store multiplier is gone. If it reappears, this fails loudly."""
    assert not hasattr(pricing, "otd_multiplier")
    assert not hasattr(pricing, "WA_EXCISE")
    assert not hasattr(pricing, "LOCAL_SALES_TAX")


def test_otd_rounds_to_cents():
    # Values picked to be unambiguous in float (unlike x.xx5, which is not exactly representable
    # and can round either way) — this just pins round()'s actual behaviour.
    assert pricing.otd(12.341, "yakima") == 12.34
    assert pricing.otd(12.346, "yakima") == 12.35


def test_otd_guards_zero_negative_and_junk():
    assert pricing.otd(0, "yakima") == 0.0
    assert pricing.otd(-5, "yakima") == 0.0
    assert pricing.otd(None, "yakima") == 0.0
    assert pricing.otd("not-a-number", "yakima") == 0.0
