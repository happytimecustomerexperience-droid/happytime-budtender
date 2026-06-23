"""The thin Bearer HTTP client to the happytime-budtender microservice (11-P1 §3.1; 21-SPEC §9).

The ONLY seam between the voice repo and budtender (ADR-004). Per-method (one method per endpoint
P1 needs), Bearer-authed, pooled (keep-alive — no fresh TLS handshake per voice turn, 21-SPEC §8.3),
timeout-bounded, fail-graceful. It holds NO Dutchie key and NO ranking/pairing logic — it forwards
slots and returns budtender's already-leak-safe JSON (``serializers.public_product`` allowlist).

Cross-cutting invariants (binding, 21-SPEC §9):
  * The Bearer header is attached HERE only, redacted in every log line (never the raw token).
  * Graceful-empty on EVERY method: a connect/read timeout or non-2xx returns the method's typed
    empty result + a logged warning — NEVER raises into the voice turn (21-SPEC §8.2).
  * Fail-closed: an empty ``HHT_BACKEND_TOKEN`` → no request is issued (mirrors budtender's own
    ``auth.ServiceTokenPermission`` fail-closed posture) → typed-empty.
  * No re-ranking, no Dutchie, no margin math (the client is pure transport).
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# The leak-safe allowlist budtender serializes (serializers.PUBLIC_PRODUCT_FIELDS). The client
# does not enforce it (budtender already does), but it documents the only fields that arrive.
_API_PREFIX = "/api/v1"


class BudtenderClient:
    """A pooled Bearer client to happytime-budtender. Constructed once (module singleton
    ``budtender()``); reuse the session across turns (keep-alive)."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: int | None = None,
    ):
        self.base_url = (
            base_url if base_url is not None else _setting("HHT_BUDTENDER_BASE_URL")
        ).rstrip("/")
        self._token = token if token is not None else _setting("HHT_BACKEND_TOKEN")
        self.timeout = (
            timeout if timeout is not None else int(_setting("HHT_BUDTENDER_TIMEOUT", 8) or 8)
        )
        # Pooled session (keep-alive). A connect timeout of ~2s + the read timeout bounds the turn.
        self._session = requests.Session()
        self._connect_timeout = 2.0

    # ── headers + HTTP primitives ─────────────────────────────────────────────
    def _headers(self) -> dict:
        """Bearer + JSON headers. The token is read once; NEVER logged."""
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "happytime-voice/0.1",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{_API_PREFIX}{path}"

    def _post(self, path: str, payload: dict, *, empty, require_token: bool = True):
        """POST JSON; on any failure return ``empty`` (typed graceful-empty), never raise."""
        if require_token and not self._token:
            logger.warning("budtender token not configured; skipping POST %s", path)
            return empty
        if not self.base_url:
            logger.warning("budtender base url not configured; skipping POST %s", path)
            return empty
        try:
            resp = self._session.post(
                self._url(path),
                json=payload,
                headers=self._headers(),
                timeout=(self._connect_timeout, self.timeout),
            )
            if resp.status_code >= 300:
                logger.warning("budtender POST %s → HTTP %s", path, resp.status_code)
                return empty
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            logger.warning("budtender POST %s unreachable: %s", path, type(exc).__name__)
            return empty
        except Exception:  # noqa: BLE001 — a transport/parse error must not crash the turn
            logger.warning("budtender POST %s failed", path, exc_info=True)
            return empty

    def _get(self, path: str, params: dict | None = None, *, empty, require_token: bool = True):
        """GET; on any failure return ``empty``, never raise. ``/health/`` passes
        ``require_token=False`` (the open probe carries no Bearer)."""
        if require_token and not self._token:
            logger.warning("budtender token not configured; skipping GET %s", path)
            return empty
        if not self.base_url:
            logger.warning("budtender base url not configured; skipping GET %s", path)
            return empty
        try:
            headers = self._headers() if require_token else {"Accept": "application/json"}
            resp = self._session.get(
                self._url(path),
                params=params or {},
                headers=headers,
                timeout=(self._connect_timeout, self.timeout),
            )
            if resp.status_code >= 300:
                logger.warning("budtender GET %s → HTTP %s", path, resp.status_code)
                return empty
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            logger.warning("budtender GET %s unreachable: %s", path, type(exc).__name__)
            return empty
        except Exception:  # noqa: BLE001
            logger.warning("budtender GET %s failed", path, exc_info=True)
            return empty

    # ── health (open, no token) ────────────────────────────────────────────────
    def health(self) -> bool:
        """``GET /health/`` (open). True on ``{"status":"ok"}``; False when unreachable."""
        if not self.base_url:
            return False
        out = self._get("/health/", empty={}, require_token=False)
        return bool(out) and out.get("status") == "ok"

    # ── suggestions (the data plane) ───────────────────────────────────────────
    def search(
        self,
        slots: dict,
        *,
        limit: int = 3,
        phone: str | None = None,
        session_token: str | None = None,
        exclude_skus: list[str] | None = None,
        location: str | None = None,
    ) -> dict:
        """``POST /products/search/`` (trailing slash). Returns ``{"results":[…≤limit leak-safe…]}``
        verbatim; graceful-empty = ``{"results": []}``.

        The margin-vs-taste switch is the PRESENCE of ``phone`` (21-SPEC §6): a KNOWN caller's
        normalized number is sent → budtender resolves a profile → ``W_KNOWN`` (taste-first); an
        anonymous caller sends no ``phone`` → ``W_ANON`` (margin-first). Budtender owns the
        re-ranking; the client only sends/omits the identity.

        P4 ranking-weights lever (14-P4 item 1): the owner-tuned ``RankingWeights`` singleton
        (dashboard) is forwarded as ``ranking_weights`` on EVERY suggestion request so the owner's
        "high margin first" / taste levers reach the ranker per call. Omitted when the owner hasn't
        changed anything off budtender's baseline (zero behavior change until a lever is tuned).
        TODO-BUDTENDER: budtender must read a ``ranking_weights`` request param in
        ``products/search/`` (``ranking.score`` currently reads its module-level ``W_ANON``/
        ``W_KNOWN``); until it does, this param is sent + ignored harmlessly (no error)."""
        loc = location or slots.get("store") or "yakima"
        payload: dict = {"slots": slots, "limit": limit, "location": loc}
        if phone:
            payload["phone"] = phone
        if session_token:
            payload["session_token"] = session_token
        if exclude_skus:
            payload["exclude_skus"] = list(exclude_skus)
        ranking = _ranking_config()
        if ranking:
            payload["ranking_weights"] = ranking
        out = self._post("/products/search/", payload, empty={"results": []})
        if not isinstance(out, dict) or "results" not in out:
            return {"results": []}
        return out

    def check_sku(self, store: str, sku: str, *, category: str | None = None) -> dict:
        """SKU-scoped purchasability + OTD price (21-SPEC §5.3) via the single-SKU budtender
        endpoint ``GET /products/by-sku/`` (resolved TODO-B3). The old capped-ranked-search
        workaround missed specific SKUs (a SKU ranked below the limit looked out-of-stock); this is
        an exact, reliable lookup. Budtender returns a row ONLY when in stock (MIN_STOCK + the
        purchasable gate), so a returned product IS buyable. Returns
        ``{in_stock, sku, price_otd, stock_on_hand, name}``; graceful-empty = ``{"in_stock": False}``.
        ``price_otd`` is computed via ``pricing.otd`` — the raw pre-tax ``price`` is NEVER surfaced
        for speaking (ADR-009). ``category`` is accepted for back-compat but no longer needed."""
        from voice import pricing

        target = str(sku)
        out = self._get("/products/by-sku/", {"store": store, "sku": target}, empty={})
        prod = out.get("product") if isinstance(out, dict) else None
        if isinstance(prod, dict) and str(prod.get("sku")) == target:
            return {
                "in_stock": True,
                "sku": target,
                "price_otd": pricing.otd(prod.get("price"), store),
                "stock_on_hand": prod.get("stock_on_hand"),
                "name": prod.get("name"),
            }
        return {"in_stock": False}

    def pair_for_sku(
        self,
        store: str,
        anchor_sku: str,
        *,
        phone: str | None = None,
        session_token: str | None = None,
    ) -> dict:
        """``POST /pairing/for-sku`` (NO trailing slash). Returns
        ``{pairing, reason_code, reason_text, strength}`` verbatim; graceful-empty =
        ``{"pairing": None, "reason_code": "none", "reason_text": "", "strength": 0.0}``."""
        empty = {"pairing": None, "reason_code": "none", "reason_text": "", "strength": 0.0}
        payload: dict = {"location": store, "sku": str(anchor_sku)}
        if phone:
            payload["phone"] = phone
        if session_token:
            payload["session_token"] = session_token
        out = self._post("/pairing/for-sku", payload, empty=empty)
        if not isinstance(out, dict):
            return dict(empty)
        out.setdefault("pairing", None)
        out.setdefault("strength", 0.0)
        out.setdefault("reason_text", "")
        out.setdefault("reason_code", "none")
        return out

    # ── returning-caller handshake (§7) ───────────────────────────────────────
    def resume_by_phone(
        self,
        phone_e164: str,
        *,
        location: str | None = None,
        current_session_token: str | None = None,
    ) -> dict:
        """``POST /chat/resume-by-phone``. Sends the E.164 normalized RAW phone (the key budtender
        resolves a profile by today — 21-SPEC §7.1 / ADR-022 Option A); the voice repo persists
        ONLY the peppered hash in its own DB. Returns ``{resumed, session_token, profile_summary}``;
        the only field the flow needs is ``profile_summary.has_history``. Graceful-miss never
        raises → ``{"session_token": None, "profile_summary": {"has_history": False, …}}``."""
        empty = {
            "resumed": False,
            "session_token": current_session_token,
            "profile_summary": {"has_history": False, "top_categories": [], "price_tier": ""},
        }
        if not phone_e164:
            return dict(empty)
        payload: dict = {"phone": phone_e164}
        if location:
            payload["location"] = location
        if current_session_token:
            payload["current_session_token"] = current_session_token
        out = self._post("/chat/resume-by-phone", payload, empty=empty)
        if not isinstance(out, dict):
            return dict(empty)
        summary = out.get("profile_summary")
        if not isinstance(summary, dict):
            out["profile_summary"] = {"has_history": False, "top_categories": [], "price_tier": ""}
        return out

    def persist_session(
        self,
        session_token: str,
        *,
        slots: dict | None = None,
        stage: str | None = None,
        phone: str | None = None,
        messages: list | None = None,
    ) -> dict:
        """``POST /chat/persist/`` (202). Soft-failure on any non-2xx (log + continue)."""
        payload: dict = {"session_token": session_token}
        if slots is not None:
            payload["slots"] = slots
        if stage is not None:
            payload["stage"] = stage
        if phone:
            payload["phone"] = phone
        if messages is not None:
            payload["messages"] = messages
        return self._post("/chat/persist/", payload, empty={"ok": False})

    # ── facets (slot prep) ─────────────────────────────────────────────────────
    def facets_subtypes(self, store: str, category: str) -> list[str]:
        out = self._post(
            "/products/subtypes", {"slots": {"store": store, "category": category}}, empty={}
        )
        return out.get("subtypes", []) if isinstance(out, dict) else []

    def facets_sizes(self, store: str, category: str, subcategory: str | None = None) -> list[str]:
        slots: dict = {"store": store, "category": category}
        if subcategory:
            slots["subcategory"] = subcategory
        out = self._post("/products/sizes", {"slots": slots}, empty={})
        return out.get("sizes", []) if isinstance(out, dict) else []

    def facets_price_bands(
        self, store: str, category: str, size: str | None = None, subcategory: str | None = None
    ) -> list[dict]:
        slots: dict = {"store": store, "category": category}
        if size:
            slots["size"] = size
        if subcategory:
            slots["subcategory"] = subcategory
        out = self._post("/products/price-bands", {"slots": slots}, empty=[])
        return out if isinstance(out, list) else []

    def facets_doh(self, store: str, category: str, **filters) -> dict:
        slots: dict = {"store": store, "category": category, **filters}
        out = self._post("/products/doh-options", {"slots": slots}, empty={})
        return out if isinstance(out, dict) else {}


