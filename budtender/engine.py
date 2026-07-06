"""
Shared suggestion engine — the ONE scoring + pairing brain used by BOTH the
website/voice API (Django ORM `Product`/`CustomerProfile`) and the in-store POS
(live inventory dicts). Extracted from the two diverged copies so the formula
lives in exactly one place and can never drift again.

The scoring core works on plain dicts:
  • `from_product(p)`  maps a Django `Product` to the canonical feature dict.
  • POS live-inventory dicts already use these keys (they were shaped to match).
  • `profile_dict(profile)` maps a `CustomerProfile` (or passes a dict through).

SUPERSET of both engines: happytime's margin-first/taste-first weights, effect
score, quality-fit, novelty/recency, bucket-mix nudge, owner overrides + the POS
additions (flavor affinity folded into the affinity term, THC-band fit as an
additive nudge, dual category/cat_key lookup). Sensitive fields (margin, bucket,
velocity, price_z) are read for scoring but NEVER serialized — the caller's
allowlist serializer is the sole client boundary (see serializers.public_product).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from django.core.cache import cache

from .models import ManualPairing, Product

# ── Weights ──────────────────────────────────────────────────────────────────
# Anonymous: margin-first. Logged-in: taste leads, margin still matters. These
# are happytime's exact values (unchanged, so the API keeps byte-identical
# behaviour). Flavor is folded INTO the affinity term (not its own weight);
# THC-band fit is an additive nudge below (not a weight) — both like the POS.
W_ANON = {"margin": 0.55, "affinity": 0.0, "effect": 0.18, "category": 0.05, "bucket": 0.12, "quality": 0.0, "budget": 0.10}
W_KNOWN = {"margin": 0.22, "affinity": 0.34, "effect": 0.10, "category": 0.04, "bucket": 0.12, "quality": 0.14, "budget": 0.04}

BUCKET_NUDGE = {"profit": 1.0, "core": 0.4, "traffic": 0.0}
_TIER_CENTER = {"value": -0.6, "mid": 0.0, "top": 0.6}

# Owner policy: never suggest anything with fewer than 5 on the sales floor.
MIN_STOCK = 5

EFFECT_HINTS = {
    "relaxed": {"indica", "myrcene", "linalool", "kush"},
    "uplifted": {"sativa", "limonene", "pinene", "haze"},
    "middle": {"hybrid"},
}


def _request_weights(config: dict | None, profile) -> dict[str, float]:
    """Owner ranking levers from the dashboard, normalized. `profile` truthy →
    known-customer base weights. Unknown keys ignored, negatives dropped."""
    base = W_KNOWN if profile else W_ANON
    if not isinstance(config, dict):
        return base
    raw = config.get("w_known" if profile else "w_anon")
    if not isinstance(raw, dict):
        return base

    weights = dict(base)
    for key in base:
        try:
            value = float(raw[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            weights[key] = value

    if not profile:
        try:
            emphasis = float(config.get("margin_emphasis", 1.0))
        except (TypeError, ValueError):
            emphasis = 1.0
        if math.isfinite(emphasis) and emphasis >= 0:
            weights["margin"] *= emphasis

    total = sum(weights.values())
    if total <= 0 or not math.isfinite(total):
        return base
    return {key: value / total for key, value in weights.items()}


# ── Normalizers (ORM object / profile → the canonical dict the scorer reads) ──
def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def from_product(p: Product) -> dict:
    """Canonical scoring feature dict for a Django Product. POS live-inventory
    dicts already use these keys, so the scorer is written once."""
    return {
        "id": p.sku,
        "brand": p.brand or "",
        "category": p.category or "",
        "cat_key": p.category or "",   # ORM has no separate cat_key; POS does
        "subcategory": p.subcategory or "",
        "strain": p.strain or "",
        "strain_type": p.strain_type or "",
        "terpene": p.dominant_terpene or "",
        "name": p.name or "",
        "thc": p.thc_percent,
        "effects": p.effects or [],
        "flavors": p.flavors or [],
        "price": _f(p.price),
        "price_was": _f(p.price_was) if p.price_was else None,
        "qty": p.quantity_on_hand,
        "margin": _f(p.margin),        # gross profit $ (server-only)
        "margin_pct": _f(p.margin_pct),
        "price_z": _f(p.price_z),
        "bucket": p.bucket or "",
        "velocity": _f(p.velocity),
    }


def profile_dict(profile) -> dict | None:
    """A CustomerProfile model → the affinity dict the scorer reads; a dict is
    passed through unchanged (the POS already loads a dict); None → None."""
    if profile is None or isinstance(profile, dict):
        return profile
    return {
        "brand_affinity": profile.brand_affinity or {},
        "category_affinity": profile.category_affinity or {},
        "strain_type_affinity": profile.strain_type_affinity or {},
        "subcategory_affinity": profile.subcategory_affinity or {},
        "terpene_affinity": profile.terpene_affinity or {},
        "flavor_affinity": profile.flavor_affinity or {},
        "bucket_mix": profile.bucket_mix or {},
        "price_tier": profile.price_tier or "",
        "novelty_score": profile.novelty_score,
        "thc_min": profile.thc_min,
        "thc_max": profile.thc_max,
        "purchase_history": profile.purchase_history or [],
    }


def _aff(pf: dict | None, key: str, val) -> float:
    if not val or not pf:
        return 0.0
    return _f((pf.get(key) or {}).get(val))


# ── Signals ──────────────────────────────────────────────────────────────────
def _flavor_aff(feat: dict, pf: dict | None) -> float:
    """Best match of the product's flavors against the customer's flavor_affinity."""
    fa = (pf or {}).get("flavor_affinity") or {}
    if not fa:
        return 0.0
    return max((_f(fa.get(fl)) for fl in (feat.get("flavors") or []) if fl), default=0.0)


def _affinity_score(feat: dict, pf: dict | None) -> float:
    """Brand + strain_type lead; category (dual raw/cat_key lookup), subcategory,
    terpene and flavor fill in. Flavor is folded in here (no separate weight)."""
    if not pf:
        return 0.0
    s = 0.0
    s += 1.6 * _aff(pf, "brand_affinity", feat.get("brand"))
    s += 1.0 * _aff(pf, "strain_type_affinity", feat.get("strain_type"))
    s += 0.6 * max(_aff(pf, "category_affinity", feat.get("category")),
                   _aff(pf, "category_affinity", feat.get("cat_key")))
    s += 0.6 * _aff(pf, "subcategory_affinity", feat.get("subcategory"))
    s += 0.4 * _aff(pf, "terpene_affinity", feat.get("terpene"))
    s += 0.4 * _flavor_aff(feat, pf)
    return min(s, 1.0)


def _effect_score(feat: dict, desired: str | None) -> float:
    if not desired:
        return 0.0
    hints = EFFECT_HINTS.get(desired, set())
    hay = f"{feat.get('strain','')} {feat.get('strain_type','')} {feat.get('terpene','')} {feat.get('name','')}".lower()
    return 1.0 if any(h in hay for h in hints) else 0.0


def _quality_fit(feat: dict, pf: dict | None) -> float:
    """1.0 at the customer's usual price tier, fading with peer-relative price_z distance."""
    if not pf or not pf.get("price_tier"):
        return 0.0
    center = _TIER_CENTER.get(pf.get("price_tier"), 0.0)
    return 1.0 - min(abs(_f(feat.get("price_z")) - center) / 2.0, 1.0)


