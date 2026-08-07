"""Out-the-door (OTD, tax-included) price helpers for spoken picks.

**The menu price IS the out-the-door price. Nothing is added.**

This module used to uplift budtender's ``price`` by a WA excise + local-sales-tax multiplier
(Yakima ×1.48508), on the ADR-009 assumption that budtender returned a PRE-TAX price. That
assumption was wrong for this dispensary, and the agent was quoting roughly 48% over the real
price on every single call — a $38 eighth was spoken as "56 dollars and 43 cents".

Corrected 2026-08-07 against hard evidence, see ``bundles/tax.py``: this account's Dutchie is
configured ``taxInclusivePricing: true``, verified from a real Yakima pickup cart read out of
Dutchie's own ``computeWithPriceCartV2`` response — menu prices $27.00 + $25.00 + $15.00 checked
out at exactly $67.00, and Dutchie's menu says "*All taxes included in price." Two statutes make
that the legal shelf price: RCW 69.50.535(1)(b) (the 37% excise must be reflected in the quoted
shelf price) and RCW 82.08.050 / WAC 458-20-107 (a quoted price excludes sales tax UNLESS the
seller advertises tax-included, which Dutchie does on every product page for this account).

So ``otd()` is now identity-with-rounding. It is kept as a function, rather than deleted, because
it is the ONE place the "what does the customer actually pay" question is answered for voice — if
the account is ever reconfigured to tax-exclusive pricing, this is the single line to change.

Deliberately NOT done here: splitting the total into Dutchie's Subtotal/Taxes lines. Reproducing
that split from the captured cart lands ~3c off on a $67 order (Dutchie rounds per item, per tax
type), and a spoken tax figure that disagrees with the receipt is worse than no tax figure.

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
