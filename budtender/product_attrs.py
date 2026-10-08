"""What we KNOW about a product, read from its name, its stored product info (ProductDetail tags /
e-comm subcategory) and its stored batch lab (BatchLab). Search v2 (docs/contracts/search-v2.md) filters,
counts and ranks on these; the ONE place each derivation lives, so a facet count and the search it leads
to can never disagree.

Pure: no Django queries, no I/O. Every function takes the Product row plus the stored `info` / `lab`
dicts (None when nothing is on file) and never guesses: no data -> no match.
"""
from __future__ import annotations

import re

from . import terpenes as _terpenes

# ── Dutchie MASTER categories <-> catalog slugs ──────────────────────────────
# Product.category is the catalog slug dutchie._norm_category produced; it COLLAPSES some masters
# (Infused Pre-roll -> pre-rolls, Disposable Vape -> vape-cartridges, Solid/Liquid Edible -> edibles).
# The split is recovered from the name / product info (master_of). Each master gets its own `value`;
# values that equal a catalog slug cover that WHOLE slug, so legacy callers sending "flower",
# "concentrates", "topicals"... see exactly what they always did.
MASTER_ORDER = ("Flower", "Pre-roll", "Infused Pre-roll", "Concentrate", "Vape Cartridge", "Disposable Vape",
                "Solid Edible", "Liquid Edible", "Topical")
EXTRA_MASTERS = ("Tincture", "Capsule")   # listed after the nine only while the sync still yields them
# master -> (value used in slots.category, display label, catalog slugs it draws from)
MASTERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "Flower": ("flower", "Flower", ("flower",)),
    "Pre-roll": ("regular-pre-rolls", "Pre-Rolls", ("pre-rolls", "blunt")),
    "Infused Pre-roll": ("infused-pre-rolls", "Infused Pre-Rolls", ("pre-rolls", "infused-blunt")),
    "Concentrate": ("concentrates", "Concentrates", ("concentrates",)),
    "Vape Cartridge": ("vape-carts", "Vape Cartridges", ("vape-cartridges",)),
    "Disposable Vape": ("disposable-vapes", "Disposable Vapes", ("vape-cartridges",)),
    "Solid Edible": ("solid-edibles", "Edibles", ("edibles", "mints")),
    "Liquid Edible": ("liquid-edibles", "Drinks & Liquid Edibles", ("edibles", "beverages")),
    "Topical": ("topicals", "Topicals", ("topicals",)),
    "Tincture": ("tinctures", "Tinctures", ("tinctures",)),
    "Capsule": ("capsules", "Capsules", ("capsules",)),
}
# value -> master, for the values that are NOT a plain catalog slug (those need the master predicate)
MASTER_BY_VALUE = {v[0]: m for m, v in MASTERS.items()}
_MASTER_BY_NAME = {m.lower(): m for m in MASTERS}


def resolve_master(value) -> str | None:
    """'vape-carts' / 'Vape Cartridge' / 'vape cartridge' -> 'Vape Cartridge'; None when the value is not a
    master value (legacy catalog slugs and slot keys are resolved by ranking.CATEGORY_BY_SLOTKEY)."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return MASTER_BY_VALUE.get(v) or _MASTER_BY_NAME.get(v.lower())


# ── haystacks ────────────────────────────────────────────────────────────────
def _norm(s) -> str:
    return re.sub(r"[\s_\-]+", " ", str(s or "").lower()).strip()


def info_tags(info) -> list[str]:
    tags = (info or {}).get("tags") if isinstance(info, dict) else None
    return [t for t in tags if isinstance(t, str) and t.strip()] if isinstance(tags, list) else []


def hay(p, info=None) -> str:
    """Name (with the strain name cut out, so 'Hash Plant' or 'Diamond OG' never reads as an extraction)
    + the product's tags, lowercased with hyphens as spaces."""
    name = _norm(p.name)
    strain = _norm(getattr(p, "strain", ""))
    if len(strain) >= 4 and strain in name:
        name = name.replace(strain, " ")
    return " ".join([name, *(_norm(t) for t in info_tags(info))])


def _ecom(info) -> str:
    if not isinstance(info, dict):
        return ""
    return _norm(f"{info.get('ecom_category') or ''} {info.get('ecom_subcategory') or ''}")


