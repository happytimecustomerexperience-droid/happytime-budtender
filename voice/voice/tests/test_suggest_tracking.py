"""voice/tools/suggest.py — suggestion tracking (suggestion-analytics-v1): the phone search fetches wide
(12) so dedupe can pick 3 different options, so it asks budtender NOT to record the 12 and then reports
exactly the picks the agent speaks, labelled ``source:"phone"``. Offline, budtender stubbed."""

from __future__ import annotations

import pytest

from voice import budtender_client
from voice.tools import suggest


def _row(sku, brand, strain, rank):
    return {"rank": rank, "sku": sku, "name": f"{brand} {strain}", "brand": brand, "strain": strain,
            "price": 30.0, "thc_percent": 22.0, "why_this": f"why {sku}", "stock_on_hand": 9}


class FakeBudtender:
    def __init__(self, results):
        self.results = results
        self.search_kw: list[dict] = []
        self.shown: list[dict] = []

    def search(self, slots, **kw):
        self.search_kw.append(kw)
        return {"results": self.results[: kw.get("limit", 3)]}

    def suggestions_shown(self, store, picks, **kw):
        self.shown.append({"store": store, "picks": list(picks), **kw})
        return {"ok": True, "recorded": len(picks)}


@pytest.fixture
def fake_bt(monkeypatch):
    def install(results):
        fb = FakeBudtender(results)
        monkeypatch.setattr(budtender_client, "budtender", lambda: fb)
        monkeypatch.setattr(suggest, "budtender", lambda: fb)
        return fb
    return install


def test_search_is_labelled_phone_and_unrecorded_then_only_spoken_picks_are_reported(fake_bt):
    shelf = [_row("A", "Brand A", "Alpha", 1), _row("B", "Brand A", "Beta", 2),  # same brand → deduped
             _row("C", "Brand C", "Gamma", 3), _row("D", "Brand D", "Delta", 4), _row("E", "Brand E", "Eps", 5)]
    fb = fake_bt(shelf)
    ctx = {"call_id": "", "store": "pullman", "session_token": "vc-call-9", "_caller_phone": "+15095550100",
           "recognition_resolved": True}  # a known caller, already resolved at call start

    out = suggest.handle_suggest_products({"store": "pullman", "category": "edibles"}, ctx)

    assert [p["sku"] for p in out["picks"]] == ["A", "C", "D"]
    assert (fb.search_kw[0]["limit"], fb.search_kw[0]["source"], fb.search_kw[0]["record"]) == (12, "phone", False)
    assert len(fb.shown) == 1
    shown = fb.shown[0]
    assert [p["sku"] for p in shown["picks"]] == ["A", "C", "D"]
    assert [p["rank"] for p in shown["picks"]] == [1, 3, 4]  # the rank budtender gave, not the spoken slot
    assert (shown["store"], shown["session_token"], shown["phone"]) == ("pullman", "vc-call-9", "+15095550100")


def test_nothing_found_reports_nothing(fake_bt):
    fb = fake_bt([])
    out = suggest.handle_suggest_products({"store": "yakima", "category": "flower"}, {"call_id": ""})
    assert out["picks"] == [] and fb.shown == []
