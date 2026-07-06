"""Cart-aware cross-sell — complement ladder, price gate, stock floor, co-purchase recency."""

from pos import pairing


def _item(pid, cat_key, price, qty=10, brand="B", margin=0.5, sub=""):
    return {
        "product_id": str(pid), "ProductId": pid, "name": f"P{pid}", "brand": brand,
        "category": cat_key.title(), "cat_key": cat_key, "cat_label": cat_key.title(),
        "price": price, "qty": qty, "margin_pct": margin, "subcategory": sub,
        "strain_type": "hybrid", "strain": "", "terpene": "", "effects": [], "flavors": [],
        "thc": 22, "price_z": 0.0, "UnitPrice": price,
    }


def test_returns_cheaper_lighter_complement():
    anchor = _item(1, "flower", 40)
    items = [anchor, _item(2, "pre-rolls", 8), _item(3, "edibles", 12), _item(4, "flower", 50)]
    pairs = pairing.pair_for(items, anchor, None, n=3)
    assert pairs
    assert all(p["cat_key"] in ("pre-rolls", "edibles", "tinctures") for p in pairs)  # ladder
    assert all(p["price"] <= 20 for p in pairs)                                       # <=50% of 40
    assert all("why" in p and "pair_strength" in p for p in pairs)


def test_price_gate_and_stock_floor():
    anchor = _item(1, "flower", 40)
    items = [anchor,
             _item(2, "pre-rolls", 30),       # 30 > 20 -> price-gated out
             _item(3, "edibles", 3, qty=2)]   # qty 2 < MIN_STOCK(5) -> stock-gated out
    assert pairing.pair_for(items, anchor, None) == []


def test_no_anchor_or_items():
    assert pairing.pair_for([], None, None) == []
    assert pairing.pair_for([_item(2, "edibles", 5)], None, None) == []


def test_copurchase_recency_drives_reason():
    anchor = _item(1, "flower", 40)
    cand = _item(2, "pre-rolls", 8)
    profile = {"purchase_history": [{"product_id": "2", "times_bought": 3}]}
    pairs = pairing.pair_for([anchor, cand], anchor, profile, n=1)
    assert pairs and "restock" in pairs[0]["why"].lower()       # bought 2+ times -> replenish copy
    assert pairs[0]["pair_strength"] > 0
