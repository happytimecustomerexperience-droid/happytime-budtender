"""PosReadClient — read-only Dutchie REST (api.pos.dutchie.com), Basic auth.

Vendored + trimmed from monorepo dutchie_pos_client.py. Kept ONLY what this app
needs (D5): customer search, current inventory, product catalog. HTTP Basic auth =
base64(api_key + ":"). Used to fill the local browse cache + as a fallback customer
lookup; the live cart-add ids come from PosRegisterClient.product_search.
"""

from __future__ import annotations

import base64
import logging

from .transport import http_get

logger = logging.getLogger(__name__)

_BASE = "https://api.pos.dutchie.com"


class PosReadClient:
    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        token = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> object:
        resp = http_get(_BASE + path, headers=self._headers(), params=params, timeout=self.timeout)
        if resp.status_code >= 400:
            logger.warning("Dutchie REST %s -> HTTP %s: %s", path, resp.status_code,
                           getattr(resp, "text", "")[:200])
            return []
        try:
            return resp.json()
        except Exception:
            return []

    # ── the 3 reads we keep ──────────────────────────────────────────────────
    def search_customers(self, phone: str = "", name: str = "") -> list[dict]:
        """Customer lookup. Dutchie REST exposes /customer/customers (paged).

        We filter client-side on phone/name — the public REST customer endpoint has
        no documented phone query param. For live floor lookup prefer the POS
        guest_search; this is the catalog fallback.
        """
        rows = self._get("/customer/customers", {"fromLastModifiedDateUTC": "2000-01-01"})
        if not isinstance(rows, list):
            return []
        phone_d = "".join(c for c in (phone or "") if c.isdigit())
        name_l = (name or "").strip().lower()
        out = []
        for r in rows:
            r_phone = "".join(c for c in str(r.get("phone") or "") if c.isdigit())
            r_name = f"{r.get('firstName','')} {r.get('lastName','')}".strip().lower()
            if phone_d and phone_d in r_phone:
                out.append(r)
            elif name_l and name_l in r_name:
                out.append(r)
        return out

    def inventory(self) -> list[dict]:
        """Current inventory snapshot for the browse cache."""
        rows = self._get("/inventory")
        return rows if isinstance(rows, list) else []

    def products(self) -> list[dict]:
        """Product catalog (productId, name, category, price, cost)."""
        rows = self._get("/products")
        return rows if isinstance(rows, list) else []
