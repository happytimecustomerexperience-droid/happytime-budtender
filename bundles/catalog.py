"""Bundle definitions and store identity.

Slot rules live HERE, server-side, keyed by the bundle slug. The URL carries which
bundle and which SKUs, never the discount depth — otherwise editing `b=weekend` onto
a cheap cart would mint a 30% coupon.

Categories are `pos.imagemap.category_key` slugs (what `pos.catalog._normalize`
stamps as `cat_key`), NOT `budtender.dutchie._norm_category` slugs. The two
vocabularies disagree — `vapes` vs `vape-cartridges`, `concentrate` vs
`concentrates` — and a slot matcher that crossed them would silently never match.
See CATEGORY_ALIASES for the bridge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote_plus

# ── stores ───────────────────────────────────────────────────────────────────
# The URL's `loc` is the happytime location_slug (what the website and
# budtender_product use). pos.catalog is keyed by the Dutchie store_key. They
# differ for Mount Vernon.
LOCATION_TO_STORE_KEY = {"yakima": "yakima", "mount-vernon": "mtvernon", "pullman": "pullman"}
STORE_LABELS = {
    "yakima": "Happy Time — Yakima",
    "mount-vernon": "Happy Time — Mount Vernon",
    "pullman": "Happy Time — Pullman",
}
# Street addresses and phones, taken from happytimeweed.com's own content
# (data/author.json + the location pages) rather than retyped — a shopper is being
# told where to drive and who to call, so these must match the marketing site
# exactly. Yakima is the default store; the other two are opt-in via the picker.
STORES = {
    "yakima": {
        "street": "1315 N 1st St",
        "city": "Yakima, WA 98901",
        "phone": "(509) 571-1106",
        "hours": "8 AM – 11:30 PM daily",
    },
    "mount-vernon": {
        "street": "200 Suzanne Ln",
        "city": "Mt Vernon, WA 98273",
        "phone": "(360) 488-2923",
        "hours": "9 AM – 10 PM daily",
    },
    "pullman": {
        "street": "5602 WA-270",
        "city": "Pullman, WA 99163",
        "phone": "(509) 334-2788",
        "hours": "9 AM – 10 PM daily",
    },
}

# Kept as the one-line form the rest of the app already renders. STORES is the
# detail; this stays the short label so existing templates don't change meaning.
STORE_ADDRESS = {slug: f"{s['street']}, {s['city']}" for slug, s in STORES.items()}


def store_key_for(location_slug: str) -> str:
    return LOCATION_TO_STORE_KEY.get(location_slug, location_slug)


def store_label(location_slug: str) -> str:
    return STORE_LABELS.get(location_slug, location_slug.replace("-", " ").title())


def store_info(location_slug: str) -> dict:
    """Everything a pickup card needs: label, street, city, phone, hours, map link."""
    s = STORES.get(location_slug) or {}
    street, city = s.get("street", ""), s.get("city", "")
    full = ", ".join(p for p in (street, city) if p)
    return {
        "slug": location_slug,
        "label": store_label(location_slug),
        "street": street,
        "city": city,
        "address": full,
        "phone": s.get("phone", ""),
        "hours": s.get("hours", ""),
        # Query-based so it resolves without us pinning a place id that can rot.
        "map_url": (
            "https://www.google.com/maps/search/?api=1&query="
            + quote_plus(f"Happy Time Dispensary {full}")
        ) if full else "",
    }


def all_stores() -> list[dict]:
    """Every store, Yakima first — it is the default and the largest by far."""
    order = ["yakima"] + [s for s in STORES if s != "yakima"]
    return [store_info(s) for s in order]


# `budtender.dutchie._norm_category` slug -> `pos.imagemap.category_key` slug.
# Only the ones that actually differ.
CATEGORY_ALIASES = {
    "vape-cartridges": "vapes",
    "concentrates": "concentrate",
    "beverages": "edibles",   # drinks share the edible slot in every bundle
    "capsules": "edibles",
}


def canon_category(cat: str) -> str:
    c = (cat or "").strip().lower()
    return CATEGORY_ALIASES.get(c, c)


@dataclass(frozen=True)
class Slot:
    """One line of a bundle's promise, e.g. '2x 1pk pre-roll'."""

    key: str
    label: str
    qty: int
    categories: tuple[str, ...]           # cat_key values that satisfy this slot
    sizes: tuple[str, ...] = ()           # subcategory values, '' = any size
    # Weight equality is a hard gate for flower and pre-rolls — a 1g cart does not
    # satisfy a 3.5g flower slot, and swapping across sizes breaks the discount math.
    strict_size: bool = False

    def accepts(self, item: dict) -> bool:
        if canon_category(item.get("cat_key") or "") not in self.categories:
            return False
        if self.sizes and self.strict_size:
            return _size_of(item) in self.sizes
        return True


def _size_of(item: dict) -> str:
    return str(item.get("subcategory") or "").strip().lower()


@dataclass(frozen=True)
class Bundle:
    slug: str
    name: str
    discount_pct: int
    slots: tuple[Slot, ...] = field(default_factory=tuple)

    @property
    def item_count(self) -> int:
        return sum(s.qty for s in self.slots)


FLOWER = ("flower",)
PREROLL = ("pre-rolls",)
VAPE = ("vapes",)
EDIBLE = ("edibles",)

BUNDLES: dict[str, Bundle] = {
    "roll-relax": Bundle(
        slug="roll-relax", name="Roll & Relax Bundle", discount_pct=20,
        slots=(
            Slot("flower", "3.5g flower", 1, FLOWER, ("3.5g",), strict_size=True),
            Slot("preroll", "1pk pre-roll (regular or infused)", 2, PREROLL),
            Slot("edible", "10pk edible or drink", 1, EDIBLE),
        ),
    ),
    "vape-munch": Bundle(
        slug="vape-munch", name="Vape & Munch Bundle", discount_pct=25,
        slots=(
            Slot("vape", "Vape cart or disposable", 1, VAPE),
            Slot("preroll", "1pk pre-roll (regular or infused)", 1, PREROLL),
            Slot("edible", "10pk edible or drink", 1, EDIBLE),
        ),
    ),
    "weekend": Bundle(
        slug="weekend", name="Weekend Bundle", discount_pct=30,
        slots=(
            Slot("flower", "3.5g flower", 1, FLOWER, ("3.5g",), strict_size=True),
            Slot("vape", "Vape cart or disposable", 1, VAPE),
            Slot("preroll", "Single pre-roll (regular or infused)", 2, PREROLL),
            Slot("edible", "10pk edible or drink", 1, EDIBLE),
        ),
    ),
}


def get_bundle(slug: str) -> Bundle | None:
    return BUNDLES.get((slug or "").strip().lower())
