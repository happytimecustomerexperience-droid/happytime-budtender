"""Signed bundle URLs.

The landing page tells a budtender "this customer gets 30% off". Unsigned, anyone
could hand-edit the query string to claim a 30% bundle on arbitrary items — the URL
IS the coupon. So every emailed link carries an HMAC over its own parameters, and
the page refuses to honour a bundle whose signature does not verify.

Canonical form (what gets signed): every param except `sig`, sorted by name, each
occurrence rendered as `k=v` and joined with `&`. Repeated `i` values sort among
themselves, so param order in the URL never changes the signature.

    b=roll-relax&c=a3f9…&exp=1755302400&i=3483543:1&i=3554685:2&loc=yakima

Both sides of the wire must share `BUNDLE_URL_SECRET`: `alpine-automations` signs,
this app verifies.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.conf import settings

# Params that participate in the signature. Anything else on the URL (utm_*, AlpineIQ
# click-tracking, etc.) is deliberately excluded so a marketing tool appending its own
# tracking params can never invalidate a live link.
SIGNED_PARAMS = ("b", "loc", "i", "c", "exp")

MAX_ITEMS = 12          # a bundle is 3-5 slots; well past that is someone probing
MAX_QTY = 12


class BundleUrlError(Exception):
    """URL is malformed, unsigned, forged, or expired."""


def _secret() -> bytes:
    raw = getattr(settings, "BUNDLE_URL_SECRET", "") or ""
    if not raw:
        raise BundleUrlError("BUNDLE_URL_SECRET is not configured")
    return raw.encode()


def canonical(params: dict) -> str:
    """Deterministic string for `params`. Values may be str or list[str]."""
    parts: list[str] = []
    for key in sorted(params):
        if key == "sig" or key not in SIGNED_PARAMS:
            continue
        value = params[key]
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in sorted(str(x) for x in values):
            parts.append(f"{key}={v}")
    return "&".join(parts)


def sign(params: dict) -> str:
    return hmac.new(_secret(), canonical(params).encode(), hashlib.sha256).hexdigest()


def build_url(base: str, *, bundle: str, store: str, items: list[tuple[str, int]],
              customer_token: str = "", ttl_days: int = 14, now: int | None = None) -> str:
    """Build a fully signed bundle URL. Used by the URL-builder command and tests."""
    exp = int(now if now is not None else time.time()) + ttl_days * 86400
    params: dict = {
        "b": bundle,
        "loc": store,
        "i": [f"{sku}:{int(qty)}" for sku, qty in items],
        "exp": str(exp),
    }
    if customer_token:
        params["c"] = customer_token
    params["sig"] = sign(params)
    pairs: list[tuple[str, str]] = []
    for key in ("b", "loc", "i", "c", "exp", "sig"):
        if key not in params:
            continue
        value = params[key]
        if isinstance(value, list):
            pairs.extend((key, v) for v in value)
        else:
            pairs.append((key, str(value)))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(pairs)}"


def customer_token(phone: str) -> str:
    """Opaque, stable handle for a phone number. Never put a raw phone in a URL —
    emailed links get forwarded, logged by mail providers and indexed.

    Normalizes to bare 10-digit US form first: the same person reaches us as
    '+1 (509) 555-1212', '15095551212' and '5095551212' depending on which system
    exported them, and those must produce ONE token or personalization silently
    misses whenever the formats disagree.
    """
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if not digits:
        return ""
    return hmac.new(_secret(), f"cust:{digits}".encode(), hashlib.sha256).hexdigest()[:32]


@dataclass
class BundleRequest:
    """A verified bundle link."""

    bundle: str
    store: str
    items: list[tuple[str, int]] = field(default_factory=list)
    customer_token: str = ""
    expires_at: int = 0
    expired: bool = False

    @property
    def skus(self) -> list[str]:
        return [sku for sku, _ in self.items]


def _parse_items(raw: list[str]) -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    for entry in raw[:MAX_ITEMS]:
        sku, _, qty_raw = str(entry).partition(":")
        sku = sku.strip()[:64]
        if not sku:
            continue
        try:
            qty = int(qty_raw or 1)
        except ValueError:
            qty = 1
        items.append((sku, min(max(qty, 1), MAX_QTY)))
    return items


def parse(query_params, *, now: int | None = None) -> BundleRequest:
    """Verify and parse a request's query params.

    `query_params` is a Django QueryDict (or any mapping exposing `getlist`).
    Raises BundleUrlError on a missing/bad signature. An EXPIRED link is not an
    error — it parses with `expired=True` so the page can still render the items
    and say the offer has ended, rather than showing a dead end.
    """
    def getlist(key):
        if hasattr(query_params, "getlist"):
            return query_params.getlist(key)
        value = query_params.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    params: dict = {}
    for key in SIGNED_PARAMS:
        values = getlist(key)
        if not values:
            continue
        params[key] = values if key == "i" else values[0]

    sig = (getlist("sig") or [""])[0]
    if not sig:
        raise BundleUrlError("missing signature")
    if not hmac.compare_digest(sig, sign(params)):
        raise BundleUrlError("bad signature")

    bundle = str(params.get("b") or "").strip()[:32]
    store = str(params.get("loc") or "").strip()[:32]
    if not bundle or not store:
        raise BundleUrlError("missing bundle or store")

    try:
        exp = int(params.get("exp") or 0)
    except ValueError:
        raise BundleUrlError("bad expiry") from None

    return BundleRequest(
        bundle=bundle,
        store=store,
        items=_parse_items(params.get("i") or []),
        customer_token=str(params.get("c") or "").strip()[:64],
        expires_at=exp,
        expired=bool(exp and int(now if now is not None else time.time()) > exp),
    )
