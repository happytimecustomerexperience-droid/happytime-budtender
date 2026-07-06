"""Generic cannabis education — terpene / effect / strain-type explanations shown on
the product detail page (Dutchie-menu style). These are GENERIC reference facts, not
per-brand data, so a small constant table is appropriate. Lookups are normalized
(lowercase, strip) and degrade to None when unknown."""

from __future__ import annotations

import re

TERPENES = {
    "myrcene": ("Earthy, musky, clove", "The most common cannabis terpene — relaxing and sedating; the classic body-heavy, couch-lock feel."),
    "limonene": ("Bright citrus, lemon", "Uplifting and mood-boosting; associated with stress relief and an energetic headspace."),
    "caryophyllene": ("Peppery, spicy, woody", "The only terpene that binds CB2 receptors — calming and known for anti-inflammatory, soothing effects."),
    "beta-caryophyllene": ("Peppery, spicy, woody", "Binds CB2 receptors — calming, anti-inflammatory, soothing."),
    "pinene": ("Fresh pine, rosemary", "Promotes alertness and focus and may counteract short-term memory fog."),
    "alpha-pinene": ("Fresh pine, rosemary", "Alertness and focus; may offset THC's memory effects."),
    "linalool": ("Floral, lavender", "Calming and relaxing; widely associated with anti-anxiety and restful effects."),
    "terpinolene": ("Herbal, piney, floral", "Fresh and uplifting; common in bright, energetic sativa-leaning strains."),
    "humulene": ("Hoppy, woody, earthy", "Grounding and known as an appetite suppressant; shared with hops."),
    "ocimene": ("Sweet, herbal, woody", "Uplifting with a fresh, herbal character; mild decongestant reputation."),
    "bisabolol": ("Chamomile, soft floral", "Gentle and soothing; calming and skin-friendly."),
    "guaiol": ("Pine, rose, wood", "Earthy and grounding; studied for anti-inflammatory potential."),
    "nerolidol": ("Woody, fresh bark, citrus", "Relaxing and soft; mild sedative character."),
    "eucalyptol": ("Cool, minty, eucalyptus", "Fresh and clarifying; associated with focus and easy breathing."),
}

EFFECTS = {
    "relaxed": "Eases body and mind — good for winding down.",
    "relaxing": "Eases body and mind — good for winding down.",
    "calm": "Settles the nerves without heavy sedation.",
    "uplifted": "Bright, mood-lifting headspace.",
    "happy": "Light, positive, feel-good mood.",
    "euphoric": "Strong, blissful elevation.",
    "sleepy": "Sedating — best saved for nighttime.",
    "sedated": "Heavy, restful — nighttime / couch-lock.",
    "focused": "Clear, productive, dialed-in headspace.",
    "energetic": "Active and motivated — daytime energy.",
    "energized": "Active and motivated — daytime energy.",
    "creative": "Free-flowing, imaginative headspace.",
    "hungry": "Appetite stimulation — 'the munchies'.",
    "talkative": "Social and chatty.",
    "tingly": "Light physical, body-buzz sensation.",
    "pain": "Reported relief from aches and tension.",
    "pain relief": "Reported relief from aches and tension.",
    "anxiety": "Reported easing of anxious, racing thoughts.",
    "stress": "Reported stress relief and decompression.",
}

STRAIN_TYPES = {
    "indica": "Indica-leaning — typically relaxing, body-heavy, evening-friendly.",
    "sativa": "Sativa-leaning — typically uplifting, heady, daytime-friendly.",
    "hybrid": "Hybrid — a balanced blend of relaxing and uplifting traits.",
    "cbd": "CBD-forward — minimal high, oriented toward calm and relief.",
}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def terpene_info(name):
    """(aroma, effect) for a terpene name, or None."""
    return TERPENES.get(_norm(name)) or TERPENES.get((name or "").strip().lower())


def effect_info(name):
    n = (name or "").strip().lower()
    return EFFECTS.get(n) or EFFECTS.get(_norm(n))


def strain_type_info(name):
    return STRAIN_TYPES.get((name or "").strip().lower())
