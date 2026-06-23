"""Recorded copy of budtender ranking.py's taxonomy constants — the parity anchor for the
voice KB weights/types taxonomy (22-SPEC-kb-seed.md §3.6 / acceptance D5).

The voice agent and the budtender suggestion API must speak the SAME vocabulary (taxonomy
parity, binding). budtender is a separate repo/service (its env holds the Dutchie keys —
ADR-004/019), not a Python import path here, so its constants are RECORDED here verbatim from
``happytime-budtender/budtender/ranking.py`` (``_SUBTYPE_KEYWORDS["concentrates"]`` value
slugs + ``_GRAM_HINTS``). If budtender adds/renames a subtype, update this file and re-seed.

The parity test (tests/test_kb.py) reads these to assert every ``axis=concentrate_subtype``
term and every ``axis=weight`` term we seed is a real budtender vocabulary slug.
"""

from __future__ import annotations

# budtender ranking._SUBTYPE_KEYWORDS["concentrates"] — the value (slug) of each tuple.
# Recorded from happytime-budtender/budtender/ranking.py L298-316.
CONCENTRATE_SUBTYPE_VALUES = frozenset(
    {
        "rosin",
        "live-resin",
        "rso",
        "distillate",
        "diamonds",
        "sauce",
        "badder",
        "shatter",
        "crumble",
        "sugar",
        "wax",
        "hash",
        "kief",
        "applicator",
    }
)

# budtender ranking._GRAM_HINTS — float grams → human label. Recorded from ranking.py L382-385.
GRAM_HINTS = {
    0.5: "Half gram",
    1.0: "Gram",
    2.0: "2 grams",
    3.5: "Eighth",
    4.0: "4 grams",
    7.0: "Quarter",
    8.0: "8 grams",
    10.0: "10 grams",
    14.0: "Half oz",
    28.0: "Ounce",
}

# The weight-axis terms the voice taxonomy uses, mapped to their gram value — must each
# correspond to a budtender _GRAM_HINTS entry (parity). value-string → grams.
WEIGHT_TERM_GRAMS = {
    "half-gram": 0.5,
    "gram": 1.0,
    "two grams": 2.0,
    "eighth": 3.5,
    "four grams": 4.0,
    "quarter": 7.0,
    "eight grams": 8.0,
    "ten grams": 10.0,
    "half-ounce": 14.0,
    "ounce": 28.0,
}
