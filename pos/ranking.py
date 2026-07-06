"""Profile-aware ranking for the POS menu.

This was a forked copy of happytime's ranker; it is now a thin re-export of the
shared ``budtender.engine`` so the in-store menu and the website rank on ONE
formula and can never drift again. The ``pos.ranking.*`` import surface is kept
stable for ``catalog``, ``views`` and the tests.
"""
from budtender.engine import (  # noqa: F401
    blend_session_taste,
    rank,
    _affinity_score as affinity_score,
    _quality_fit as quality_fit,
    _thc_band_fit as thc_band_fit,
)
