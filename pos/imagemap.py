"""Resolve product imagery: live Dutchie image -> brand logo -> category tile.

Brand logos + category tiles were copied from happytimeweed into
static/budtender/img/{brands,categories}/. We match the Dutchie BrandName /
ProductCategory to a file by normalized name (lowercase, alnum-only), so
"Phat Panda" -> "phat panda.png", "Ray's Lemonade" -> "ray-s-lemonade.png".
Returns paths for {% static %}; None when there's no match (template falls back).
"""

from __future__ import annotations

import os
import re

_DIR = os.path.join(os.path.dirname(__file__), "static", "pos", "img")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _scan(sub):
    out = {}
    d = os.path.join(_DIR, sub)
    try:
        for fn in os.listdir(d):
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg")):
                out[_norm(os.path.splitext(fn)[0])] = f"pos/img/{sub}/{fn}"
    except FileNotFoundError:
        pass
    return out


_BRANDS = _scan("brands")
_CATS = _scan("categories")


def category_key(cat):
    """Map a Dutchie category string to one of our tile keys, or '' (none)."""
    c = (cat or "").lower()
    if "pre" in c and "roll" in c:
        return "pre-rolls"
    if "flower" in c or "bud" in c:
        return "flower"
    if "edible" in c or "gummy" in c or "gummies" in c or "chocolate" in c or "candy" in c:
        return "edibles"
    if "cart" in c or "vape" in c or "vaporizer" in c or "disposable" in c:
        return "vapes"
    if any(k in c for k in ("concentrate", "rosin", "wax", "dab", "extract", "bho", "hash", "shatter", "diamond")):
        return "concentrate"
    if "topical" in c:
        return "topicals"
    if "tincture" in c or "sublingual" in c:
        return "tinctures"
    return ""


def brand_logo(brand):
    return _BRANDS.get(_norm(brand))


def category_tile(category):
    return _CATS.get(category_key(category))


def product_image(p):
    """Preview = the BRAND LOGO when we have one, else None (the card renders the
    brand NAME as a clean text tile). Owner decision: a consistent branded catalog
    look, not mixed product photos. Returns (static_path_or_None, is_static=True)."""
    return brand_logo(p.get("brand")), True
