"""Budtender customer summary — RFM segment + soft one-liner from real signals only."""

import datetime

from pos import persona


def _days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def test_segment_buckets():
    assert persona.segment(0, 5) == "New"
    assert persona.segment(1, 5) == "New"
    assert persona.segment(20, 100) == "Lapsed"
    assert persona.segment(15, 5) == "Champion"
    assert persona.segment(8, 30) == "Loyal"
    assert persona.segment(3, 10) == "Regular"


def test_summarize_builds_line_from_real_signals():
    s = persona.summarize({
        "orders": 14, "last_purchase": _days_ago(5), "price_tier": "mid",
        "novelty_score": 0.7, "strain_type_affinity": {"indica": 0.8},
        "category_affinity": {"Flower": 0.6}, "terpene_affinity": {"myrcene": 0.5},
        "thc_min": 25, "thc_max": 30,
    })
    assert s["segment"] == "Champion"
    assert "usually indica flower" in s["line"] and "myrcene-forward" in s["line"]
    assert "25–30% THC" in s["line"] and "explorer" in s["line"]
    assert "last in 5d" in s["line"]


def test_summarize_omits_unknowns_no_fabrication():
    s = persona.summarize({"orders": 2})
    assert s["segment"] == "Regular"
    assert "usually" not in s["line"] and "THC" not in s["line"] and "forward" not in s["line"]


def test_summarize_creature_of_habit_and_tier():
    s = persona.summarize({"orders": 7, "last_purchase": _days_ago(40),
                           "price_tier": "value", "novelty_score": 0.1})
    assert s["segment"] == "Loyal"
    assert "creature of habit" in s["line"] and "budget" in s["line"]


def test_summarize_none():
    assert persona.summarize(None) is None