def _thc_band_fit(feat: dict, pf: dict | None) -> float:
    """1.0 when product THC sits in the customer's usual [thc_min, thc_max] band,
    fading over ~10pp outside it. 0 when the band or the product THC is unknown."""
    if not pf:
        return 0.0
    lo, hi, thc = pf.get("thc_min"), pf.get("thc_max"), feat.get("thc")
    if lo is None or hi is None or thc in (None, ""):
        return 0.0
    lo, hi, thc = _f(lo), _f(hi), _f(thc)
    if lo > hi:
        lo, hi = hi, lo
    if lo <= thc <= hi:
        return 1.0
    dist = (lo - thc) if thc < lo else (thc - hi)
    return max(0.0, 1.0 - dist / 10.0)


def _novelty_bias(feat: dict, pf: dict | None) -> float:
    """Habitual buyers get a boost for brands they already buy; explorers get a
    boost for brands they have NOT bought. Roughly -0.3..+0.3."""
    brand = feat.get("brand")
    if not pf or not brand:
        return 0.0
    known = _aff(pf, "brand_affinity", brand) > 0
    nov = _f(pf.get("novelty_score"))
    return 0.3 * (1.0 - nov) if known else 0.3 * nov


def _recent_affinity(pf: dict | None, top: int = 3) -> tuple[set, set]:
    """Brand/category from the customer's MOST RECENT purchases (RFM recency)."""
    hist = (pf or {}).get("purchase_history") or []
    items = [h for h in hist if isinstance(h, dict) and h.get("last_bought_at")]
    items.sort(key=lambda h: str(h.get("last_bought_at")), reverse=True)
    recent = items[:top]
    return ({h.get("brand") for h in recent if h.get("brand")},
            {h.get("category") for h in recent if h.get("category")})