# ── extraction / form kinds (concentrates + vapes) ───────────────────────────
# (value, label, pattern, solventless). Multi-label: "Live Hash Rosin Badder" is live-rosin, hash-rosin,
# rosin and badder. The broad legacy values keep their legacy meaning: rosin covers every rosin, and the
# legacy subtype "live-resin" still folds cured resin in (ranking.kinds_of adds the legacy subtype).
EXTRACTIONS: tuple[tuple[str, str, re.Pattern, bool], ...] = tuple(
    (v, label, re.compile(pat), sl) for v, label, pat, sl in (
        ("live-rosin", "Live Rosin", r"\blive (?:hash )?rosin\b", True),
        ("hash-rosin", "Hash Rosin", r"\b(?:hash|ice ?water(?: hash)?|bubble(?: hash)?) rosin\b", True),
        ("rosin", "Rosin", r"\brosin\b", True),
        ("live-resin", "Live Resin", r"\blive resin\b", False),
        ("cured-resin", "Cured Resin", r"\bcured resin\b", False),
        ("hash", "Hash", r"\b(?:bubble hash|ice ?water hash|dry ?sift|temple balls?|hash)\b(?! (?:rosin|hole|oil))",
         True),
        ("kief", "Kief", r"\bkief\b", True),
        ("distillate", "Distillate", r"\b(?:distillate|disty)\b", False),
        ("diamonds", "Diamonds", r"\bdiamonds?\b", False),
        ("sauce", "Sauce", r"\bsauce\b", False),
        ("badder", "Badder / Budder", r"\b(?:badder|batter|budder)\b", False),
        ("shatter", "Shatter", r"\bshatter\b", False),
        ("wax", "Wax", r"\bwax\b", False),
        ("crumble", "Crumble", r"\bcrumble\b", False),
        ("sugar", "Sugar", r"\bsugar\b", False),
        ("rso", "RSO", r"\b(?:rso|rick simpson|feco)\b", False),
    ))
EXTRACTION_LABELS = {v: label for v, label, _, _ in EXTRACTIONS}
_SOLVENTLESS_KINDS = {v for v, _, _, sl in EXTRACTIONS if sl}
_SOLVENTLESS_WORDS = re.compile(r"\b(?:solventless|trichomes?|ice ?water|dry ?sift|bubble hash)\b")
EXTRACTION_CATEGORIES = frozenset({"concentrates", "vape-cartridges"})


def extraction_kinds(p, info=None) -> set[str]:
    if (p.category or "") not in EXTRACTION_CATEGORIES:
        return set()
    h = hay(p, info)
    return {v for v, _, rx, _ in EXTRACTIONS if rx.search(h)}


# ── infusion (infused pre-rolls / blunts, edibles) ───────────────────────────
INFUSIONS: tuple[tuple[str, str, re.Pattern], ...] = tuple(
    (v, label, re.compile(pat)) for v, label, pat in (
        ("diamond", "Diamonds", r"\b(?:diamonds?|thca)\b"),
        ("live-rosin", "Live Rosin", r"\blive (?:hash )?rosin\b"),
        ("rosin", "Rosin", r"\brosin\b"),
        ("live-resin", "Live Resin", r"\blive resin\b"),
        ("hash", "Hash", r"\b(?:hash|bubble hash|hash hole)\b(?! rosin)"),
        ("kief", "Kief", r"\bkief\b"),
        ("moonrock", "Moon Rock", r"\bmoon ?rocks?\b"),
        ("distillate", "Distillate", r"\b(?:distillate|disty)\b"),
    ))
INFUSION_LABELS = {v: label for v, label, _ in INFUSIONS}
_INFUSED_WORDS = re.compile(r"\b(?:infused|diamonds?|hash hole|moon ?rocks?|thca)\b")
_EDIBLE_CATEGORIES = frozenset({"edibles", "beverages", "mints", "capsules", "tinctures"})


def is_infused_preroll(p, info=None) -> bool:
    cat = p.category or ""
    if cat == "infused-blunt":
        return True
    if cat not in ("pre-rolls", "blunt"):
        return False
    return bool(_INFUSED_WORDS.search(hay(p, info))) or "infused" in _ecom(info)


def infusion_kinds(p, info=None) -> set[str]:
    cat = p.category or ""
    if cat in ("pre-rolls", "blunt", "infused-blunt"):
        if not is_infused_preroll(p, info):
            return set()
    elif cat not in _EDIBLE_CATEGORIES:
        return set()
    h = hay(p, info)
    return {v for v, _, rx in INFUSIONS if rx.search(h)}


