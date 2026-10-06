"""Generic cannabis education — terpene / effect / strain-type explanations shown on
the product detail page (Dutchie-menu style). These are GENERIC reference facts, not
per-brand data, so a small constant table is appropriate. Lookups are normalized
(lowercase, strip) and degrade to None when unknown.

Wording is bound by happytimeweed/LCB-CONTENT-COMPLIANCE.md 2.1: experiential words only, hedged, nothing
therapeutic (pos/tests/test_education.py sweeps every string against budtender.compliance and the website's
own rules). A terpene's EFFECT comes from budtender.terpenes, the one source the cards and the phone agent
use; only the aroma words (a description, not a claim) live here."""

from __future__ import annotations

import re

from budtender import terpenes as _terpenes

# Aroma words per canonical terpene (budtender.terpenes.canonical: "beta-caryophyllene" -> "caryophyllene").
_AROMA = {
    "myrcene": "Earthy, musky, clove",
    "limonene": "Bright citrus, lemon",
    "caryophyllene": "Peppery, spicy, woody",
    "pinene": "Fresh pine, rosemary",
    "linalool": "Floral, lavender",
    "terpinolene": "Herbal, piney, floral",
    "humulene": "Hoppy, woody, earthy",
    "ocimene": "Sweet, herbal, woody",
    "bisabolol": "Chamomile, soft floral",
    "guaiol": "Pine, rose, wood",
    "nerolidol": "Woody, fresh bark, citrus",
    "eucalyptol": "Cool, minty, eucalyptus",
}

EFFECTS = {
    "relaxed": "Eases body and mind — good for winding down.",
    "relaxing": "Eases body and mind — good for winding down.",
    "calm": "A calm, easygoing headspace.",
    "uplifted": "Bright, mood-lifting headspace.",
    "happy": "Light, positive, feel-good mood.",
    "euphoric": "Strong, blissful elevation.",
    "sleepy": "Deeply relaxing — often an evening choice.",
    "sedated": "Heavy, deeply relaxed — often an evening choice.",
    "focused": "Clear, productive, dialed-in headspace.",
    "energetic": "Active and motivated — daytime energy.",
    "energized": "Active and motivated — daytime energy.",
    "creative": "Free-flowing, imaginative headspace.",
    "hungry": "Appetite stimulation — 'the munchies'.",
    "talkative": "Social and chatty.",
    "tingly": "Light physical, body-buzz sensation.",
    # The condition tags (pain / anxiety / stress) get no blurb: describing what they do is a
    # therapeutic claim, so the page shows the tag alone.
}

STRAIN_TYPES = {
    "indica": "Indica-leaning — typically relaxing, body-heavy, evening-friendly.",
    "sativa": "Sativa-leaning — typically uplifting, heady, daytime-friendly.",
    "hybrid": "Hybrid — a balanced blend of relaxing and uplifting traits.",
    "cbd": "CBD-forward — minimal high, a calmer, clear-headed experience.",
}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def terpene_info(name):
    """(aroma, effect) for a terpene name, or None. The effect is the hedged lean from
    budtender.terpenes, or "" for a terpene it claims nothing for."""
    key = _terpenes.canonical(name)
    aroma = _AROMA.get(key)
    if aroma is None:
        return None
    lean = _terpenes.NOTES.get(key, {}).get("lean")
    return (aroma, f"Often described as {lean} — everyone is different." if lean else "")


def effect_info(name):
    n = (name or "").strip().lower()
    return EFFECTS.get(n) or EFFECTS.get(_norm(n))


def strain_type_info(name):
    return STRAIN_TYPES.get((name or "").strip().lower())