def _recency_boost(feat: dict, recent_brands: set, recent_cats: set) -> float:
    b = 0.0
    if feat.get("brand") and feat.get("brand") in recent_brands:
        b += 0.10
    if feat.get("category") and feat.get("category") in recent_cats:
        b += 0.05
    return b


def score_one(feat: dict, pf: dict | None, ctx: dict) -> float:
    """The ONE demand score. `ctx` carries the set-relative values computed once
    per ranking call: W, m_lo, span, mid, desired, category, price_sensitive,
    recent_brands, recent_cats."""
    W = ctx["W"]
    margin_norm = (_f(feat.get("margin")) - ctx["m_lo"]) / ctx["span"]
    mid = ctx["mid"]
    budget_fit = 1 - min(abs(_f(feat.get("price")) - mid) / (mid or 1), 1)
    nudge = BUCKET_NUDGE.get(feat.get("bucket"), 0.4)
    if feat.get("bucket") == "traffic" and ctx["price_sensitive"]:
        nudge = 0.6
    if pf and pf.get("bucket_mix"):
        nudge = 0.6 * nudge + 0.4 * _f((pf.get("bucket_mix") or {}).get(feat.get("bucket"), 0.0))
    return (
        W["margin"] * margin_norm
        + W["affinity"] * _affinity_score(feat, pf)
        + W["effect"] * _effect_score(feat, ctx["desired"])
        + W["category"] * (1.0 if ctx["category"] and feat.get("category") == ctx["category"] else 0.0)
        + W["bucket"] * nudge
        + W["quality"] * _quality_fit(feat, pf)
        + W["budget"] * budget_fit
        + _novelty_bias(feat, pf)
        + _recency_boost(feat, ctx["recent_brands"], ctx["recent_cats"])
        + 0.12 * _thc_band_fit(feat, pf)   # additive nudge (POS superset), like novelty/recency
    )


def why(feat: dict, desired: str | None, pf: dict | None) -> str:
    """A short, PERSUASIVE reason for THIS pick, from real signals only. Ordered
    strongest-converting first: personal hook, live deal, requested effect, real
    potency, genuine scarcity, then flavor/strain fallback."""
    bits: list[str] = []
    brand = feat.get("brand")
    st = feat.get("strain_type") or ""

    if pf and brand and _aff(pf, "brand_affinity", brand) >= 0.25:
        bits.append(f"your go-to {brand}")
    elif pf and st and _aff(pf, "strain_type_affinity", st) >= 0.4:
        bits.append(f"right in your {st.lower()} lane")
    elif pf and feat.get("subcategory") and _aff(pf, "subcategory_affinity", feat["subcategory"]) >= 0.4:
        bits.append(f"your usual {feat['subcategory']}")
    elif pf and pf.get("price_tier") and _quality_fit(feat, pf) >= 0.7:
        bits.append("exactly your usual quality")

    pw, pr = _f(feat.get("price_was")), _f(feat.get("price"))
    if pw - pr >= 1:
        bits.append(f"on sale — save ${pw - pr:.0f}")

    if desired and _effect_score(feat, desired):
        bits.append(f"dialed in for {desired}")

    if _f(feat.get("thc")) >= 25:
        bits.append(f"hits hard at {_f(feat.get('thc')):.0f}% THC")

    q = feat.get("qty")
    try:
        if q is not None and 0 < int(q) <= 5:
            bits.append("almost gone")
    except (TypeError, ValueError):
        pass

    if len(bits) < 2 and feat.get("terpene"):
        bits.append(f"{feat['terpene'].lower()}-forward")
    elif len(bits) < 2 and feat.get("strain"):
        bits.append(feat["strain"])

    picked = [b for b in bits if b][:2]
    if not picked:
        return f"a standout {brand} pick" if brand else "a standout pick"
    s = " · ".join(picked)
    return s[0].upper() + s[1:]


