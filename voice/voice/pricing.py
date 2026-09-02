"""Out-the-door (OTD) price helper for spoken picks.

The tax rule ("the menu price IS the price — nothing is added") lives in ONE place:
``bundles/tax.py`` at the repo root (RCW 69.50.535(1)(b) / WAC 458-20-107). The voice image's
Docker build context is ``voice/`` only (see ``voice/Dockerfile`` / ``docker-compose*.yaml``), so
it cannot import that root package — ``otd()`` below is identity-with-rounding, kept in lockstep
with ``bundles.tax.quote()`` by ``bundles/tests/test_pricing_lockstep.py`` at the root.

**Leak-safe:** derives from the allowlisted ``price`` only — no cost/margin.
"""

from __future__ import annotations


def otd(price: float | int | None, store: str | None = None) -> float:
    """The out-the-door price the customer pays — the menu price, unchanged.

    ``store`` is accepted and ignored: the price is tax-inclusive at every store, and keeping the
    parameter means callers do not change if per-store behaviour ever diverges again. A
    None/non-positive price returns ``0.0`` (no fabricated number — Numbers-Guard)."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0:
        return 0.0
    return round(p, 2)


def spoken(amount: float | int | None) -> str:
    """A TTS-safe spoken form of a dollar amount so the voice reads it as words, never the mangled
    "$16.34". ``16.34 -> "16 dollars and 34 cents"``, ``30 -> "30 dollars"``, ``1.05 -> "1 dollar
    and 5 cents"``. A None/non-positive amount → ``""`` (the agent then says nothing — Numbers-Guard).

    The agent is told to voice THIS string verbatim; it never speaks the bare ``price_otd`` number."""
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return ""
    if a <= 0:
        return ""
    dollars = int(a)
    cents = int(round((a - dollars) * 100))
    if cents >= 100:  # rounding edge (e.g. 16.999): carry into dollars
        dollars += 1
        cents -= 100
    d_word = "dollar" if dollars == 1 else "dollars"
    if cents == 0:
        return f"{dollars} {d_word}"
    c_word = "cent" if cents == 1 else "cents"
    if dollars == 0:
        return f"{cents} {c_word}"
    return f"{dollars} {d_word} and {cents} {c_word}"
