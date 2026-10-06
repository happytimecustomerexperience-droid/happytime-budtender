"""Dutchie's product-master record -> `info`, the allowlisted facts a pick may carry (Contract D).

POST /api/product-master/get-product-details-v2 returns ~155 keys, and among them Cost, Vendor, VendorId,
location prices, WeedMaps/LeafLink ids and the operator's free-text descriptions. NONE of that may reach a
customer, so this is an ALLOWLIST: a key not named below cannot get through, by construction.

Left out on purpose: OnlineDescription / BigOnlineDescription (operator free text: possible therapeutic
claims and a prompt-injection surface; the owner may opt in later after a compliance screen) and
THCContent / CBDContent (their units are unverified, so they are never interpreted). Free text that is
allowed is stripped of markup and control characters, collapsed and capped. Pure: no Django, no I/O.
"""
from __future__ import annotations

import html
import json
import re
import unicodedata

from . import compliance

MAX_TEXT = 300         # short free text (serving size, flavor, instructions, names): cut at a word boundary
INGREDIENT_CAP = 600   # ingredients: cut at a comma/space boundary, never mid-word, marked with an ellipsis
ALLERGEN_CAP = 1000    # allergens: NEVER cut. Over the cap the whole key is omitted (see _allergens)
MAX_LIST = 12
ELLIPSIS = "…"

_BLOCK_TAGS = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")

# info key -> Dutchie key, for the plain-text fields
_TEXT = (("strain_type", "StrainType"), ("brand", "BrandName"))
_FREE_TEXT = (("serving_size", "ServingSize"), ("flavor", "Flavor"), ("instructions", "ProductInstructions"))
_CATEGORY = (("ecom_category", "EcomCategory"), ("ecom_subcategory", "EcomSubcategory"))

# What `info` may ever contain, and the shape of each key (public_info enforces it again at serialization).
INFO_KEYS = ("strain_type", "brand", "doh_approved", "high_cbd", "tags", "ingredients", "allergens",
             "active_ingredients", "serving_size", "flavor", "instructions", "ecom_category", "ecom_subcategory")


def _strip_controls(s: str) -> str:
    return "".join(ch if (ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf")) else "" for ch in s)


def _normalize(value) -> str:
    """Plain text from a free-text field, UNCUT: markup and control/format characters removed,
    whitespace collapsed. Anything that is not a string is ignored (never guessed)."""
    if not isinstance(value, str):
        return ""
    s = html.unescape(_BLOCK_TAGS.sub(" ", value))
    s = _BLOCK_TAGS.sub(" ", s)  # script/style blocks that only appeared once the entities were decoded
    s = _TAGS.sub(" ", s)
    return _SPACE.sub(" ", _strip_controls(s)).strip()


def _cut(text: str, cap: int, mark: bool = False) -> str:
    """`text` within `cap` characters, cut at the last comma/space boundary so no word is ever split.
    `mark` appends an ellipsis (inside the cap) when something was cut, so a partial list never reads
    as complete. Only a single unbroken token longer than the cap is cut hard."""
    if len(text) <= cap:
        return text
    room = cap - 1 if mark else cap
    if text[room] in " ,":
        cut = text[:room]  # the cut already falls on a boundary
    else:
        i = max(text.rfind(" ", 0, room), text.rfind(",", 0, room))
        cut = text[:i] if i > 0 else text[:room]
    return cut.rstrip(" ,;") + (ELLIPSIS if mark else "")


def _safe(text: str) -> str:
    """`text`, or "" when it carries a therapeutic claim or anything that talks to a model: operator-typed
    text is dropped whole, never sanitised into something that merely looks fine."""
    return "" if (compliance.therapeutic_hits(text) or compliance.injection_hits(text)) else text


def _clean(value, cap: int = MAX_TEXT, mark: bool = False) -> str:
    """Screened plain text from a free-text field, capped at `cap` characters at a word boundary."""
    return _cut(_safe(_normalize(value)), cap, mark)


def _allergens(value) -> str:
    """The allergen list WHOLE, or "". It is never tag-stripped ("Soy (>1%)" must keep "Soy"), only
    control characters and whitespace are cleaned; it gets the injection screen (without the bare `<` / `>`
    rule) but NEVER a therapeutic drop; and a list over the cap is omitted entirely rather than cut,
    because a partial allergen list could drop the one allergen that matters to a customer."""
    if not isinstance(value, str):
        return ""
    text = _SPACE.sub(" ", _strip_controls(value)).strip()
    if not text or len(text) > ALLERGEN_CAP or compliance.injection_hits(text, markup=False):
        return ""
    return text


def _names(value) -> list[str]:
    """A list of plain names from a comma string, a list, or a JSON string of either; objects are read
    by their Name. Each name is screened on its own (a bad one is dropped, the rest stay)."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("[", "{"):
            try:
                value = json.loads(text)
            except ValueError:
                value = text.split(",")
        else:
            value = text.split(",")
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = next((item[k] for k in ("Name", "name", "IngredientName") if isinstance(item.get(k), str)), "")
        name = _clean(item)[:60]
        if name and name not in out:
            out.append(name)
    return out[:MAX_LIST]


def has_structure(data) -> bool:
    """Does this look like a product-master record at all (as opposed to an empty/odd answer)?"""
    return isinstance(data, dict) and "ProductId" in data


def info_from_data(data) -> dict | None:
    """Allowlisted `info` from a product-master record, or None when nothing customer-facing is filled in."""
    if not isinstance(data, dict):
        return None
    info: dict = {}
    for key, src in _TEXT:
        if value := _clean(data.get(src)):
            info[key] = value
    # A flag is stated only when it is true: "DOH approved" is a fact worth showing; "not DOH approved"
    # on a card would be noise, and the absence of a flag is not a claim either way.
    for key, src in (("doh_approved", "DoHApproved"), ("high_cbd", "HighCBD")):
        if data.get(src) is True:
            info[key] = True
    if tags := _names(data.get("ProductTags")):
        info["tags"] = tags
    if value := _clean(data.get("IngredientList"), INGREDIENT_CAP, mark=True):
        info["ingredients"] = value
    if value := _allergens(data.get("AllergenList")):
        info["allergens"] = value
    if active := _names(data.get("ActiveIngredients")):
        info["active_ingredients"] = active
    for key, src in _FREE_TEXT + _CATEGORY:
        if value := _clean(data.get(src)):
            info[key] = value
    return info or None


def public_info(info) -> dict | None:
    """The stored `info` re-validated for a customer: only the allowlisted keys, only the right types,
    every text screened again. A stored row is never trusted to be clean (it may predate a rule, or carry a
    key it should not), so a correction to the rules takes effect without re-fetching."""
    if not isinstance(info, dict):
        return None
    out: dict = {}
    for key in ("strain_type", "brand", "serving_size", "flavor", "instructions", "ecom_category", "ecom_subcategory"):
        if value := _clean(info.get(key)):
            out[key] = value
    for key in ("doh_approved", "high_cbd"):
        if info.get(key) is True:
            out[key] = True
    for key in ("tags", "active_ingredients"):
        if isinstance(info.get(key), list) and (names := _names(info[key])):
            out[key] = names
    if value := _clean(info.get("ingredients"), INGREDIENT_CAP, mark=True):
        out["ingredients"] = value
    if value := _allergens(info.get("allergens")):
        out["allergens"] = value
    return {k: out[k] for k in INFO_KEYS if k in out} or None