# ── Pairing / upsell (ONE complementary, cheaper, in-stock add-on) ───────────
# ONE pairing core (`_pair_rank`) serves BOTH surfaces: the website (ORM Products,
# Redis co-purchase, one tuple) and the in-store POS menu (live dicts, no Redis,
# top-n list). `_canon_cat` bridges the two category vocabularies so a single
# ladder works for both — the ORM slug vocab (vape-cartridges/concentrates/
# beverages) and the POS cat_key vocab (vapes/concentrate/tinctures).
_CANON_CAT = {
    "flower": "flower",
    "pre-rolls": "pre-rolls", "preroll": "pre-rolls", "pre-roll": "pre-rolls",
    "vapes": "vapes", "vape-cartridges": "vapes", "vape-cartridge": "vapes",
    "vape": "vapes", "cartridge": "vapes", "disposable-vapes": "vapes", "disposables": "vapes",
    "concentrate": "concentrate", "concentrates": "concentrate",
    "edibles": "edibles", "edible": "edibles",
    "tinctures": "tinctures", "tincture": "tinctures",
    "beverages": "beverages", "beverage": "beverages", "drinks": "beverages",
    "topicals": "topicals", "topical": "topicals",
}


def _canon_cat(feat: dict) -> str:
    """Canonical category for the pairing ladder — tries cat_key then category so
    it works for both a live POS dict and an ORM-derived feat."""
    for v in (feat.get("cat_key"), feat.get("category")):
        c = _CANON_CAT.get((v or "").strip().lower())
        if c:
            return c
    return (feat.get("cat_key") or feat.get("category") or "").strip().lower()


# The add-on is a LIGHTER, cheaper category than the anchor — a grab-and-go
# impulse, ordered by attachment rate (pre-rolls lead). Keyed on CANONICAL
# category; every complement name is vocab-invariant (pre-rolls/edibles/
# beverages/tinctures), so it's safe for the ORM `category__in` query too.
LADDER = {
    "flower":       ["pre-rolls", "edibles", "beverages", "tinctures"],
    "concentrate":  ["pre-rolls", "edibles", "beverages", "tinctures"],
    "vapes":        ["pre-rolls", "edibles", "beverages", "tinctures"],
    "pre-rolls":    ["edibles", "beverages", "tinctures"],
    "edibles":      ["beverages", "tinctures"],
    "beverages":    ["edibles", "tinctures"],
    "tinctures":    ["edibles", "beverages"],
    "topicals":     ["edibles", "tinctures"],
}
DEFAULT_COMPLEMENT = ["pre-rolls", "edibles", "beverages"]

MAX_PAIR_PRICE_RATIO = 0.50   # hard gate: pair.price <= 50% of anchor.price
IDEAL_PAIR_PRICE_RATIO = 0.25  # sweet spot the price_fit term peaks at
W_BASKET = 0.40
W_CUSTOMER = 0.25
W_LADDER = 0.15
W_MARGIN = 0.15
W_PRICEFIT = 0.25
RECENT_DAYS = 30


def pair_attr_key(category: str | None, subcategory: str | None) -> str:
    """Durable attribute bucket for the co-purchase matrix: 'flower|3.5g'. Keying
    on attributes (not SKU) means a sold-out item still informs the pairing."""
    return f"{(category or '').strip().lower()}|{(subcategory or '').strip().lower()}"


