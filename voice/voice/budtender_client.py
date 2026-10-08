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

    def _post(
        self, path: str, payload: dict, *, empty, require_token: bool = True, budget: float | None = None
    ):
        """POST JSON; on any failure return ``empty`` (typed graceful-empty), never raise.
        ``budget`` caps connect + read together at that many seconds (a slow path that must answer
        inside a hard deadline); the default is the client's own connect + read timeouts."""
        if require_token and not self._token:
            logger.warning("budtender token not configured; skipping POST %s", path)
            return empty
        if not self.base_url:
            logger.warning("budtender base url not configured; skipping POST %s", path)
            return empty
        if budget:
            connect = min(self._connect_timeout, budget / 2)
            timeout = (connect, budget - connect)
        else:
            timeout = (self._connect_timeout, self.timeout)
        try:
            resp = self._session.post(
                self._url(path),
                json=payload,
                headers=self._headers(),
                timeout=timeout,
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

    def _get(
        self,
        path: str,
        params: dict | None = None,
        *,
        empty,
        require_token: bool = True,
        read_timeout: float | None = None,
    ):
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
                timeout=(self._connect_timeout, read_timeout or self.timeout),
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
        changed anything off budtender's baseline (zero behavior change until a lever is tuned)."""
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
                # public_product's lab report + exact menu slug; suggest.check_inventory validates.
                "coa_url": prod.get("coa_url"),
                "menu_slug": prod.get("menu_slug"),
                # ...and the potency / lab / product facts the same row carries. Passed through
                # untouched: suggest._facts is the one place that validates and shapes them.
                "thc_percent": prod.get("thc_percent"),
                "size": prod.get("size"),
                "lab": prod.get("lab"),
                "info": prod.get("info"),
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

    def deals(self) -> dict:
        """``GET /deals/`` — every deal running today on each store's Dutchie online menu:
        ``{ok, stores: {slug: [deal…] | None}, errors, fetched_at}``. A store whose Dutchie feed was
        unreachable is ``None`` (unknown), never ``[]`` (none). Graceful-empty = ``{"stores": {}}``,
        which reads as every store unreachable. A cold fetch reads six Dutchie feeds (~6 s), so the
        read timeout is longer than a voice turn's."""
        out = self._get("/deals/", empty={}, read_timeout=45)
        if not isinstance(out, dict) or not isinstance(out.get("stores"), dict):
            return {"ok": False, "stores": {}, "errors": {}}
        return out

    # ── staff customer browse (P7) — the dashboard reads the LIVE, auto-recomputed profiles ──
    def phone_cart_upsert(self, payload: dict) -> dict:
        """``POST /phone-cart/upsert``. Stages cart intent only; never submits Dutchie."""
        empty = {"ok": False, "error": "budtender_unavailable"}
        out = self._post("/phone-cart/upsert", payload or {}, empty=empty)
        return out if isinstance(out, dict) else dict(empty)

    def phone_cart_release(self, payload: dict) -> dict:
        """``POST /phone-cart/release``. Marks a draft released at hangup."""
        empty = {"ok": False, "error": "budtender_unavailable"}
        out = self._post("/phone-cart/release", payload or {}, empty=empty)
        return out if isinstance(out, dict) else dict(empty)

    def phone_cart_claim(self, payload: dict) -> dict:
        """``POST /phone-cart/claim`` for server-side POS handoff tests/admin flows."""
        empty = {"ok": False, "error": "budtender_unavailable"}
        out = self._post("/phone-cart/claim", payload or {}, empty=empty)
        return out if isinstance(out, dict) else dict(empty)

    def list_customers(self, *, q: str = "", limit: int = 25, offset: int = 0) -> dict:
        """``POST /customer/list`` — the staff roster for the dashboard Customers page. Returns
        ``{ok, total, count, offset, limit, customers:[…leak-safe rows…]}``; graceful-empty (budtender
        unreachable / token unset) = ``{ok: False, customers: [], total: 0}`` so the dashboard can
        fall back to its local snapshot."""
        empty = {"ok": False, "customers": [], "total": 0, "offset": offset, "limit": limit}
        out = self._post(
            "/customer/list", {"q": q or "", "limit": limit, "offset": offset}, empty=empty
        )
        if not isinstance(out, dict):
            return dict(empty)
        out.setdefault("ok", True)
        out.setdefault("customers", [])
        out.setdefault("total", len(out["customers"]))
        return out

    def get_customer(self, *, customer_id=None, phone: str | None = None,
                     name: str | None = None) -> dict | None:
        """``POST /customer/detail`` — one full profile by opaque id (preferred), name (for the
        dashboard's analytics→live enrichment), or phone. Returns the leak-safe ``customer`` dict,
        or ``None`` when missing/unreachable."""
        if customer_id in (None, "") and not name and not phone:
            return None
        payload: dict = {}
        if customer_id not in (None, ""):
            payload["id"] = customer_id
        elif name:
            payload["name"] = name
        elif phone:
            payload["phone"] = phone
        out = self._post("/customer/detail", payload, empty={})
        if isinstance(out, dict) and out.get("ok") and isinstance(out.get("customer"), dict):
            return out["customer"]
        return None

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

    def caller_context(
        self,
        phone_e164: str,
        *,
        store: str | None = None,
        session_token: str | None = None,
        timeout: float = 2.5,
    ) -> dict:
        """``POST /customer/caller-context`` (NO trailing slash): who is calling, DB-only. Creates a
        "voice" profile for a number budtender has never seen. Returns ``{ok, created, known,
        first_name, has_history, orders, days_since_last, top_categories, price_tier, brands,
        flavors, terpenes}`` (no phone, no cost/margin) plus, once the memory contract ships,
        ``brief`` (<= 600 chars of plain text), ``style`` (small enum dict) and ``tier`` (``"trusted"``
        for a carrier-caller-ID caller); older budtenders omit them and ``voice.caller`` reads that as
        "no memory". ``timeout`` caps connect + read together
        (the assistant-request path answers inside Vapi's fixed 7.5 s, so it is not the client's
        8 s default); it is a request budget, not a wall-clock kill. ``{}`` on ANY failure —
        unknown, never "a new caller"."""
        if not phone_e164:
            return {}
        payload: dict = {"phone": phone_e164}
        if store:
            payload["store"] = store
        if session_token:
            payload["session_token"] = session_token
        out = self._post("/customer/caller-context", payload, empty={}, budget=timeout)
        return out if isinstance(out, dict) and out.get("ok") else {}

    def memory_learn(self, call_id: str, user_turns: list, *, channel: str = "voice", timeout: float = 6.0) -> dict:
        """``POST /customer/memory/learn`` (NO trailing slash, backend token): hand budtender the
        customer's OWN turns of a finished call so it can fold them into the shared customer memory
        (contract customer-memory-v1). Body is exactly ``{call_id, transcript_user_turns, channel}``:
        at most 40 turns of 500 characters, no phone number (budtender resolves the caller from the
        call id it linked at caller-context time). Nothing is sent without a call id or a turn.
        ``{}`` on ANY failure; never raises (best-effort, post-call)."""
        turns = [t[:500] for t in (str(x).strip() for x in (user_turns or [])) if t][-40:]
        if not call_id or not turns:
            return {}
        payload = {"call_id": str(call_id), "transcript_user_turns": turns, "channel": channel or "voice"}
        out = self._post("/customer/memory/learn", payload, empty={}, budget=timeout)
        return out if isinstance(out, dict) else {}

    def profile_upsert(self, phone_e164: str, *, name: str = "", source: str = "voice") -> dict:
        """``POST /customer/profile-upsert``: create the profile for a phone budtender has not seen
        and remember a first name (stored only when the row has none; never written to Dutchie).
        Returns ``{status, created, first_name, profile_summary}``; ``{}`` on any failure."""
        if not phone_e164:
            return {}
        payload: dict = {"phone": phone_e164, "source": source}
        if name:
            payload["name"] = name
        out = self._post("/customer/profile-upsert", payload, empty={})
        return out if isinstance(out, dict) else {}

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
    def chat_message(
        self,
        message: str,
        *,
        session_token: str | None = None,
        location: str | None = None,
        channel: str = "chat",
        phone: str | None = None,
    ) -> dict:
        """``POST /chat/message``: persist a shopper turn and get the Gemini-backed reply.

        This is for server-side website/proxy callers. It reuses the same pooled Bearer seam as
        product tools, so the browser never gets the backend token.
        """
        text = str(message or "").strip()
        if not text:
            return {"ok": False, "message": None, "source": "empty"}
        payload: dict = {"message": text, "channel": channel or "chat"}
        if session_token:
            payload["session_token"] = session_token
        if location:
            payload["location"] = location
        if phone:
            payload["phone"] = phone
        out = self._post(
            "/chat/message",
            payload,
            empty={"ok": False, "message": None, "source": "unavailable"},
        )
        return out if isinstance(out, dict) else {"ok": False, "message": None, "source": "unavailable"}

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


# ── root-notify nudge (P6 instant-refresh chain, kb/signals.py) ─────────────────
def _notify(path: str) -> bool:
    """Best-effort ``POST {base_url}/api/v1{path}`` telling root a KB row changed so its own
    caches refresh instantly. NEVER raises into the caller's save — any failure is a logged
    warning. Off when ``HHT_NOTIFY_BUDTENDER`` is false (same gating shape as ``HHT_AUTO_PUBLISH``
    — off under pytest unless a test opts in) or the base url is unset."""
    if not _setting("HHT_NOTIFY_BUDTENDER", False):
        return False
    base_url = _setting("HHT_BUDTENDER_BASE_URL")
    if not base_url:
        return False
    token = _setting("HHT_BACKEND_TOKEN")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}{_API_PREFIX}{path}", headers=headers, timeout=(2, 5)
        )
        if resp.status_code >= 300:
            logger.warning("budtender notify %s -> HTTP %s", path, resp.status_code)
            return False
        return True
    except (requests.Timeout, requests.ConnectionError) as exc:
        logger.warning("budtender notify %s unreachable: %s", path, type(exc).__name__)
        return False
    except Exception:  # noqa: BLE001 — a notify failure must never break the save
        logger.warning("budtender notify %s failed", path, exc_info=True)
        return False


def notify_persona_refresh() -> bool:
    """Nudge root's ``POST /api/v1/persona/refresh`` after an ``AgentPrompt`` save."""
    return _notify("/persona/refresh")


def notify_store_facts_refresh() -> bool:
    """Nudge root's ``POST /api/v1/store-facts/refresh`` after a ``StoreFact`` save/delete."""
    return _notify("/store-facts/refresh")


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
