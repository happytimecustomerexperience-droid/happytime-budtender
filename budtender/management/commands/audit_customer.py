"""Verify one real customer's profile against their transaction history, then check the suggestions the
website would show them land in their ranges. Staff CLI (run on the VPS, against the real DB): READ-ONLY,
writes nothing (not the profile, not memory, not SuggestedProduct), calls no Dutchie API.

    python manage.py audit_customer --phone 5095551234 [--json] [--store yakima] [--limit 5]

1. RAW history stats (purchase_history joined to the catalogue): orders, categories, price / THC / per-piece
   mg / ratio / format / extraction distributions, the last purchases.
2. `derived` (budtender.customer_model.compute_derived) next to it, plus the stored memory["derived"] when
   memory.py has written one, and FLAGS where the stored profile disagrees with the history.
3. For each category in next_likely + the top affinities: ranking.rank_products (the website's code path)
   and PASS / FAIL / WARN / SKIP checks: picks in stock; #1 is not their very last purchase unless they are
   due; picks inside their price and THC bands (p10..p90 widened 25%, else their price tier widened 25%);
   a strong ratio / format / extraction preference is matched by #1. A band or preference check only
   FAILs when the shelf HAS a fitting alternative (not counting what they just bought); else WARN.
Exit status 1 on any FAIL. `--json` prints one stable (sorted-key) JSON document.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError

from budtender import customer_model, identity, lab_enrich, live_stock, product_attrs, ranking
from budtender.customer_model import BAND_WIDEN
from budtender.models import STORES, CustomerProfile
from budtender.tasks import _normalize_phone

CHECK_TOP = 3


def _lookup(phone: str) -> tuple[CustomerProfile, list[str]]:
    """The profile behind this number, the way identity.profile_for_phone finds it, but a staff audit
    also opens a row the website would refuse (shared row, non-identifying number), with a flag."""
    e164 = _normalize_phone(phone or "")
    if not e164:
        raise CommandError("not a usable US phone number")
    flags = []
    prof = identity.profile_for_phone(e164)
    if prof is None:
        prof = identity.follow(CustomerProfile.objects.filter(phone=e164).select_related("merged_into").first())
        if prof is None:
            raise CommandError("no customer profile for that number")
        if identity.shared_profile(prof):
            flags.append("SHARED row (>= %d Dutchie ids): the website and phone treat this number as anonymous"
                         % identity.SHARED_DUTCHIE_IDS)
        if identity.non_identifying_phone(e164):
            flags.append("non-identifying number (store line / junk pattern): the website treats it as anonymous")
    return prof, flags


def _dist(pairs) -> dict | None:
    vals = sorted(v for v, w in pairs for _ in range(w))
    if not vals:
        return None
    r = customer_model._r
    return {"n": len(vals), "min": r(vals[0]), "median": r(vals[len(vals) // 2]), "max": r(vals[-1])}


def raw_stats(rows: list[dict]) -> dict:
    cats, ratios, forms, methods, doses = Counter(), Counter(), Counter(), Counter(), Counter()
    price, thc = defaultdict(list), defaultdict(list)
    for r in rows:
        w = r["times"]
        cats[r["category"] or "(unknown)"] += w
        if r["ratio"]:
            ratios[r["ratio"]] += w
        if r["form"]:
            forms[r["form"]] += w
        for m in r["methods"]:
            methods[m] += w
        if r["piece_mg"]:
            doses[f"{customer_model._r(r['piece_mg'])}mg"] += w
        if r["price"]:
            price[r["category"]].append((r["price"], w))
        if r["thc"] is not None:
            thc[r["category"]].append((r["thc"], w))
    dated = sorted((r for r in rows if r["last"]), key=lambda r: (r["last"], r["sku"]), reverse=True)
    firsts = [r["first"] for r in rows if r["first"]]
    return {
        "lines": len(rows), "units": sum(r["times"] for r in rows),
        "orders_estimate": customer_model.orders_estimate(rows),
        "first_purchase": min(firsts).date().isoformat() if firsts else None,
        "last_purchase": dated[0]["last"].date().isoformat() if dated else None,
        "not_in_catalogue": sum(1 for r in rows if not r["in_catalogue"]),
        "categories": dict(cats.most_common()),
        "price_by_cat": {c: _dist(v) for c, v in sorted(price.items())},
        "thc_by_cat": {c: _dist(v) for c, v in sorted(thc.items())},
        "piece_mg": dict(doses.most_common()), "ratios": dict(ratios.most_common()),
        "forms": dict(forms.most_common()), "extraction": dict(methods.most_common()),
        "last_purchases": [{"date": r["last"].date().isoformat(), "sku": r["sku"], "name": r["name"],
                            "category": r["category"], "price": r["price"], "times": r["times"]} for r in dated[:10]],
    }


def profile_flags(prof: CustomerProfile, raw: dict, derived: dict) -> list[str]:
    flags = []
    aff = prof.category_affinity or {}
    raw_cats = {c: n for c, n in raw["categories"].items() if c != "(unknown)"}
    if aff and raw_cats:
        top_aff = max(aff.items(), key=lambda kv: (kv[1], kv[0]))[0]
        top_raw = max(raw_cats.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if customer_model.canon(top_aff) != customer_model.canon(top_raw):
            flags.append(f"category_affinity leads with {top_aff!r} but the history's most-bought is {top_raw!r}")
    elif raw_cats and not aff:
        flags.append("history present but category_affinity is empty (recompute_affinity never ran?)")
    if prof.total_orders != raw["units"]:
        flags.append(f"total_orders={prof.total_orders} but the history holds {raw['units']} units")
    inhal = [v for c, d in raw["thc_by_cat"].items() if c not in product_attrs.RATIO_CATEGORIES and d
             for v in (d["min"], d["max"])]
    if inhal and prof.thc_min is not None and prof.thc_max is not None:
        if abs(min(inhal) - prof.thc_min) > 0.5 or abs(max(inhal) - prof.thc_max) > 0.5:
            flags.append(f"thc band {prof.thc_min}-{prof.thc_max} vs history {min(inhal)}-{max(inhal)} "
                         "(the stored band mixes edible package numbers or is stale)")
    if raw["not_in_catalogue"]:
        flags.append(f"{raw['not_in_catalogue']} history line(s) no longer match a catalogue product "
                     "(derived uses the line's own name/category)")
    mem = prof.memory if isinstance(getattr(prof, "memory", None), dict) else {}
    stored = mem.get("derived")
    if isinstance(stored, dict):
        volatile = {"days_since_last", "due_for_reorder"}
        try:   # memory.py stores a sanitized copy (empty maps dropped): compare like with like
            from budtender.memory import sanitize_derived
            fresh, stored = sanitize_derived(derived), sanitize_derived(stored)
        except Exception:  # noqa: BLE001 - memory.py ships separately
            fresh = {k: v for k, v in derived.items() if v not in ({}, [], None)}
            stored = {k: v for k, v in stored.items() if v not in ({}, [], None)}
        diff = sorted(k for k in set(fresh) | set(stored) if k not in volatile and stored.get(k) != fresh.get(k))
        if diff:
            flags.append("stored memory['derived'] is stale on: " + ", ".join(diff))
    if derived["confidence"] == "low":
        flags.append("confidence low: the website ranks this customer exactly as before tailoring "
                     "(band/preference checks are SKIPped)")
    return flags


def _band(derived: dict, key: str, cat: str):
    b = (derived.get(key) or {}).get(cat)
    if b:
        return b["p10"] * (1 - BAND_WIDEN), b["p90"] * (1 + BAND_WIDEN), "p10-p90 +25%"
    return None


def _tier_band(prof):
    if prof.price_tier in ("value", "mid", "top"):
        lo, hi = ranking.price_tier_bounds(prof.price_tier)
        return lo * (1 - BAND_WIDEN), (hi * (1 + BAND_WIDEN) if hi < 1e8 else 1e9), f"tier {prof.price_tier} +25%"
    return None


def _fmt(x):
    return "inf" if x >= 1e8 else f"{x:g}"


def check_category(store: str, cat: str, prof, derived: dict, tailor, limit: int) -> tuple[list, list]:
    live = live_stock.stock_map(store)
    labs, details = lab_enrich.Memo(), lab_enrich.Memo()
    picks = ranking.rank_products(store, {"category": cat}, prof, limit=limit, labs=labs)
    elig = ranking.eligible(store, {"category": cat}, labs=labs, details=details, live=live)
    lab_enrich.labs_for([p.batch_id for p in elig], memo=labs)
    lab_enrich.details_for([p.product_id for p in elig], memo=details)

    def fit(p):
        return tailor.match(p, details.get(p.product_id) or None, labs.get(p.batch_id) or None,
                            price=ranking._live_price(live, p))

    def thc_of(p):
        return lab_enrich.effective_thc(p.thc_percent, labs.get(p.batch_id) or None)

    last = tailor.last_skus
    fresh_alts = [p for p in elig if p.sku not in last]
    out = []

    def add(check, status, detail):
        out.append({"category": cat, "check": check, "status": status, "detail": detail})

    shown = [{"rank": i + 1, "sku": p.sku, "name": p.name, "price": ranking._live_price(live, p),
              "thc": thc_of(p), "why": why} for i, (p, why) in enumerate(picks)]
    if not picks:
        add("picks", "WARN", "no in-stock picks in this category")
        return out, shown

    bad = [p.sku for p, _ in picks if not (p.availability and (
        live.buyable(sku=p.sku, product_id=p.product_id, min_stock=ranking.MIN_STOCK) if live.usable
        else p.quantity_on_hand >= ranking.MIN_STOCK))]
    add("in_stock", "FAIL" if bad else "PASS", f"not on the floor: {bad}" if bad else f"{len(picks)} picks in stock")

    top = picks[0][0]
    pband = _band(derived, "price_by_cat", cat) or _tier_band(prof)
    if top.sku in last and not derived["due_for_reorder"] and derived["cadence_days"]:
        # an alternative only counts when it is in their price range (a $20 5-pack is no stand-in for the
        # $8 single a value shopper just bought)
        alts = [p for p in fresh_alts if not pband or pband[0] <= ranking._live_price(live, p) <= pband[1]]
        why_not = (f"#1 {top.sku} is their last purchase and they are not due "
                   f"(cadence {derived['cadence_days']}d, {derived['days_since_last']}d since)")
        add("not_last_purchase", "FAIL" if alts else "WARN",
            why_not + ("" if alts else "; no other in-range product on this shelf"))
    else:
        add("not_last_purchase", "PASS", "due for a reorder" if top.sku in last else "#1 is not their last purchase")

    low = derived["confidence"] == "low"

    def band_check(name, band, value_of):
        if band is None:
            add(name, "SKIP", "no band for this category")
            return
        lo, hi, how = band
        if low:
            add(name, "SKIP", f"low confidence (band {_fmt(lo)}-{_fmt(hi)}, {how})")
            return
        inside = [p for p in fresh_alts if (v := value_of(p)) is not None and lo <= float(v) <= hi]
        n = min(CHECK_TOP, len(picks), len(inside))
        if not n:
            add(name, "WARN", f"nothing in stock inside {_fmt(lo)}-{_fmt(hi)} ({how})")
            return
        def outside(p):
            v = value_of(p)
            return v is None or not lo <= float(v) <= hi
        out_of = [f"#{i + 1} {p.sku}={value_of(p)}" for i, (p, _) in enumerate(picks[:n]) if outside(p)]
        # #1 out of range, or two of the top three, is a FAIL; one variety slot outside is a WARN
        status = "FAIL" if (out_of and outside(picks[0][0])) or len(out_of) >= 2 else ("WARN" if out_of else "PASS")
        add(name, status, f"top {n} vs {_fmt(lo)}-{_fmt(hi)} ({how})" + (f": outside {out_of}" if out_of else ""))

    band_check("price_band", pband, lambda p: ranking._live_price(live, p))
    band_check("thc_band", None if cat in product_attrs.RATIO_CATEGORIES else _band(derived, "thc_by_cat", cat), thc_of)

    def pref_check(name, wanted, has):
        if wanted is None:
            return
        if low:
            add(name, "SKIP", f"low confidence (prefers {wanted})")
            return
        alts = [p for p in fresh_alts if has(fit(p))]
        if not alts:
            add(name, "WARN", f"prefers {wanted}; no other in-stock match on this shelf")
            return
        ok = has(fit(top))
        add(name, "PASS" if ok else "FAIL", f"prefers {wanted}; #1 {top.sku} " + ("matches" if ok else
            f"does not (in stock: {[p.sku for p in alts[:5]]})"))

    if cat in product_attrs.RATIO_CATEGORIES:
        pref_check("ratio", tailor.ratio_pref if tailor.strong_ratio() else None,
                   lambda m: bool(m["ratio"]) or bool(m.get("cbd_dominant")))
        if cat in ("edibles", "solid-edibles", "liquid-edibles"):   # a tincture/topical shelf IS its format
            pref_check("form", tailor.strong_form(), lambda m: m["form_value"] == tailor.strong_form())
    ext = tailor.strong_extraction()
    if ext and cat in product_attrs.EXTRACTION_CATEGORIES | product_attrs.FORM_CATEGORIES:
        pref_check("extraction", ext, lambda m: (m["extraction"] or 0) >= customer_model.STRONG_EXTRACTION)
    return out, shown


def audit(phone: str, store: str = "yakima", limit: int = 5) -> dict:
    prof, flags = _lookup(phone)
    rows = customer_model.history_rows(prof)
    derived = customer_model.compute_derived(prof, rows=rows)
    raw = raw_stats(rows)
    flags += profile_flags(prof, raw, derived)
    tailor = customer_model.Tailor(derived, customer_model.last_visit_skus(prof))
    top_aff = [c for c, _ in sorted((prof.category_affinity or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
    cats = list(dict.fromkeys([*derived["next_likely"], *top_aff]))[:5]
    checks, picks = [], {}
    for cat in cats:
        c, shown = check_category(store, cat, prof, derived, tailor, limit)
        checks += c
        picks[cat] = shown
    status = Counter(c["status"] for c in checks)
    return {
        "profile": {"id": prof.id, "phone_last4": prof.phone[-4:], "source": prof.source,
                    "total_orders": prof.total_orders, "price_tier": prof.price_tier,
                    "thc_band": [prof.thc_min, prof.thc_max],
                    "top_categories": {c: (prof.category_affinity or {})[c] for c in top_aff}},
        "store": store, "raw": raw, "derived": derived, "flags": flags, "picks": picks, "checks": checks,
        "summary": {s: status.get(s, 0) for s in ("PASS", "FAIL", "WARN", "SKIP")},
        "result": "FAIL" if status.get("FAIL") else "PASS",
    }


class Command(BaseCommand):
    help = "Audit one customer's derived profile against their history and check their suggestions (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--phone", required=True)
        parser.add_argument("--store", default="yakima", choices=[s for s, _ in STORES])
        parser.add_argument("--limit", type=int, default=5)
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, phone, store, limit, as_json, **opts):
        rep = audit(phone, store=store, limit=max(1, min(limit, ranking.SEARCH_CAP)))
        if as_json:
            self.stdout.write(json.dumps(rep, sort_keys=True, indent=2, default=str))
        else:
            self._text(rep)
        if rep["result"] == "FAIL":
            raise SystemExit(1)

    def _text(self, rep):
        w = self.stdout.write
        p, raw, d = rep["profile"], rep["raw"], rep["derived"]
        w(f"Customer #{p['id']} (…{p['phone_last4']}, {p['source']})  store={rep['store']}  "
          f"tier={p['price_tier'] or '-'}  total_orders={p['total_orders']}")
        w(f"RAW  orders~{raw['orders_estimate']}  lines={raw['lines']}  units={raw['units']}  "
          f"{raw['first_purchase']} .. {raw['last_purchase']}  not-in-catalogue={raw['not_in_catalogue']}")
        w(f"     categories {raw['categories']}")
        for c, dist in raw["price_by_cat"].items():
            w(f"     price {c}: {dist}   derived {d['price_by_cat'].get(c)}")
        for c, dist in raw["thc_by_cat"].items():
            w(f"     thc   {c}: {dist}   derived {d['thc_by_cat'].get(c)}")
        w(f"     ratios {raw['ratios']}  forms {raw['forms']}  extraction {raw['extraction']}  mg {raw['piece_mg']}")
        for lp in raw["last_purchases"][:5]:
            w(f"     last {lp['date']} {lp['sku']} {lp['name']} ({lp['category']}, ${lp['price']}, x{lp['times']})")
        w("DERIVED " + json.dumps(d, sort_keys=True))
        for f in rep["flags"]:
            w(f"FLAG {f}")
        if not rep["picks"]:
            w("-- no categories to check (no purchase history): suggestions are the store's default ranking")
        for cat, shown in rep["picks"].items():
            w(f"-- {cat}")
            for s in shown:
                w(f"   #{s['rank']} {s['sku']} {s['name']}  ${s['price']:g}  thc={s['thc']}  “{s['why']}”")
            for c in (c for c in rep["checks"] if c["category"] == cat):
                w(f"   {c['status']:<4} {c['check']}: {c['detail']}")
        s = rep["summary"]
        w(f"RESULT {rep['result']}  pass={s['PASS']} fail={s['FAIL']} warn={s['WARN']} skip={s['SKIP']}")