def _history_index(pf: dict | None) -> dict:
    """Purchase history keyed on the identifier the pairing uses — Dutchie
    product_id when present (POS live dicts), else sku (website SKUs)."""
    out = {}
    for h in (pf or {}).get("purchase_history") or []:
        if isinstance(h, dict):
            key = h.get("product_id") or h.get("sku")
            if key:
                out[str(key)] = h
    return out


def _copurchase_signal(sku: str, hist: dict) -> tuple[float, str]:
    """(score, reason_code) from the customer's own purchase history."""
    h = hist.get(sku)
    if not h:
        return 0.0, ""
    times = int(h.get("times_bought", 0) or 0)
    if times >= 2:
        return 1.0, "bought_2plus_times"
    last = h.get("last_bought_at")
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS):
                return 0.7, "bought_before_not_recent"
        except (ValueError, TypeError):
            pass
        return 0.0, ""
    return 0.4, "bought_before_not_recent"


def _global_popularity(location: str, anchor_sku: str, cand_sku: str) -> float:
    """Nightly co-purchase matrix in Redis: pair:{location}:{sku} -> {sku: weight}."""
    data = cache.get(f"pair:{location}:{anchor_sku}") or {}
    return _f(data.get(cand_sku)) if isinstance(data, dict) else 0.0


def _attr_popularity(location: str, anchor: dict, cand: dict) -> float:
    """Attribute-level 'bought together' weight from the durable matrix
    (pairattr:{loc}:{category|size}) — survives SKU rotation. Reads feat dicts."""
    data = cache.get(f"pairattr:{location}:{pair_attr_key(anchor.get('category'), anchor.get('subcategory'))}") or {}
    if not isinstance(data, dict):
        return 0.0
    return _f(data.get(pair_attr_key(cand.get("category"), cand.get("subcategory"))))


def _reason_text(reason_code, anchor: dict | None, pair: dict | None, pf) -> str:
    """A compelling, human sentence built only from real signals + ATTRIBUTES.
    `anchor`/`pair` are feat dicts (ORM-derived or a live POS dict)."""
    acat = ((anchor.get("category") if anchor else None) or "this").replace("-", " ").rstrip("s")
    pcat = ((pair.get("category") if pair else None) or "add-on").replace("-", " ").rstrip("s")
    if reason_code == "staff_pick":
        return f"Our budtenders hand-pick this {pcat} to go with your {acat}."
    if reason_code == "bought_2plus_times":
        return f"You grab this one a lot — perfect to restock alongside your {acat}."
    if reason_code == "bought_before_not_recent":
        return f"You've loved this before — it's been a minute, and it pairs great with your {acat}."
    if reason_code == "popular_pair":
        return f"Folks who grab a {acat} almost always toss in a {pcat} like this — an easy add-on."
    if reason_code == "your_brand" and pair and pair.get("brand"):
        return f"It's {pair.get('brand')} — right in your wheelhouse — and a different way to enjoy the night."
    if reason_code == "your_lane":
        return f"Matches your taste and complements the {acat} you just picked."
    return f"Round out your {acat} with this {pcat} — a quick, cheap add-on."


