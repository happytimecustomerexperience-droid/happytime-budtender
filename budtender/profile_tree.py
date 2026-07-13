"""Pure customer-profile transform helpers.

These functions only consume profile payload dictionaries and return normalized,
renderable structures for POS profile drill-down.
"""

from __future__ import annotations


def build_category_pref_tree(purchase_history: list[dict]) -> list[dict]:
    """Build ordered category/subcategory trees with normalized share + bucket data.

    Output shape:
    [
      {
        "category": "flower",
        "weight": 0.52,
        "times_bought": 31,
        "subcategories": [
          {
            "subcategory": "7g",
            "weight": 0.41,
            "times_bought": 14,
            "buckets": {"traffic": 2, "core": 8, "profit": 4},
            "products": [...],
          },
        ],
      }
    ]
    """

    def _as_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    agg = {}
    total_units = 0.0
    for row in purchase_history or []:
        if not isinstance(row, dict):
            continue
        cat = (row.get("category") or "other").strip() or "other"
        sub = (row.get("subcategory") or "unclassified").strip() or "unclassified"
        units = _as_float(row.get("times_bought") or row.get("qty") or 0)
        bucket = (row.get("bucket") or "").strip().lower() or "core"

        cat_node = agg.setdefault(cat, {
            "category": cat,
            "times_bought": 0.0,
            "subcategories": {},
        })
        sub_node = cat_node["subcategories"].setdefault(sub, {
            "subcategory": sub,
            "times_bought": 0.0,
            "buckets": {"traffic": 0.0, "core": 0.0, "profit": 0.0},
            "products": [],
        })

        if bucket not in sub_node["buckets"]:
            sub_node["buckets"][bucket] = 0.0
        sub_node["times_bought"] += units
        sub_node["buckets"][bucket] += units
        sub_node["products"].append(dict(row))
        cat_node["times_bought"] += units
        total_units += units

    if total_units <= 0:
        return []

    out = []
    for cat_node in agg.values():
        sub_nodes = sorted(
            cat_node.pop("subcategories").values(),
            key=lambda s: s["times_bought"],
            reverse=True,
        )
        normalized_subs = []
        for sub_node in sub_nodes:
            sub_node["weight"] = round(sub_node["times_bought"] / total_units, 4)
            btotal = sum(sub_node["buckets"].values()) or 1.0
            for bkey, cnt in list(sub_node["buckets"].items()):
                # keep decimal shares for UI bars and stable tests
                sub_node["buckets"][bkey] = round(cnt / btotal, 4)
            sub_node["times_bought"] = int(round(sub_node["times_bought"]))
            normalized_subs.append(sub_node)
        cat_node["subcategories"] = normalized_subs
        cat_node["weight"] = round(cat_node["times_bought"] / total_units, 4)
        cat_node["times_bought"] = int(round(cat_node["times_bought"]))
        out.append(cat_node)

    out.sort(key=lambda c: c["times_bought"], reverse=True)
    return out