def is_solventless(p, info=None) -> bool:
    """Ice-water hash / rosin / kief / 'trichome' extractions — for concentrates, vapes, infused pre-rolls and
    edibles (a live-rosin gummy is solventless; a flower is not an extraction at all)."""
    kinds = extraction_kinds(p, info)
    if kinds & _SOLVENTLESS_KINDS:
        return True
    inf = infusion_kinds(p, info)
    if inf & {"rosin", "live-rosin", "hash", "kief"}:
        return True
    if kinds or inf or (p.category or "") in EXTRACTION_CATEGORIES:
        return bool(_SOLVENTLESS_WORDS.search(hay(p, info)))
    return False


# ── master category of a product ─────────────────────────────────────────────
_DISPOSABLE = re.compile(r"\b(?:disposables?|dispo|all in one|aio|ready to use|rechargeable)\b")
_LIQUID = re.compile(r"\b(?:drinks?|beverages?|soda|seltzer|shots?|syrup|elixir|lemonade|tonic|juice|tea|coffee"
                     r"|cold brew|sparkling|fl ?oz|ml)\b")


def is_disposable(p, info=None) -> bool:
    return bool(_DISPOSABLE.search(hay(p, info))) or "disposable" in _ecom(info)


def is_liquid_edible(p, info=None, legacy_subtype: str = "") -> bool:
    if (p.category or "") == "beverages":
        return True
    e = _ecom(info)
    return legacy_subtype == "drinks" or bool(_LIQUID.search(hay(p, info))) or "beverage" in e or "drink" in e


def master_of(p, info=None, legacy_subtype: str = "") -> str | None:
    cat = p.category or ""
    if cat == "flower":
        return "Flower"
    if cat in ("pre-rolls", "blunt", "infused-blunt"):
        return "Infused Pre-roll" if is_infused_preroll(p, info) else "Pre-roll"
    if cat == "concentrates":
        return "Concentrate"
    if cat == "vape-cartridges":
        return "Disposable Vape" if is_disposable(p, info) else "Vape Cartridge"
    if cat in ("edibles", "beverages", "mints"):
        return "Liquid Edible" if is_liquid_edible(p, info, legacy_subtype) else "Solid Edible"
    if cat == "topicals":
        return "Topical"
    if cat == "tinctures":
        return "Tincture"
    if cat == "capsules":
        return "Capsule"
    return None


# ── lab-derived numbers ──────────────────────────────────────────────────────
CANNABINOID_SLOTS = {"cbd_min": "cbd", "cbg_min": "cbg", "cbn_min": "cbn", "cbc_min": "cbc", "thcv_min": "thcv"}


def cannabinoid_pct(lab, name: str) -> float:
    """% of one cannabinoid from the stored lab: CBD from `cbd_total`, minors (CBG/CBN/CBC/THCV) from
    `minor_cannabinoids` (the lab's three strongest minors are stored, so a weaker one reads as 0)."""
    if not isinstance(lab, dict):
        return 0.0
    if name == "cbd":
        v = lab.get("cbd_total")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    for row in lab.get("minor_cannabinoids") or []:
        if isinstance(row, dict) and str(row.get("name") or "").lower() == name:
            v = row.get("pct")
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    return 0.0


def lab_terpenes(lab) -> dict[str, float]:
    """{canonical terpene: pct} from the stored lab's terpenes (pct > 0 only)."""
    out: dict[str, float] = {}
    rows = lab.get("terpenes") if isinstance(lab, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("pct"), (int, float)) and row["pct"] > 0:
            name = _terpenes.canonical(row.get("name"))
            if name:
                out[name] = max(out.get(name, 0.0), float(row["pct"]))
    return out


