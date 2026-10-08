"""Regression lock: tailoring (customer_model `derived`) must leave everyone it does not apply to
EXACTLY as before. An anonymous shopper, a brand-new profile and a low-confidence profile (2 orders)
get byte-identical picks, reasons and pairings to the pre-tailoring code (git HEAD d9afeb6), recorded
in data/tailoring_golden_head.json over the synthetic catalogue (tailoring_fixtures).

Regenerate ONLY from the pre-tailoring code:  HHT_WRITE_TAILORING_GOLDEN=1 pytest this file.
"""
import json
import os
from pathlib import Path

from django.test import TestCase

from budtender import engine
from budtender.engine import pair_for
from budtender.ranking import rank_products
from budtender.tests import tailoring_fixtures as fx

GOLDEN = Path(__file__).parent / "data" / "tailoring_golden_head.json"

CATEGORIES = [None, "flower", "edibles", "concentrates", "vape-cartridges", "pre-rolls", "tinctures",
              "solid-edibles", "vape-carts"]
VARIANTS = [{}, {"price_tier": "mid"}, {"price_tier": "value"}, {"price_tier": "top"}, {"sort_by": "potency"},
            {"sort_by": "price_asc"}, {"size": "3.5g"}, {"subcategory": "hybrid"}, {"subcategory": "rosin"},
            {"effect_desired": "relaxed"}, {"subcategory": "gummies"}]
ANCHORS = ["F3", "F5", "F1", "C1", "C5", "V3", "PR2", "E1", "E4", "T1", "TP1"]


def _snapshot(profiles: dict) -> dict:
    out = {}
    for who, prof in profiles.items():
        rows = {}
        for cat in CATEGORIES:
            for v in VARIANTS:
                slots = {"store": fx.LOC, **({"category": cat} if cat else {}), **v}
                key = json.dumps(slots, sort_keys=True)
                rows[key] = [[p.sku, why] for p, why in rank_products(fx.LOC, slots, prof, limit=8)]
        pairs = {}
        for sku in ANCHORS:
            from budtender.models import Product
            anchor = Product.objects.get(location_slug=fx.LOC, sku=sku)
            pair, code, text, strength = pair_for(fx.LOC, anchor, prof)
            pairs[sku] = [pair.sku if pair else None, code, text, strength]
        out[who] = {"rank": rows, "pair": pairs}
    return out


def _pos_snapshot() -> dict:
    """The in-store POS dict surfaces (engine.rank / pair_items) with a plain dict profile."""
    items = [{"product_id": f"x{i}", "name": n, "brand": b, "category": c, "price": pr, "margin_pct": m,
              "qty": 20, "thc": t} for i, (n, b, c, pr, m, t) in enumerate([
                  ("A 3.5g", "ValueCo", "flower", 30, 0.4, 20), ("B 3.5g", "Phat Panda", "flower", 55, 0.3, 29),
                  ("C Pre-Roll", "ValueCo", "pre-rolls", 8, 0.5, 20), ("D Gummies 10mg", "Verdelux", "edibles", 12, 0.4, None),
                  ("E Soda", "Fizz", "beverages", 7, 0.5, None), ("F Tincture", "Verdelux", "tinctures", 14, 0.4, None)])]
    pf = {"brand_affinity": {"Verdelux": 0.6}, "category_affinity": {"edibles": 0.7}, "price_tier": "mid",
          "purchase_history": []}
    return {"rank": [[r["product_id"], r["score"], r["why"]] for r in engine.rank(items, pf)],
            "rank_anon": [[r["product_id"], r["score"], r["why"]] for r in engine.rank(items, None)],
            "pair": [[r["product_id"], r["why"], r["pair_strength"]] for r in engine.pair_items(items, items[1], pf, n=3)]}


class TailoringLeavesUntailoredShoppersUnchanged(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.utils import timezone
        from budtender.models import SyncState
        SyncState.objects.update_or_create(location_slug=fx.LOC, defaults={"last_synced_at": timezone.now()})
        cls.cat, cls.personas = fx.make_all()

    def test_anonymous_new_and_low_confidence_match_head(self):
        snap = {**_snapshot({"anonymous": None, "new": self.personas["new"], "low": self.personas["low"]}),
                "pos": _pos_snapshot()}
        if os.environ.get("HHT_WRITE_TAILORING_GOLDEN") == "1":
            GOLDEN.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
        golden = json.loads(GOLDEN.read_text())
        for who in ("anonymous", "new", "low"):
            for key, picks in golden[who]["rank"].items():
                self.assertEqual(snap[who]["rank"][key], picks, (who, key))
            for sku, pair in golden[who]["pair"].items():
                self.assertEqual(snap[who]["pair"][sku], pair, (who, "pair", sku))
        self.assertEqual(snap["pos"], golden["pos"])
        # the lock is not vacuous: plenty of non-empty result lists were compared
        self.assertGreater(sum(1 for w in ("anonymous", "new", "low")
                               for v in golden[w]["rank"].values() if v), 100)
