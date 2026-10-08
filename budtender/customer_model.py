"""What a known customer actually buys, as numbers the ranker can use (docs/contracts/customer-memory-v1.md,
the `derived` block). Deterministic, no LLM, no chat text: only `purchase_history` (tasks._fold_history)
joined to the products we sell (Product / ProductDetail tags / BatchLab) and, for pairings, our own
SuggestedProduct rows against later purchases.

  compute_derived(profile) -> dict    exactly the contract's `derived` schema (memory.py stores it in
                                      profile.memory["derived"]; this module never writes the profile)
  derived_for(profile)                the stored `derived`, else computed once per profile object (memo)
  tailor_for(profile) -> Tailor|None  the SOFT ranking signals; None for anonymous / new / low confidence,
                                      so those shoppers rank exactly as they always did

Tailoring is a nudge on top of every hard filter (stock, category, size, price slots, DOH): it only
re-orders what `ranking.eligible` already allowed, and it never touches cost or margin.
"""
from __future__ import annotations

import copy
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import lab_enrich, product_attrs
from .engine import _CANON_CAT, LADDER

# confidence by (estimated) order count
CONF_MED, CONF_HIGH = 3, 8
DECLINE_AFTER_DAYS = 14   # a pairing shown this long ago and never bought is a "no thanks"
REORDER_AT = 0.85         # due for a reorder at 85% of their usual gap
MAX_KEYS = 6              # cap per map, keeps `derived` well inside memory's 4 KB

# ── soft ranking weights (added to engine.score_one's demand score, typically ~0.3-1.2) ──
RATIO_BOOST = 0.30              # candidate's THC:CBD ratio is one they buy (x rank weight below)
RATIO_RANK_WEIGHT = (1.0, 0.75, 0.5, 0.35)
NO_RATIO_PENALTY = 0.10         # ratio-lean shopper, THC-only edible-type candidate
FORM_BOOST = 0.15               # x the candidate form's share (gummy 0.8 -> +0.12)
EXTRACTION_BOOST = 0.20         # x the best share among the candidate's methods
DOSE_BOOST = 0.10               # per-piece mg inside their [min, max]
PRICE_BOOST = 0.08              # price inside their category band p10..p90 widened 25%
PRICE_OUT_STEP = 0.15           # outside it: a step...
PRICE_OUT_PENALTY = 0.25        # ...plus this x relative distance past the band edge (capped at 1)
THC_BOOST = 0.06                # THC inside their category band widened 25%
THC_OUT_STEP = 0.10             # outside it: a step...
THC_OUT_PENALTY = 0.10          # ...plus this x 2 * relative distance (capped at 1)
LAST_BUY_PENALTY = 0.50         # their very last visit's products, not yet due (replaces every fit term)
FIT_NO_VARIETY = 0.30           # a fit this strong is not damped by the ranker's brand-variety factor
REORDER_BOOST = 0.05            # ...and when it IS due
BAND_WIDEN = 0.25
# pairing (engine._pair_rank)
PAIR_ACCEPTED_BOOST = 0.20
PAIR_DECLINED_PENALTY = 0.15
PAIR_ATTR_BOOST = 0.20
# "strong preference" (audit_customer checks a pick matches it when the shelf has a match)
STRONG_RATIO_LEAN, STRONG_FORM, STRONG_EXTRACTION = 0.5, 0.6, 0.5

EMPTY = {"ratio_pref": [], "cbd_lean": 0.0, "forms": {}, "extraction": {}, "dose_mg": {}, "price_by_cat": {},
         "thc_by_cat": {}, "cadence_days": None, "days_since_last": None, "due_for_reorder": False,
         "pairings": {"accepted": [], "declined": []}, "next_likely": [], "confidence": "low"}


def canon(category) -> str:
    raw = str(category or "").strip().lower()
    return _CANON_CAT.get(raw, raw)


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x: float, nd: int = 2):
    x = round(float(x), nd)
    return int(x) if x == int(x) else x


def _wpct(pairs: list[tuple[float, int]], q: float) -> float:
    """Weighted nearest-rank percentile of (value, weight) pairs."""
    vals = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in vals)
    need, acc = q * total, 0
    for v, w in vals:
        acc += w
        if acc >= need - 1e-9:
            return v
    return vals[-1][0]


def _band(pairs) -> dict | None:
    pairs = [(v, w) for v, w in pairs if v is not None]
    if not pairs:
        return None
    return {"p10": _r(_wpct(pairs, 0.10)), "p50": _r(_wpct(pairs, 0.50)), "p90": _r(_wpct(pairs, 0.90))}


