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
