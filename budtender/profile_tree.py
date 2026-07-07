"""Pure customer-profile transform helpers.

These functions only consume profile payload dictionaries and return normalized,
renderable structures for POS profile drill-down.
"""

from __future__ import annotations


def build_category_pref_tree(purchase_history: list[dict]) -> list[dict]:
    """Build ordered category/subcategory buckets from purchase-history rows."""

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
        bucket = (row.get("bucket") or "").strip() or "core"

        cat_node = agg.setdefault(cat, {
            "category": cat,
            "times_bought": 0.0,
            "subcats": {},
            "items": 0,
        })
        sub_node = cat_node["subcats"].setdefault(sub, {
            "subcategory": sub,
            "times_bought": 0.0,
            "buckets": {"traffic": 0, "core": 0, "profit": 0},
            "rows": [],
            "items": 0,
        })
        cat_node["times_bought"] += units
        cat_node["items"] += 1
        sub_node["times_bought"] += units
        sub_node["items"] += 1
        if bucket not in sub_node["buckets"]:
            sub_node["buckets"][bucket] = 0
        sub_node["buckets"][bucket] += 1
        sub_node["rows"].append(dict(row))
        total_units += units

    if total_units <= 0:
        return []

    out = []
    for cat_node in agg.values():
        sub_nodes = sorted(
            cat_node.pop("subcats").values(),
            key=lambda s: s["times_bought"],
            reverse=True,
        )
        for sub_node in sub_nodes:
            sub_node["weight"] = round((sub_node["times_bought"] / total_units), 4)
            sub_node["products"] = sub_node.pop("rows")
            btotal = sum(sub_node["buckets"].values()) or 1
            for bkey, cnt in list(sub_node["buckets"].items()):
                sub_node["buckets"][bkey] = round(cnt / btotal, 4)
        cat_node["weight"] = round(cat_node["times_bought"] / total_units, 4)
        cat_node["subcats"] = sub_nodes
        out.append(cat_node)

    out.sort(key=lambda c: c["times_bought"], reverse=True)
    return out