# ── owner ranking-weights lever (14-P4 item 1) ──────────────────────────────────
def _ranking_config() -> dict | None:
    """Read the owner-tuned ``RankingWeights`` singleton → the per-request ``ranking_weights``
    config, or ``None`` to OMIT it (owner hasn't tuned anything → budtender uses its own defaults).

    Fail-safe: a DB error / un-migrated table / missing app must NEVER crash a voice turn — any
    failure returns ``None`` (budtender falls back to its baseline). The dashboard app holds the
    singleton; imported lazily so ``voice`` never hard-depends on ``dashboard`` at module load."""
    try:
        from dashboard.models import RankingWeights

        weights = RankingWeights.load()
        if weights.is_default():
            return None
        return weights.as_request_config()
    except Exception:  # noqa: BLE001 — a weights read must never break a suggestion turn
        logger.warning("ranking-weights read failed; using budtender defaults", exc_info=True)
        return None


# ── module singleton ───────────────────────────────────────────────────────────
def _setting(name: str, default=""):
    return getattr(settings, name, default)


_CLIENT: BudtenderClient | None = None


def budtender() -> BudtenderClient:
    """The process-wide pooled client (keep-alive). Built lazily so settings/env are read once
    the app is configured (not at import)."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = BudtenderClient()
    return _CLIENT


def reset_client() -> None:
    """Test seam: drop the singleton so a fixture can rebuild it with patched settings."""
    global _CLIENT
    _CLIENT = None
