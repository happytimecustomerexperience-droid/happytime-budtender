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

    # Lab terpene profile (the strongest few canonical terpenes). Only the website's similar search
    # passes these; a caller without them (the POS) scores exactly as before.
    tp = _jaccard(a.get("terpenes") or [], b.get("terpenes") or [])
    if tp:
        score += 0.08 * tp
        if tp >= 0.5:
            reasons.append("similar terpene profile")
    if a.get("master") and a.get("master") == b.get("master"):
        score += 0.05

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


# ── Website "find similar" (search v2) ───────────────────────────────────────
# The reasons similarity() writes that a customer may read. Category/bucket/brand-family reasons are
# left out: the card already shows the category, and the bucket is server-side merchandising.
_CUSTOMER_REASONS = ("same strain", "same terpene", "similar terpene profile", "overlapping effects",
                     "overlapping flavors")


def features(p, lab=None, info=None) -> dict:
    """similarity() input for a Product: catalog facts + the stored lab's terpenes and THC. No bucket,
    margin or cost (similarity's bucket term stays off for customer-facing use)."""
    from . import lab_enrich, product_attrs
    from .ranking import _effective_grams, master_of, product_subtype

    terps = sorted(product_attrs.lab_terpenes(lab).items(), key=lambda kv: -kv[1])
    return {
        "category": p.category or "",
        "subcategory": product_subtype(p.name, p.category),
        "strain": product_attrs.norm_text(p.strain),
        "strain_type": (p.strain_type or "").lower(),
        "dominant_terpene": lab_enrich.dominant_terpene(lab) or (p.dominant_terpene or "").lower(),
        "terpenes": [t for t, _ in terps[:3]],
        "effects": list(p.effects or []),
        "flavors": list(p.flavors or []),
        "price": float(p.price or 0),
        "price_z": float(p.price_z or 0),
        "thc": lab_enrich.effective_thc(p.thc_percent, lab) or 0,
        "unit_weight": _effective_grams(p),
        "potency_mg": p.potency_mg,
        "brand": p.brand or "",
        "master": master_of(p, info),
    }


def _why(reasons: list[str]) -> str:
    keep = [r for r in reasons if r.startswith(_CUSTOMER_REASONS)]
    return ("Similar: " + ", ".join(keep[:3]) + ".") if keep else "A similar pick."


def similar_products(location: str, anchor, *, slots: dict | None = None, labs=None,
                     limit: int = 20) -> list[tuple]:
    """[(Product, why)] most similar to `anchor`, in stock only (ranking.eligible), in the anchor's
    catalog category, never the anchor itself (nor another listing with its exact name). Same master
    category first (a disposable anchor shows disposables before carts); within it up to two same-strain
    picks lead, then similarity score; the order is deterministic, so paging by offset is stable."""
    from . import lab_enrich
    from .ranking import eligible

    labs = lab_enrich.Memo() if labs is None else labs
    details = lab_enrich.Memo()
    s = dict(slots or {})
    s["category"] = anchor.category or s.get("category")
    s.pop("subcategory", None)
    cands = eligible(location, s, exclude_skus={anchor.sku}, labs=labs, details=details)
    name = (anchor.name or "").strip().lower()
    cands = [p for p in cands if (p.name or "").strip().lower() != name]
    if not cands:
        return []
    lab_enrich.labs_for([anchor.batch_id] + [p.batch_id for p in cands], memo=labs)
    lab_enrich.details_for([anchor.product_id] + [p.product_id for p in cands], memo=details)
    fa = features(anchor, labs.get(anchor.batch_id) or None, details.get(anchor.product_id) or None)
    scored = []
    for p in cands:
        fb = features(p, labs.get(p.batch_id) or None, details.get(p.product_id) or None)
        res = similarity(fa, fb)
        same_master = bool(fa["master"]) and fa["master"] == fb["master"]
        same_strain = bool(fa["strain"]) and fa["strain"] == fb["strain"]
        scored.append((same_master, same_strain, res["score"], p, res["reasons"]))
    scored.sort(key=lambda t: (not t[0], -t[2], t[3].id))
    # Rule #1 (owner): the first two are same-strain matches whenever two exist (same master first).
    pins = [t for t in scored if t[1]][:2]
    pin_ids = {t[3].id for t in pins}
    ordered = pins + [t for t in scored if t[3].id not in pin_ids]
    return [(t[3], _why(t[4])) for t in ordered[:limit]]