def total_terpenes(lab) -> float:
    v = (lab or {}).get("total_terpenes") if isinstance(lab, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0.0


def terpene_label(name: str) -> str:
    known = _terpenes.NOTES.get(name)
    return known["label"] if known else name.replace("-", " ").title()


def has_lab(lab) -> bool:
    return isinstance(lab, dict) and bool(lab)


# ── free text / tags ─────────────────────────────────────────────────────────
def norm_text(s) -> str:
    return _norm(s)


def tag_match(p, info, wanted: list[str]) -> bool:
    """ANY wanted tag is one of the product's tags, or a whole phrase of its name ('live resin')."""
    tags = {_norm(t) for t in info_tags(info)}
    name = f" {_norm(p.name)} "
    return any(w in tags or f" {w} " in name for w in wanted)


_SIZE_TAG = re.compile(r"^\d+(?:\.\d+)?\s*(?:g|mg|pk|ct|oz|ml|pack)$")


def is_size_tag(tag: str) -> bool:
    return bool(_SIZE_TAG.match(_norm(tag)))


# ── customer tailoring derivations (budtender.customer_model) ────────────────
# Ratio / form / extraction method / per-piece strength of ONE product, read from its name, tags and
# stored lab. Used to describe what a customer buys (customer_model.compute_derived) and to match a
# candidate against it (the ranker's soft tailoring). Same rule as the rest of this module: no data, no
# match, never a guess.
RATIO_CATEGORIES = frozenset({"edibles", "beverages", "mints", "capsules", "tinctures", "topicals"})
FORM_CATEGORIES = RATIO_CATEGORIES
_RATIO_RE = re.compile(r"(?<![\d.:])(\d{1,3}(?:\.\d)?)\s*:\s*(\d{1,3}(?:\.\d)?)(?:\s*:\s*(\d{1,3}(?:\.\d)?))?(?![\d.:])")
_RATIO_ORDER = re.compile(r"\b(thc|cbd|cbg|cbn)\s*[:/]\s*(thc|cbd|cbg|cbn)\b")
_CANNABINOID_WORD = re.compile(r"\b(?:thc|cbd|cbg|cbn)\b")
_STANDARD_RATIOS = (1, 2, 3, 4, 5, 8, 10, 15, 18, 20, 25, 30)


def _fmt_num(x: float) -> str:
    return f"{int(x)}" if float(x) == int(x) else f"{x:g}"


def _lab_num(lab, key: str) -> float:
    v = lab.get(key) if isinstance(lab, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0.0


def cannabinoid_ratio(p, info=None, lab=None) -> str | None:
    """THC:CBD ratio as 'a:b' ('1:1', '2:1', '1:20'), or a printed three-part ratio ('1:1:1') as is.
    The name/tags win; the printed order is turned to THC:CBD when the label says 'CBD:THC', or when the
    stored lab (or a CBD-only label) shows the printed bigger number is the CBD. No ratio printed -> the
    lab's THC and CBD totals, snapped to a standard ratio, when CBD is a real share (<= 5:1, or CBD leads)."""
    raw = " ".join([str(getattr(p, "name", "") or "").lower(), *(t.lower() for t in info_tags(info))])
    cat = getattr(p, "category", "") or ""
    thc, cbd = _lab_num(lab, "thc_total"), _lab_num(lab, "cbd_total")
    m = _RATIO_RE.search(raw)
    if m and (cat in RATIO_CATEGORIES or _CANNABINOID_WORD.search(raw) or cbd):
        a, b, c = float(m.group(1)), float(m.group(2)), m.group(3)
        if a <= 0 or b <= 0 or max(a, b) > 100:
            return None
        if c is not None:
            return f"{_fmt_num(a)}:{_fmt_num(b)}:{_fmt_num(float(c))}"
        order = _RATIO_ORDER.search(raw)
        if order and order.group(1) == "cbd" and order.group(2) == "thc":
            flip = True
        elif order and order.group(1) == "thc" and order.group(2) == "cbd":
            flip = False
        elif thc and cbd and a != b:
            flip = (a > b) == (cbd > thc)
        else:
            cbd_label = (getattr(p, "strain_type", "") or "").lower() == "cbd" or (
                "cbd" in raw and not re.search(r"\bthc\b", raw))
            flip = a > b and cbd_label
        a, b = (b, a) if flip else (a, b)
        return f"{_fmt_num(a)}:{_fmt_num(b)}"
    if thc and cbd and min(thc, cbd) >= 0.5:
        hi, lo = max(thc, cbd), min(thc, cbd)
        r = hi / lo
        if cbd < thc and r > 5.5:
            return None   # an ordinary THC product with a trace of CBD
        k = min(_STANDARD_RATIOS, key=lambda s: abs(s - r) / s)
        if abs(k - r) / k > 0.2:
            return None
        return "1:1" if k == 1 else (f"{k}:1" if thc > cbd else f"1:{k}")
    return None


def cbd_dominant(p, lab=None, ratio: str | None = None) -> bool:
    """CBD leads: a CBD strain type, a lab with more CBD than THC, or a ratio whose CBD side is bigger."""
    if (getattr(p, "strain_type", "") or "").lower() == "cbd":
        return True
    thc, cbd = _lab_num(lab, "thc_total"), _lab_num(lab, "cbd_total")
    if cbd and cbd > thc:
        return True
    if ratio and ratio.count(":") == 1:
        a, b = (float(x) for x in ratio.split(":"))
        return b > a
    return False


_FORM_WORDS = (
    ("gummy", re.compile(r"\bgumm(?:y|ies)\b")),
    ("chocolate", re.compile(r"\b(?:chocolates?|peanut butter cups?|truffles?)\b")),
    ("capsule", re.compile(r"\b(?:capsules?|softgels?|pills?)\b")),
    ("mint", re.compile(r"\b(?:mints?|lozenges?|tablets?|troches?)\b")),
    ("candy", re.compile(r"\b(?:lollipops?|lollis?|suckers?|hard candy|candy)\b")),
    ("baked", re.compile(r"\b(?:cookies?|brownies?|rice crisp(?:y|ies)?|crispy treats?)\b")),
    ("chew", re.compile(r"\b(?:caramels?|chews?|taffy)\b")),
    ("tincture", re.compile(r"\btinctures?\b")),
)
FORM_PLURALS = {"gummy": "gummies", "chocolate": "chocolates", "capsule": "capsules", "mint": "mints",
                "candy": "candies", "baked": "baked treats", "chew": "chews", "tincture": "tinctures",
                "drink": "drinks", "topical": "topicals", "edible": "edibles"}


def edible_form(p, info=None) -> str | None:
    """gummy | chocolate | drink | tincture | capsule | topical | mint | candy | baked | chew | edible, for the
    edible-type categories; None for flower, pre-rolls, vapes and concentrates."""
    cat = getattr(p, "category", "") or ""
    if cat not in FORM_CATEGORIES:
        return None
    fixed = {"tinctures": "tincture", "topicals": "topical", "capsules": "capsule", "beverages": "drink",
             "mints": "mint"}
    if cat in fixed:
        return fixed[cat]
    h = hay(p, info)
    for form, rx in _FORM_WORDS[:2]:
        if rx.search(h):
            return form
    if is_liquid_edible(p, info):
        return "drink"
    for form, rx in _FORM_WORDS[2:]:
        if rx.search(h):
            return form
    return "edible"


EXTRACTION_METHODS = ("live-rosin", "hash-rosin", "rosin", "live-resin", "cured-resin", "distillate",
                      "full-spectrum", "rso", "hash", "kief")
METHOD_LABELS = {**EXTRACTION_LABELS, "full-spectrum": "full spectrum", "rso": "RSO"}
_FULL_SPECTRUM = re.compile(r"\b(?:full spectrum|fse|fso|whole plant)\b")


def extraction_methods(p, info=None) -> set[str]:
    """Every extraction METHOD a product answers to (textures like badder/sauce left out): the concentrate/
    vape kinds, an infused edible or pre-roll's infusion, and 'full-spectrum'. Keeps the umbrella 'rosin'
    on a live/hash rosin, so a plain-rosin buyer still matches one."""
    kinds = (extraction_kinds(p, info) | infusion_kinds(p, info)) & set(EXTRACTION_METHODS)
    if (getattr(p, "category", "") or "") in EXTRACTION_CATEGORIES | FORM_CATEGORIES and _FULL_SPECTRUM.search(hay(p, info)):
        kinds.add("full-spectrum")
    return kinds


def primary_methods(kinds: set[str]) -> set[str]:
    """The methods to COUNT for a purchase: the umbrella 'rosin' only when nothing more specific is named."""
    return kinds - {"rosin"} if kinds & {"live-rosin", "hash-rosin"} else set(kinds)


_MG_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*mg\b")
_PIECES_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*(?:pk|pack|ct|count|pcs?|pieces?)\b")


def piece_mg(p) -> float | None:
    """mg per piece/serving of an edible-type product, from the name: '10mg 10pk' -> 10, '100mg 10pk' -> 10,
    '5mg 20pk' -> 5, a can '10mg' -> 10. A package total with no pack count ('100mg bar') or a tincture
    bottle is unknown (None). No mg in the name -> the stored potency, split by the pack count."""
    cat = getattr(p, "category", "") or ""
    if cat not in FORM_CATEGORIES or cat in ("tinctures", "topicals"):
        return None
    name = str(getattr(p, "name", "") or "").lower()
    mgs = [float(x) for x in _MG_RE.findall(name)]
    pk = _PIECES_RE.search(name)
    pieces = int(pk.group(1)) if pk and 1 < int(pk.group(1)) <= 100 else None
    if len(mgs) >= 2:
        lo, hi = min(mgs), max(mgs)
        return lo if hi / lo >= 4 else mgs[0]
    if len(mgs) == 1:
        m = mgs[0]
        if m >= 50:
            return round(m / pieces, 2) if pieces else None
        return m
    pot = getattr(p, "potency_mg", None)
    if isinstance(pot, (int, float)) and pot > 0:
        if pieces:
            return round(float(pot) / pieces, 2)
        return float(pot) if pot <= 50 else None
    return None
