"""What a Happy Time customer actually pays.

**The menu price IS the price. Nothing is added at checkout.**

Verified against Dutchie's own pre-submit checkout for this dispensary (a real 3-item
Yakima pickup cart, read out of its `computeWithPriceCartV2` GraphQL response):

    menu prices   $27.00 + $25.00 + $15.00  = $67.00
    Dutchie shows  Subtotal $54.05
                   Discount -$8.00
                   Taxes    $20.95
                   ORDER TOTAL $67.00       <- equals the menu prices, exactly

and the response carries `taxInclusivePricing: true`. Dutchie's own menu and cart
drawer say it in words: "*All taxes included in price."

So the storefront's job is NOT to add tax. It is to stop implying tax is coming. The
old copy — "Taxes and the final total are calculated at the register" — understated the
price the customer will pay is already on screen, and invited them to expect a bigger
number at the counter.

Two statutes explain how a tax-inclusive shelf price is legal and how it decomposes:

  * RCW 69.50.535(1)(b) — the 37% cannabis excise "must be reflected in the price list
    or quoted shelf price".
  * RCW 82.08.050 / WAC 458-20-107 — a quoted price is presumed to EXCLUDE sales tax
    unless the seller advertises that tax is included, which Dutchie does on every
    product page for this account.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: split the total into Dutchie's exact
Subtotal / Taxes lines. Reproducing that split from the one cart we captured lands
about three cents off on a $67 order, and the per-item numbers did not fall out of any
single rate we could confirm (Dutchie appears to round per item, per tax type). A
displayed tax figure that disagrees with the receipt is worse than no tax figure. If
the exact breakdown is wanted, the honest route is to call `computeWithPriceCartV2`
rather than to model it — see docs/custom-order-bundles.md.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")

# Combined state+local sales tax, from DOR's address API, keyed by LOCATION CODE rather
# than city name — the Pullman store geocodes to unincorporated Whitman County (3800),
# NOT the City of Pullman, and its address says "Pullman". Effective Q3 2026.
#
# Used only for the informational "roughly this much of your price is tax" figure. The
# TOTAL never depends on it, so a stale rate here cannot change what anyone is charged.
SALES_TAX = {
    "yakima": (Decimal("0.086"), "3913", "Q3 2026"),        # YAKIMA CITY
    "mount-vernon": (Decimal("0.090"), "2907", "Q3 2026"),  # MOUNT VERNON
    "pullman": (Decimal("0.080"), "3800", "Q3 2026"),       # WHITMAN COUNTY, unincorporated
}

# RCW 69.50.535(1)(a). Applied to the price net of tax, alongside sales tax.
EXCISE_RATE = Decimal("0.37")


def _money(value) -> Decimal:
    # Half-up, the way a till rounds. Python's default banker's rounding settles half
    # cents down as often as up and drifts from the register.
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def rate_for(location_slug: str) -> Decimal:
    known = SALES_TAX.get(location_slug)
    return known[0] if known else max(r for r, _, _ in SALES_TAX.values())


def quote(subtotal, location_slug: str) -> dict:
    """What to show for a cart whose lines are menu prices.

    `total` is the menu total, unchanged and exact — that is what the customer pays.
    `tax_included` is an ESTIMATE of how much of it is tax, for the "all taxes
    included" line. Callers must present it as approximate; it is never used to
    compute the total.
    """
    total = _money(subtotal)
    combined = EXCISE_RATE + rate_for(location_slug)

    # total = base + base*combined  ->  base = total / (1 + combined)
    base = _money(total / (Decimal("1") + combined))

    return {
        "total": float(total),          # exact: the menu price, what they pay
        "pre_tax": float(base),         # estimate
        "tax_included": float(_money(total - base)),  # estimate
        "tax_is_estimate": True,
        "sales_tax_rate": float(rate_for(location_slug)),
    }


if __name__ == "__main__":  # ponytail: one runnable check, no framework
    # The property that matters: the total is the menu total, untouched.
    for shelf in (0, 15, 67, 95.5):
        assert quote(shelf, "yakima")["total"] == float(_money(shelf)), shelf

    q = quote(67, "yakima")
    # Against the captured Dutchie cart: it reported $20.95 of tax on this $67 order.
    assert abs(q["tax_included"] - 20.95) < 0.10, q
    assert q["pre_tax"] + q["tax_included"] == q["total"], q
    # The old bug this replaces: tax must never be ADDED to the menu price.
    assert q["total"] == 67.00, q
    print("ok")
