"""
Pairing/upsell selection — ONE complementary, in-stock, high-margin add-on.

The implementation now lives in the shared ``engine`` module so the website,
voice and the in-store POS all choose add-ons with the exact same logic
(complement ladder + the customer's own co-purchase recency + taste affinity +
price-fit + margin, plus the nightly Redis co-purchase matrices). This module
keeps the historical ``from .pairing import pair_for / pair_attr_key`` paths
stable for ``views`` and ``tasks``.
"""
from .engine import pair_attr_key, pair_for  # noqa: F401
