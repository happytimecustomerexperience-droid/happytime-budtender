"""Pure product similarity scoring for POS replacement rows and same-session nudges.

Inputs and outputs are plain dicts so this module stays testable without DB or
POS state.
"""

from __future__ import annotations

from .engine import _f


def _jaccard(left: list[str], right: list[str]) -> float:
    ls = {str(x).strip().lower() for x in (left or []) if str(x).strip()}
    rs = {str(x).strip().lower() for x in (right or []) if str(x).strip()}
    if not ls and not rs:
        return 0.0
    inter = ls & rs
    union = ls | rs
    return len(inter) / len(union) if union else 0.0


def _near(v1, v2, scale=5.0):
    try:
        d = abs(_f(v1) - _f(v2))
    except Exception:
        d = 9999.0
    return max(0.0, 1.0 - d / scale)


def similarity(a: dict, b: dict) -> dict:
    """Return score + reasons for product proximity.

    The score is normalized to 0..1 and intentionally bounded by a small pure
    signal set:

    - same category / subcategory (strong)
    - same strain / strain type / bucket (medium+)
    - overlapping effects / flavors / terpene
    - nearby price + potency/size profile
    - nearby THC/potency/weight

    Brand match contributes only a nudge; a vape + edible same-brand should not
    dominate similarity.
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return {"score": 0.0, "reasons": ["missing product data"]}

    reasons: list[str] = []
    score = 0.0

    if str(a.get("category") or "").strip().lower() == str(b.get("category") or "").strip().lower():
        score += 0.30
        cat = (str(a.get("category") or "") or "same category").strip()
        reasons.append(f"same {cat.lower() or 'category'}")
    if str(a.get("subcategory") or "").strip().lower() == str(b.get("subcategory") or "").strip().lower() and a.get("subcategory"):
        score += 0.20
        reasons.append(f"same {a.get('subcategory')}")
    if str(a.get("strain") or "") and str(a.get("strain") or "").strip().lower() == str(b.get("strain") or "").strip().lower():
        score += 0.12
        reasons.append("same strain")
    if str(a.get("strain_type") or "") and str(a.get("strain_type") or "").strip().lower() == str(b.get("strain_type") or "").strip().lower():
        score += 0.07
        reasons.append(f"same {a.get('strain_type')} type")
    if str(a.get("dominant_terpene") or a.get("terpene") or "").strip().lower() and (
            str(a.get("dominant_terpene") or a.get("terpene") or "").strip().lower() ==
            str(b.get("dominant_terpene") or b.get("terpene") or "").strip().lower()):
        score += 0.06
        reasons.append(f"same terpene ({(a.get('dominant_terpene') or a.get('terpene'))})")

    fx = _jaccard(a.get("effects") or [], b.get("effects") or [])
    if fx:
        score += 0.12 * fx
        if fx >= 0.33:
            reasons.append("overlapping effects")

    fl = _jaccard(a.get("flavors") or [], b.get("flavors") or [])
    if fl:
        score += 0.05 * fl
        if fl >= 0.33:
            reasons.append("overlapping flavors")

    if str(a.get("bucket") or "").strip() and str(a.get("bucket") or "").strip() == str(b.get("bucket") or "").strip():
        score += 0.08
        reasons.append(f"same {a.get('bucket')} lane")

    # nearby price lane
    price_delta = abs(_f(a.get("price") or a.get("price_was")) - _f(b.get("price") or b.get("price_was"))
                   ) / max(_f(a.get("price") or a.get("price_was")) or 1.0, 1.0)
    if price_delta < 0.45:
        score += 0.08 * max(0.0, 1 - price_delta / 0.45)

    # price-z similarity is usually cleaner than raw absolute price.
    z_delta = abs(_f(a.get("price_z")) - _f(b.get("price_z"))) / max(
        abs(_f(a.get("price_z"))),
        abs(_f(b.get("price_z"))),
        1.0,
    )
    score += 0.06 * max(0.0, 1 - z_delta / 1.0)

    thc_score = max(0.0, 1.0 - abs(_f(a.get("thc") or a.get("thc_percent")) - _f(b.get("thc") or b.get("thc_percent"))) / 30.0)
    if thc_score > 0:
        score += 0.03 * thc_score

    for key in ("potency_mg", "unit_weight", "unit_grams"):
        v = _near(a.get(key), b.get(key))
        if v:
            score += 0.04 * v
            break

    if a.get("brand") and a.get("brand") == b.get("brand"):
        # context-only signal: good for discoverability, not dominance.
        score += 0.02

    if len(reasons) < 2 and str(a.get("flavors") or "") and str(b.get("flavors") or ""):
        reasons.append("same brand family")

    # Prevent same-category-only inflation from ranking every same-category SKU
    # as a true taste match.
    if score >= 1.0 and a.get("category") and b.get("category") and a.get("category") != b.get("category"):
        score = 0.95

    score = min(1.0, round(score, 4))
    return {"score": score, "reasons": list(dict.fromkeys(reasons))[:4]}
