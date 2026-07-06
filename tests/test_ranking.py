"""Ranking + suggestion engine unit checks (pure, no DB/network)."""

from pos import ranking, suggest


def _p(**kw):
    base = {"brand": "", "category": "", "subcategory": "", "strain": "", "strain_type": "",
            "terpene": "", "thc": 0, "price": 30, "price_was": 0, "margin_pct": 50,
            "price_z": 0, "bucket": "core", "velocity": 0, "qty": 10, "effects": [],
            "product_id": "x"}
    base.update(kw)
    return base


def test_rank_puts_customers_brand_first():
    items = [_p(product_id="a", brand="Generic"), _p(product_id="b", brand="Phat Panda")]
    profile = {"brand_affinity": {"Phat Panda": 0.9}, "category_affinity": {},
               "strain_type_affinity": {}, "subcategory_affinity": {}, "terpene_affinity": {},
               "bucket_mix": {}, "price_tier": "mid", "novelty_score": 0.2, "purchase_history": []}
    ranked = ranking.rank(items, profile)
    assert ranked[0]["brand"] == "Phat Panda"
    assert ranked[0]["why"]  # a reason is generated for a known customer


def test_rank_anon_is_margin_first():
    items = [_p(product_id="a", brand="A", margin_pct=10), _p(product_id="b", brand="B", margin_pct=90)]
    ranked = ranking.rank(items, None)
    assert ranked[0]["product_id"] == "b"  # higher margin wins when anonymous
    assert ranked[0]["why"] == ""         # no personalized reason without a profile


def test_suggest_favorite_in_stock():
    inv = [_p(product_id="42", brand="1UP", category="vapes", qty=8)]
    profile = {"purchase_history": [{"product_id": "42", "brand": "1UP", "category": "vapes",
                                     "times_bought": 4, "last_bought_at": "2026-06-20"}],
               "category_affinity": {"vapes": 0.8}, "brand_affinity": {"1UP": 0.8}, "orders": 4}
    out = suggest.suggest(inv, profile, limit=5)
    assert out and out[0]["type"] in ("favorite", "fresh_favorite")
    assert out[0]["product_id"] == "42"


def test_suggest_empty_without_profile():
    assert suggest.suggest([_p()], None) == []