def _pair_rank(anchor_feat, cand_feats, pf, *, location=None, n=1):
    """The ONE pairing scorer over feat dicts (used by BOTH pair_for and
    pair_items). Ranks complementary, cheaper, in-stock add-ons by real
    co-purchase + taste + ladder + price-fit + margin. `location` set → also
    consult the Redis co-purchase matrices. Returns up to `n` ranked dicts:
    {id, feat, reason_code, reason_text, strength}."""
    complements = LADDER.get(_canon_cat(anchor_feat), DEFAULT_COMPLEMENT)
    apr = _f(anchor_feat.get("price")) or 1.0
    cands = [f for f in cand_feats
             if _canon_cat(f) in complements
             and _f(f.get("qty")) >= MIN_STOCK
             and str(f.get("id")) != str(anchor_feat.get("id"))
             and 0 < _f(f.get("price")) <= MAX_PAIR_PRICE_RATIO * apr]
    if not cands:
        return []
    hist = _history_index(pf)
    margins = [_f(f.get("margin")) for f in cands]
    m_lo, m_hi = min(margins), max(margins)
    span = (m_hi - m_lo) or 1.0

    ranked = []
    for f in cands:
        margin_norm = (_f(f.get("margin")) - m_lo) / span
        ccat = _canon_cat(f)
        ladder_rank = 1 - (complements.index(ccat) / max(len(complements), 1)) if ccat in complements else 0.0
        if f.get("subcategory") and anchor_feat.get("subcategory") and f["subcategory"] == anchor_feat["subcategory"]:
            ladder_rank *= 0.5  # prefer a different size/format, not a near-dupe
        price_fit = max(0.0, 1 - abs(_f(f.get("price")) / apr - IDEAL_PAIR_PRICE_RATIO) / IDEAL_PAIR_PRICE_RATIO)
        co_score, co_reason = _copurchase_signal(str(f.get("id")), hist)
        sku_pop = _global_popularity(location, str(anchor_feat.get("id")), str(f.get("id"))) if location else 0.0
        attr_pop = _attr_popularity(location, anchor_feat, f) if location else 0.0
        basket = max(co_score, sku_pop, attr_pop)
        cust = (0.6 * _affinity_score(f, pf) + 0.4 * _quality_fit(f, pf)) if pf else 0.0
        score = (W_BASKET * basket + W_CUSTOMER * cust + W_LADDER * ladder_rank
                 + W_MARGIN * margin_norm + W_PRICEFIT * price_fit)
        if co_reason:
            reason = co_reason
        elif sku_pop > 0 or attr_pop > 0:
            reason = "popular_pair"
        elif pf and f.get("brand") and _aff(pf, "brand_affinity", f["brand"]) >= 0.25:
            reason = "your_brand"
        elif pf and _affinity_score(f, pf) >= 0.3:
            reason = "your_lane"
        else:
            reason = "pairs_well"
        strength = round(min(1.0, 0.45 * basket + 0.35 * cust + 0.20 * price_fit
                             + (0.15 if co_reason else 0.0)), 3)
        ranked.append((score, {"id": f.get("id"), "reason_code": reason,
                               "reason_text": _reason_text(reason, anchor_feat, f, pf),
                               "strength": strength}))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in ranked[:n]]


def pair_for(location: str, anchor: Product | None, profile):
    """Website upsell: ONE complementary add-on for an ORM Product anchor, with
    the ManualPairing override + Redis co-purchase. Returns
    (pair|None, reason_code, reason_text, strength)."""
    if anchor is None:
        return None, "none", "", 0.0
    pf = profile_dict(profile)

    # Admin-defined manual pairing wins (when the override product is in stock).
    mp = ManualPairing.objects.filter(location_slug=location, anchor_sku=anchor.sku, active=True).first()
    if mp:
        forced = Product.objects.filter(
            location_slug=location, sku=mp.pair_sku, availability=True, quantity_on_hand__gte=MIN_STOCK
        ).first()
        if forced:
            return forced, "staff_pick", _reason_text(
                "staff_pick", from_product(anchor), from_product(forced), pf), 1.0

    anchor_feat = from_product(anchor)
    complements = LADDER.get(_canon_cat(anchor_feat), DEFAULT_COMPLEMENT)
    qs = (Product.objects.filter(location_slug=location, availability=True,
                                 quantity_on_hand__gte=MIN_STOCK, category__in=complements)
          .exclude(sku=anchor.sku))
    by_id = {p.sku: p for p in qs}
    ranked = _pair_rank(anchor_feat, [from_product(p) for p in by_id.values()], pf, location=location, n=1)
    if not ranked:
        return None, "none", "", 0.0
    r = ranked[0]
    return by_id[r["id"]], r["reason_code"], r["reason_text"], r["strength"]


# ── POS dict surface (live-inventory menu) ───────────────────────────────────
# The website ranks ORM Product rows (rank_products above); the in-store menu
# ranks the live product_SearchV2 dicts. Both go through the SAME score_one /
# signals here, so the two can never drift again. These functions keep the POS's
# historical `pos.ranking.*` / `pos.pairing.*` call shapes.