def _shares(counter: Counter, total: float) -> dict:
    top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_KEYS]
    return {k: _r(v / total) for k, v in top} if total else {}


# ── history joined to the catalogue ──────────────────────────────────────────
def _history(profile) -> list[dict]:
    if isinstance(profile, dict):
        return [h for h in profile.get("purchase_history") or [] if isinstance(h, dict)]
    return [h for h in (getattr(profile, "purchase_history", None) or []) if isinstance(h, dict)]


def history_rows(profile) -> list[dict]:
    """purchase_history rows joined to the product they name (ONE query each for products, labs and
    details), with the product derivations a customer is described by. A row whose product is gone from
    the catalogue keeps what the row itself recorded (name, category, THC, price)."""
    from .models import Product

    hist = _history(profile)
    if not hist:
        return []
    pids = {str(h.get("product_id")) for h in hist if h.get("product_id")}
    skus = {str(h.get("sku")) for h in hist if h.get("sku")}
    by_pid, by_sku = {}, {}
    for p in Product.objects.filter(product_id__in=pids).order_by("id") if pids else ():
        by_pid.setdefault(p.product_id, p)
    missing = skus - {p.sku for p in by_pid.values()}
    for p in Product.objects.filter(sku__in=missing).order_by("id") if missing else ():
        by_sku.setdefault(p.sku, p)
    prods = {}
    for i, h in enumerate(hist):
        prods[i] = by_pid.get(str(h.get("product_id") or "")) or by_sku.get(str(h.get("sku") or ""))
    labs, details = lab_enrich.Memo(), lab_enrich.Memo()
    lab_enrich.labs_for([p.batch_id for p in prods.values() if p and p.batch_id], memo=labs)
    lab_enrich.details_for([p.product_id for p in prods.values() if p and p.product_id], memo=details)

    rows = []
    for i, h in enumerate(hist):
        p = prods[i]
        if p is None:
            p = Product(name=str(h.get("product_name") or ""), category=str(h.get("category") or ""),
                        strain=str(h.get("strain") or ""), strain_type=str(h.get("strain_type") or ""),
                        potency_mg=_num(h.get("potency_mg")), thc_percent=_num(h.get("thc_percent")))
        lab = (labs.get(p.batch_id) or None) if p.pk else None
        info = (details.get(p.product_id) or None) if p.pk else None
        ratio = product_attrs.cannabinoid_ratio(p, info, lab)
        thc = _num(h.get("thc_percent"))
        if thc is None:
            thc = _num(lab_enrich.effective_thc(p.thc_percent, lab))
        rows.append({
            "sku": str(h.get("sku") or p.sku or ""), "product_id": str(h.get("product_id") or p.product_id or ""),
            "name": p.name or str(h.get("product_name") or ""), "brand": h.get("brand") or p.brand or "",
            "category": str(h.get("category") or p.category or ""),
            "times": max(1, int(_num(h.get("times_bought")) or 1)),
            "price": _num(h.get("last_price")) or (_num(p.price) if p.pk else None) or None,
            "thc": thc,
            "first": _dt(h.get("first_bought_at")) or _dt(h.get("last_bought_at")),
            "last": _dt(h.get("last_bought_at")) or _dt(h.get("first_bought_at")),
            "ratio": ratio, "cbd_dominant": product_attrs.cbd_dominant(p, lab, ratio),
            "form": product_attrs.edible_form(p, info),
            "methods": sorted(product_attrs.primary_methods(product_attrs.extraction_methods(p, info))),
            "piece_mg": product_attrs.piece_mg(p),
            "in_catalogue": bool(p.pk),
        })
    return rows


def orders_estimate(rows) -> int:
    """Visits we can prove from an aggregated history: distinct purchase days, or the most times any one
    product was bought, whichever is larger."""
    if not rows:
        return 0
    days = {d.date() for r in rows for d in (r["first"], r["last"]) if d}
    return max(len(days), max(r["times"] for r in rows))


def _confidence(n: int) -> str:
    return "high" if n >= CONF_HIGH else ("med" if n >= CONF_MED else "low")


def _pair_key(a: str, b: str) -> str | None:
    """'flower|pre-rolls' oriented the way the pairing ladder would offer it (anchor|add-on)."""
    if b in LADDER.get(a, ()):
        return f"{a}|{b}"
    if a in LADDER.get(b, ()):
        return f"{b}|{a}"
    return None


