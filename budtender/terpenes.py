"""Terpene % -> the words a pick shows. The ONE place that happens; the website and the voice
agent repeat `lab.profile`, they never write their own.

Only six terpenes carry notes and a lean, and only because the site's approved education text
(happytimeweed/prompts/education-knowledge.md, "THC vs CBD vs terpenes") already says so. Every other
terpene is shown by name and % with NO description — nothing here is invented.

Wording is bound by happytimeweed/LCB-CONTENT-COMPLIANCE.md section 2.1: experiential words only,
always hedged ("often described as"), never therapeutic (budtender.compliance is the same screen applied
to operator text, and tests sweep every string below against it). Pure functions: no Django, no I/O.
"""
from __future__ import annotations

import re

# THE SOURCE (happytimeweed/prompts/education-knowledge.md, line 50), verbatim:
#   "**Terpenes** — the aromas that shape the experience: **myrcene** (earthy → relaxed), **limonene**
#    (citrus → mood lift), **pinene** (pine → alert), **linalool** (lavender → calm), **caryophyllene**
#    (pepper), **terpinolene** (floral)."
# canonical terpene -> aroma notes + the experiential lean (None = no lean to claim). A note is only a
# word the source names; a lean is the source's own word where it has one ("alert", "calm") and the
# LCB-approved experiential wording for "relaxed" / "mood lift" (relaxing / uplifting). Caryophyllene and
# terpinolene claim no effect at all, because the source claims none.
NOTES: dict[str, dict] = {
    "myrcene": {"label": "Myrcene", "notes": ["earthy"], "lean": "relaxing"},
    "limonene": {"label": "Limonene", "notes": ["citrus"], "lean": "uplifting"},
    "pinene": {"label": "Pinene", "notes": ["pine"], "lean": "alert"},
    "linalool": {"label": "Linalool", "notes": ["lavender"], "lean": "calm"},
    "caryophyllene": {"label": "Caryophyllene", "notes": ["pepper"], "lean": None},
    "terpinolene": {"label": "Terpinolene", "notes": ["floral"], "lean": None},
}

# The five aromas the questionnaire offers (the `aroma` slot) -> the terpenes that carry each: the ONE aroma
# map. It serves the ranking boost (ranking.py), `profile().aroma` and the sentence in `profile().explain`.
# Each terpene sits under exactly one aroma, and only the six terpenes in NOTES appear (tests pin both).
AROMA_TERPENES: dict[str, set[str]] = {
    "citrus": {"limonene"},
    "earthy": {"myrcene"},
    "pine": {"pinene"},
    "floral": {"linalool", "terpinolene"},
    "spicy": {"caryophyllene"},
}
_AROMA_OF = {t: aroma for aroma, ts in AROMA_TERPENES.items() for t in ts}
_AROMA_WORD = {"citrus": "citrusy", "earthy": "earthy", "pine": "piney", "floral": "floral", "spicy": "spicy"}

_PREFIX = re.compile(r"^(?:(?:alpha|beta|gamma|delta|trans|cis)\d*[\s\-_]*)+")
# A lab's terpene name is vendor text: only a plain one is ever put into a sentence.
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9 \-]{0,29}")
_NOTE_TERPENES = 3  # notes, aroma and the explanation are drawn from this many of the strongest terpenes


def canonical(name) -> str:
    """'Beta-Myrcene' -> 'myrcene', 'Alpha-Pinene' -> 'pinene'. '' for anything that is not a name."""
    if not isinstance(name, str):
        return ""
    return _PREFIX.sub("", name.strip().lower()).strip()


def _pct(value) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def leaning(*leans: str) -> set[str]:
    """The canonical terpenes whose lean is one of `leans`. engine.EFFECT_HINTS is built from this, so a
    terpene's effect is defined once, here."""
    return {key for key, entry in NOTES.items() if entry["lean"] in leans}


def _rows(terpenes) -> list[dict]:
    """The usable [{"name", "pct"}] rows (a name and a positive number), strongest first."""
    rows = [t for t in terpenes or []
            if isinstance(t, dict) and isinstance(t.get("pct"), (int, float)) and t["pct"] > 0 and t.get("name")]
    return sorted(rows, key=lambda t: -t["pct"])


def aroma_hit(terpenes, aroma) -> tuple[str, bool] | None:
    """(terpene, leads) when one of the strongest three terpenes carries `aroma`: the strongest such
    terpene, and whether it is THE strongest. None for no match, no data, or an aroma that is not one of
    the five in AROMA_TERPENES."""
    wanted = AROMA_TERPENES.get(aroma) if isinstance(aroma, str) else None
    if not wanted:
        return None
    for i, t in enumerate(_rows(terpenes)[:_NOTE_TERPENES]):
        name = canonical(t["name"])
        if name in wanted:
            return name, i == 0
    return None


def _explain(rows: list[dict]) -> dict:
    """{"explain", "aroma"} from the strongest three terpenes (`rows`, strongest first): one aroma sentence
    from fixed phrases plus the lab's own terpene names, then one hedged experience sentence when the
    STRONGEST terpene has a lean (the same rule as `lean`). An unknown terpene is named and described
    as nothing. No model, nothing invented; the beta disclaimer is the surface's to add."""
    top = [canonical(t["name"]) for t in rows[:_NOTE_TERPENES]]
    aroma = list(dict.fromkeys(_AROMA_OF[n] for n in top if n in _AROMA_OF))
    names = ", ".join(n for n in top if _SAFE_NAME.fullmatch(n))
    if aroma:
        words = [_AROMA_WORD[a] for a in aroma]
        text = f"Smells {', '.join(words[:-1])}{' and ' if len(words) > 1 else ''}{words[-1]} ({names})."
    else:
        text = f"Top terpenes: {names}." if names else ""
    lean = NOTES.get(top[0] if top else "", {}).get("lean")
    if lean and text:
        text += f" Customers often describe profiles like this as {lean} — everyone is different."
    return {"explain": text, "aroma": aroma}


def profile(terpenes) -> dict:
    """[{"name", "pct"}] -> {"lean", "notes", "line", "explain", "aroma"}.

    `lean` and `line` come from the single strongest terpene; `notes` from the strongest three
    that are among the six (an unknown terpene adds nothing); `explain` (<= 280 chars) and `aroma`
    are the plain-words reading of the strongest three, see `_explain`. Nothing to describe -> an
    empty profile, never a guess."""
    rows = _rows(terpenes)
    if not rows:
        return {"lean": None, "notes": [], "line": "", "explain": "", "aroma": []}

    notes: list[str] = []
    for t in rows[:_NOTE_TERPENES]:
        for note in NOTES.get(canonical(t["name"]), {}).get("notes", []):
            if note not in notes:
                notes.append(note)

    top = rows[0]
    known = NOTES.get(canonical(top["name"]))
    label = known["label"] if known else (canonical(top["name"]).title() or str(top["name"]))
    line = f"{label}-led ({_pct(top['pct'])}%)"
    lean = None
    if known:
        line += " — " + ", ".join(known["notes"])
        lean = known["lean"]
        if lean:
            line += f"; often described as {lean}"
    return {"lean": lean, "notes": notes, "line": line + ".", **_explain(rows)}