# Live in-session taste overlay: fold THIS visit's taste ({field:{name:count}})
# into the profile's affinity maps, so a new/guest/DB-down shopper personalizes
# the moment they view or add anything. Strong but not total — persisted leads.
_SESSION_AFF = {"category": "category_affinity", "brand": "brand_affinity",
                "strain_type": "strain_type_affinity", "flavor": "flavor_affinity"}
SESSION_WEIGHT = 0.6


def blend_session_taste(profile, taste):
    """Return `profile` (a dict) with this visit's `taste` folded into its
    affinities. Unchanged when there's no taste; builds a profile from taste
    alone when there's no persisted profile (so ranking uses taste-first weights)."""
    if not taste or not any(taste.get(f) for f in _SESSION_AFF):
        return profile
    eff = dict(profile or {})
    for field, akey in _SESSION_AFF.items():
        counts = taste.get(field) or {}
        if not counts:
            continue
        mx = max(counts.values()) or 1
        merged = dict(eff.get(akey) or {})
        for name, c in counts.items():
            merged[name] = min(1.0, _f(merged.get(name)) + SESSION_WEIGHT * (c / mx))
        eff[akey] = merged
    return eff


def rank(items, profile=None):
    """Rank plain live-inventory dicts best-first for this customer (the POS menu
    'For You'). Each returned dict is annotated with `score` + `why`. Margin-first
    when profile is None. Uses the SAME score_one as the ORM path."""
    items = list(items)
    if not items:
        return []
    pf = profile_dict(profile)
    W = W_KNOWN if pf else W_ANON
    feats = []
    for it in items:
        f = dict(it)
        # POS live dicts carry margin_pct (happytime enrichment), not gross $ —
        # derive a set-relative stand-in so score_one's margin_norm is uniform.
        if "margin" not in f:
            f["margin"] = _f(f.get("margin_pct")) * _f(f.get("price"))
        feats.append(f)
    margins = [f["margin"] for f in feats]
    m_lo, m_hi = min(margins), max(margins)
    span = (m_hi - m_lo) or 1.0
    prices = [_f(f.get("price")) for f in feats if _f(f.get("price")) > 0]
    mid = (min(prices) + min(max(prices), 200)) / 2 if prices else 0.0
    recent_brands, recent_cats = _recent_affinity(pf)
    ctx = {
        "W": W, "m_lo": m_lo, "span": span, "mid": mid, "desired": None,
        "category": None, "price_sensitive": bool(pf and pf.get("price_tier") == "value"),
        "recent_brands": recent_brands, "recent_cats": recent_cats,
    }
    out = []
    for orig, f in zip(items, feats):
        r = dict(orig)
        r["score"] = score_one(f, pf, ctx)
        r["why"] = why(f, None, pf) if pf else ""
        out.append(r)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# Cart-aware cross-sell for the POS menu (live dicts) — the SAME _pair_rank core
# as the website pair_for above, just fed live dicts instead of ORM rows and with
# Redis off (the menu call carries no location). One pairing brain, two surfaces.
def pair_items(items, anchor, profile, n=3):
    """Up to `n` complementary add-ons for `anchor` (a live-inventory dict),
    best-first — each a copy of the item dict + `why` + `pair_strength`. [] when
    nothing lighter+cheaper is in stock. The POS menu's cross-sell."""
    if not anchor or not items:
        return []
    pf = profile_dict(profile)

    def _feat(it):
        f = dict(it)
        f["id"] = str(it.get("product_id"))
        if "margin" not in f:   # live dicts carry margin_pct, not gross $
            f["margin"] = _f(f.get("margin_pct")) * _f(f.get("price"))
        return f

    by_id = {str(it.get("product_id")): it for it in items}
    ranked = _pair_rank(_feat(anchor), [_feat(it) for it in items], pf, location=None, n=n)
    return [dict(by_id[r["id"]], why=r["reason_text"], pair_strength=r["strength"]) for r in ranked]