def _pairings(profile, rows, now) -> dict:
    """accepted: add-on categories they took (a shown pairing bought later, or two categories bought the
    same day); declined: a pairing shown >= 14 days ago that was never bought."""
    acc, dec = Counter(), Counter()
    days = defaultdict(set)   # day -> canonical categories bought that day
    for r in rows:
        for d in {x.date() for x in (r["first"], r["last"]) if x}:
            days[d].add(canon(r["category"]))
    for cats in days.values():
        cats = sorted(cats)
        for i, a in enumerate(cats):
            for b in cats[i + 1:]:
                k = _pair_key(a, b)
                if k:
                    acc[k] += 1
    pk = getattr(profile, "pk", None)
    if pk and not isinstance(profile, dict):
        from .models import Product, SuggestedProduct
        shown = list(SuggestedProduct.objects.filter(customer_id=pk, kind="pairing").exclude(paired_with_sku="")
                     .order_by("-shown_at", "-id").values_list("sku", "paired_with_sku", "shown_at", "accepted")[:200])
        if shown:
            want = {s for row in shown for s in row[:2]}
            cat_of = {}
            for sku, cat in Product.objects.filter(sku__in=want).order_by("id").values_list("sku", "category"):
                cat_of.setdefault(sku, cat)
            last_by_sku = {}
            for r in rows:
                if r["last"] and (r["sku"] not in last_by_sku or r["last"] > last_by_sku[r["sku"]]):
                    last_by_sku[r["sku"]] = r["last"]
            for sku, anchor, at, accepted in shown:
                if sku not in cat_of or anchor not in cat_of:
                    continue
                k = f"{canon(cat_of[anchor])}|{canon(cat_of[sku])}"
                at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
                bought = last_by_sku.get(sku)
                if accepted is True or (bought is not None and bought >= at):
                    acc[k] += 1
                elif accepted is False or at <= now - timedelta(days=DECLINE_AFTER_DAYS):
                    dec[k] += 1
    accepted = sorted((k for k in acc if acc[k] >= dec[k]), key=lambda k: (-acc[k], k))[:MAX_KEYS]
    declined = sorted((k for k in dec if dec[k] > acc[k]), key=lambda k: (-dec[k], k))[:MAX_KEYS]
    return {"accepted": accepted, "declined": declined}


def _cadence(rows, n_orders: int) -> int | None:
    firsts = [r["first"] for r in rows if r["first"]]
    lasts = [r["last"] for r in rows if r["last"]]
    if not firsts or not lasts or n_orders < 2:
        return None
    span = (max(lasts) - min(firsts)).total_seconds() / 86400
    if span < 1:
        return None
    return max(1, int(round(span / (n_orders - 1))))


def _next_likely(rows, cadence, accepted, now) -> list[str]:
    """Categories ranked by how much of their buying they are, how recently, and how due; then an add-on
    they take with their top category."""
    units, last_at = Counter(), {}
    for r in rows:
        c = r["category"]
        if not c:
            continue
        units[c] += r["times"]
        if r["last"] and (c not in last_at or r["last"] > last_at[c]):
            last_at[c] = r["last"]
    total = sum(units.values())
    if not total:
        return []
    gap = cadence or 30
    score = {}
    for c, n in units.items():
        since = (now - last_at[c]).total_seconds() / 86400 if c in last_at else gap * 3
        recency = 1.0 / (1.0 + max(since, 0.0) / (gap * 3))
        due = min(max(since, 0.0) / gap, 1.5) / 1.5 if cadence else 0.0
        score[c] = (n / total) * (0.6 + 0.4 * recency) + 0.1 * due * (n / total)
    out = [c for c, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))][:3]
    if out and len(out) < 3:
        top = canon(out[0])
        for k in accepted:
            a, b = k.split("|", 1)
            if a == top and b not in {canon(c) for c in out}:
                out.append(b)
                break
    return out


