"""Cart-aware cross-sell for the POS menu.

This was a forked copy of happytime's pairing; it is now a thin re-export of the
shared ``budtender.engine`` so the menu cross-sell and the website upsell choose
add-ons with the same brain (complement ladder + the customer's own co-purchase
recency + taste affinity + price-fit + margin). Keeps ``pos.pairing.pair_for``.
"""
from budtender.engine import pair_items as pair_for  # noqa: F401