def compute_derived(profile, *, now: datetime | None = None, rows: list[dict] | None = None) -> dict:
    """The contract's `derived` dict for one profile (CustomerProfile or the POS's profile dict).
    Deterministic for a given history, catalogue and `now`; an empty history gives EMPTY."""
    now = now or datetime.now(timezone.utc)
    rows = history_rows(profile) if rows is None else rows
    if not rows:
        return copy.deepcopy(EMPTY)
    total = sum(r["times"] for r in rows)

    ratios = Counter()
    lean = 0
    forms, methods = Counter(), Counter()
    form_units = method_units = 0
    doses: list[tuple[float, int]] = []
    price_cat, thc_cat, cat_units = defaultdict(list), defaultdict(list), Counter()
    for r in rows:
        w = r["times"]
        if r["ratio"]:
            ratios[r["ratio"]] += w
        if r["ratio"] or r["cbd_dominant"]:
            lean += w
        if r["form"]:
            forms[r["form"]] += w
            form_units += w
        if r["methods"]:
            method_units += w
            for m in r["methods"]:
                methods[m] += w
        if r["piece_mg"]:
            doses.append((r["piece_mg"], w))
        if r["category"]:
            cat_units[r["category"]] += w
            if r["price"]:
                price_cat[r["category"]].append((r["price"], w))
            # THC % means nothing on a gummy or a tincture (a lab's package mg); dose_mg covers those
            if r["thc"] is not None and r["category"] not in product_attrs.RATIO_CATEGORIES:
                thc_cat[r["category"]].append((r["thc"], w))
    top_cats = [c for c, _ in sorted(cat_units.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_KEYS]]

    n_orders = orders_estimate(rows)
    cadence = _cadence(rows, n_orders)
    lasts = [r["last"] for r in rows if r["last"]]
    since = max(0, int((now - max(lasts)).total_seconds() // 86400)) if lasts else None
    due = due_for_reorder(cadence, since)
    pairings = _pairings(profile, rows, now)
    return {
        "ratio_pref": [k for k, _ in sorted(ratios.items(), key=lambda kv: (-kv[1], kv[0]))[:4]],
        "cbd_lean": _r(lean / total),
        "forms": _shares(forms, form_units),
        "extraction": _shares(methods, method_units),
        "dose_mg": ({"min": _r(min(v for v, _ in doses)), "p50": _r(_wpct(doses, 0.5)),
                     "max": _r(max(v for v, _ in doses))} if doses else {}),
        "price_by_cat": {c: b for c in top_cats if (b := _band(price_cat[c]))},
        "thc_by_cat": {c: b for c in top_cats if (b := _band(thc_cat[c]))},
        "cadence_days": cadence,
        "days_since_last": since,
        "due_for_reorder": due,
        "pairings": pairings,
        "next_likely": _next_likely(rows, cadence, pairings["accepted"], now),
        "confidence": _confidence(n_orders),
    }


# ── reading it back (stored, else computed once per request) ─────────────────
def _valid(d) -> bool:
    return isinstance(d, dict) and d.get("confidence") in ("low", "med", "high")


def derived_for(profile) -> dict | None:
    """`profile.memory["derived"]` when memory.py stored one, else compute_derived once per profile
    OBJECT (the memo rides on the instance, so a request that ranks twice computes once). Never writes."""
    if profile is None:
        return None
    if isinstance(profile, dict):
        d = profile.get("derived")
        return d if _valid(d) else None
    memo = getattr(profile, "_hht_derived", None)
    if memo is not None:
        return memo
    mem = getattr(profile, "memory", None)
    stored = mem.get("derived") if isinstance(mem, dict) else None
    d = stored if _valid(stored) else compute_derived(profile)
    try:
        profile._hht_derived = d
    except AttributeError:
        pass
    return d


def _band_of(d: dict, key: str, cat: str) -> tuple[float, float] | None:
    b = (d.get(key) or {}).get(cat) if isinstance(d.get(key), dict) else None
    if not isinstance(b, dict):
        return None
    lo, hi = _num(b.get("p10")), _num(b.get("p90"))
    if lo is None or hi is None:
        return None
    return lo * (1 - BAND_WIDEN), hi * (1 + BAND_WIDEN)


class Tailor:
    """The SOFT signals one med/high-confidence customer's `derived` gives the ranker."""

    def __init__(self, derived: dict, last_skus=(), due: bool | None = None):
        d = derived
        self.d = d
        self.ratio_pref = [r for r in (d.get("ratio_pref") or []) if isinstance(r, str)][:4]
        self.cbd_lean = _num(d.get("cbd_lean")) or 0.0
        self.forms = {k: _num(v) or 0.0 for k, v in (d.get("forms") or {}).items()} if isinstance(d.get("forms"), dict) else {}
        self.extraction = ({k: _num(v) or 0.0 for k, v in d["extraction"].items()}
                           if isinstance(d.get("extraction"), dict) else {})
        dose = d.get("dose_mg") if isinstance(d.get("dose_mg"), dict) else {}
        self.dose = (_num(dose.get("min")), _num(dose.get("max"))) if dose.get("min") is not None else None
        self.due = bool(d.get("due_for_reorder")) if due is None else due
        # "just bought it" needs a known rhythm: no cadence (one visit day) -> their last buy is simply
        # their usual, and it keeps its place (the known-taste-leads guarantee, test_no_leak)
        self.knows_rhythm = bool(_num(d.get("cadence_days")))
        pr = d.get("pairings") if isinstance(d.get("pairings"), dict) else {}
        self.accepted = {k for k in pr.get("accepted") or [] if isinstance(k, str)}
        self.declined = {k for k in pr.get("declined") or [] if isinstance(k, str)}
        self.last_skus = frozenset(s for s in last_skus if s)   # their most recent visit's products

    # strong preferences (audit + why wording)
    def strong_ratio(self) -> bool:
        return bool(self.ratio_pref) and self.cbd_lean >= STRONG_RATIO_LEAN

    def strong_form(self) -> str | None:
        top = max(self.forms.items(), key=lambda kv: (kv[1], kv[0]), default=None)
        return top[0] if top and top[1] >= STRONG_FORM else None

    def strong_extraction(self) -> str | None:
        top = max(self.extraction.items(), key=lambda kv: (kv[1], kv[0]), default=None)
        return top[0] if top and top[1] >= STRONG_EXTRACTION else None

    def match(self, p, info=None, lab=None, price: float | None = None) -> dict:
        """How a candidate product fits: each part 0..1 (None = not applicable)."""
        cat = p.category or ""
        m = {"ratio": None, "form": None, "extraction": None, "dose": None, "price": None, "thc": None,
             "ratio_value": None, "form_value": None, "method": None, "mg": None, "last": p.sku in self.last_skus,
             "price_off": 0.0, "thc_off": 0.0}
        if cat in product_attrs.RATIO_CATEGORIES:
            r = product_attrs.cannabinoid_ratio(p, info, lab)
            m["ratio_value"] = r
            if r in self.ratio_pref:
                m["ratio"] = RATIO_RANK_WEIGHT[self.ratio_pref.index(r)]
            elif self.ratio_pref or self.cbd_lean:
                m["ratio"] = 0.0
            f = product_attrs.edible_form(p, info)
            m["form_value"] = f
            if self.forms:
                m["form"] = self.forms.get(f, 0.0)
            mg = product_attrs.piece_mg(p)
            m["mg"] = mg
            if self.dose and mg is not None and self.dose[0] is not None and self.dose[1] is not None:
                m["dose"] = 1.0 if self.dose[0] <= mg <= self.dose[1] else 0.0
            m["cbd_dominant"] = product_attrs.cbd_dominant(p, lab, r)
        if self.extraction:
            kinds = product_attrs.extraction_methods(p, info)
            if kinds:
                best = max(kinds, key=lambda k: (self.extraction.get(k, 0.0), k))
                m["extraction"] = self.extraction.get(best, 0.0)
                m["method"] = best if m["extraction"] else None
        pb = _band_of(self.d, "price_by_cat", cat)
        if pb and price is not None:
            m["price"] = 1.0 if pb[0] <= price <= pb[1] else 0.0
            m["price_off"] = _off(price, pb)
        tb = _band_of(self.d, "thc_by_cat", cat) if cat not in product_attrs.RATIO_CATEGORIES else None
        thc = _num(lab_enrich.effective_thc(p.thc_percent, lab))
        if tb and thc is not None:
            m["thc"] = 1.0 if tb[0] <= thc <= tb[1] else 0.0
            m["thc_off"] = _off(thc, tb)
        return m

    def boost(self, m: dict) -> float:
        if self.just_bought(m):
            return -LAST_BUY_PENALTY      # they just bought it: show them something else first
        b = REORDER_BOOST if m["last"] and self.due else 0.0
        if m["ratio"]:
            b += RATIO_BOOST * m["ratio"]
        elif m["ratio"] == 0.0 and self.strong_ratio() and not m.get("cbd_dominant"):
            b -= NO_RATIO_PENALTY
        b += FORM_BOOST * (m["form"] or 0.0)
        b += EXTRACTION_BOOST * (m["extraction"] or 0.0)
        b += DOSE_BOOST * (m["dose"] or 0.0)
        b += PRICE_BOOST * (m["price"] or 0.0)
        if m["price_off"]:
            b -= PRICE_OUT_STEP + PRICE_OUT_PENALTY * min(1.0, m["price_off"])
        b += THC_BOOST * (m["thc"] or 0.0)
        if m["thc_off"]:
            b -= THC_OUT_STEP + THC_OUT_PENALTY * min(1.0, 2 * m["thc_off"])
        return b

    def just_bought(self, m: dict) -> bool:
        return bool(m["last"]) and self.knows_rhythm and not self.due

    def misses_ratio(self, m: dict) -> bool:
        """A ratio-lean shopper looking at a THC-only edible-type product."""
        return self.strong_ratio() and m["ratio"] == 0.0 and not m.get("cbd_dominant")

    def pair_attr(self, m: dict) -> float:
        """0..1: how well an add-on matches what they buy (ratio / form / extraction / per-piece mg). A
        ratio-lean shopper's add-on must carry a ratio they buy (or lead with CBD), or it fits not at all."""
        if self.misses_ratio(m):
            return 0.0
        parts = [v for v in (m["ratio"], m["form"], m["extraction"], m["dose"]) if v is not None]
        return sum(parts) / len(parts) if parts else 0.0

    def why_bit(self, p, m: dict) -> str | None:
        """ONE short, experiential reason from the strongest match, never a claim about effects, never the
        product's own words again (the card shows the name)."""
        if self.misses_ratio(m) or self.just_bought(m):
            return None
        low = (p.name or "").lower()

        def fresh(word) -> bool:
            return bool(word) and str(word).lower() not in low

        if m["ratio"] and m["ratio"] >= 0.75:
            r = m["ratio_value"]
            return f"your usual {r} ratio" if fresh(r) else "the ratio you usually pick"
        if m["extraction"] and m["extraction"] >= 0.4 and m["method"]:
            label = product_attrs.METHOD_LABELS.get(m["method"], m["method"]).lower()
            return f"{label}, like you usually pick" if fresh(label) else "the extraction you usually pick"
        if m["dose"] and m["mg"] is not None:
            mg = f"{_r(m['mg'])}mg"
            return f"your usual {mg} pieces" if fresh(mg) else "the strength you usually pick"
        if m["form"] and m["form"] >= 0.5 and m["form_value"]:
            plural = product_attrs.FORM_PLURALS.get(m["form_value"], m["form_value"])
            return f"your usual {plural}" if fresh(plural) and fresh(m["form_value"]) else "the format you usually pick"
        if m["price"] and m["thc"]:
            return "right in your usual price range"
        return None


def _off(x: float, band: tuple[float, float]) -> float:
    """Relative distance past the nearer band edge (0 inside)."""
    lo, hi = band
    if x < lo:
        return (lo - x) / lo if lo > 0 else 0.0
    if x > hi:
        return (x - hi) / hi if hi > 0 else 0.0
    return 0.0


def last_visit_skus(profile) -> set[str]:
    """The products of their most recent purchase day (their 'very last purchase')."""
    dated = [(d, str(h.get("sku") or "")) for h in _history(profile) if (d := _dt(h.get("last_bought_at")))]
    if not dated:
        return set()
    day = max(d for d, _ in dated).date()
    return {s for d, s in dated if d.date() == day and s}


def tailor_for(profile) -> Tailor | None:
    """Tailoring for this shopper, or None (anonymous, no history, low confidence): then the ranker and
    the pairing run exactly as they did before tailoring existed."""
    if profile is None or isinstance(profile, dict) or not _history(profile):
        return None
    d = derived_for(profile)
    if not d or d.get("confidence") not in ("med", "high"):
        return None
    return Tailor(d, last_visit_skus(profile), due=due_now(profile, d))


def due_for_reorder(cadence, since) -> bool:
    c, s = _num(cadence), _num(since)
    return bool(c and s is not None and s >= max(1, round(REORDER_AT * c)))


def due_now(profile, d: dict) -> bool:
    """Due for a reorder as of NOW (a stored `derived` is from the last sync; its days_since_last ages)."""
    lasts = [x for h in _history(profile) if (x := _dt(h.get("last_bought_at")))]
    if not lasts:
        return bool(d.get("due_for_reorder"))
    since = max(0, int((datetime.now(timezone.utc) - max(lasts)).total_seconds() // 86400))
    return due_for_reorder(d.get("cadence_days"), since)
