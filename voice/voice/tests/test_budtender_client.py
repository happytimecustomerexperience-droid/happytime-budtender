"""voice/budtender_client.py — the thin Bearer HTTP client (11-P1 §3.1; 21-SPEC §9/§11).

The HTTP layer is replaced by an in-memory ``FakeSession`` (record/replay) — NO network, NO live
key, deterministic + free. Asserts: each method's URL/headers/body; the Bearer header attached on
every non-health request; timeout/connection-error → graceful-empty (never raised); the token never
appears in a log line (A1/A2/A3, B1/B2, C1/C2 of 21-SPEC §11).
"""

from __future__ import annotations

import logging

import requests

from voice.budtender_client import BudtenderClient

BASE = "https://budtender.test"
TOKEN = "test-backend-token-xyz"


# ── a tiny fake requests.Session (record/replay, no network) ────────────────────
class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeSession:
    """Records every call; returns a queued response or a default. Can be told to raise a
    Timeout/ConnectionError to exercise the graceful-empty path."""

    def __init__(self, default=None, raise_exc=None):
        self.calls: list[dict] = []
        self.default = default if default is not None else {"results": []}
        self.raise_exc = raise_exc
        self.queued = {}  # path-suffix -> payload

    def queue(self, path_suffix, payload, status=200):
        self.queued[path_suffix] = (payload, status)

    def _record(self, method, url, headers, body, params):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "body": body, "params": params}
        )

    def _resp(self, url):
        for suffix, (payload, status) in self.queued.items():
            if url.endswith(suffix):
                return FakeResp(payload, status)
        return FakeResp(self.default)

    def post(self, url, *, json=None, headers=None, timeout=None):
        self._record("POST", url, headers, json, None)
        if self.raise_exc:
            raise self.raise_exc
        return self._resp(url)

    def get(self, url, *, params=None, headers=None, timeout=None):
        self._record("GET", url, headers, None, params)
        if self.raise_exc:
            raise self.raise_exc
        return self._resp(url)


def _client(session=None, token=TOKEN, base=BASE):
    c = BudtenderClient(base_url=base, token=token, timeout=4)
    if session is not None:
        c._session = session
    return c


# ── A. auth + reachability ──────────────────────────────────────────────────────
def test_search_attaches_bearer_and_headers():
    fs = FakeSession(default={"results": []})
    c = _client(fs)
    c.search({"store": "yakima", "category": "flower"})
    call = fs.calls[0]
    assert call["url"] == f"{BASE}/api/v1/products/search/"  # trailing slash load-bearing
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["Accept"] == "application/json"
    assert call["headers"]["User-Agent"] == "happytime-voice/0.1"


def test_pairing_path_has_no_trailing_slash():
    fs = FakeSession(default={"pairing": None, "strength": 0.0})
    c = _client(fs)
    c.pair_for_sku("yakima", "SKU1")
    assert fs.calls[0]["url"] == f"{BASE}/api/v1/pairing/for-sku"  # NO trailing slash


def test_health_carries_no_token():
    fs = FakeSession()
    fs.queue("/health/", {"status": "ok"})
    c = _client(fs)
    assert c.health() is True
    assert "Authorization" not in fs.calls[0]["headers"]  # open probe, no Bearer


def test_health_false_when_unreachable():
    fs = FakeSession(raise_exc=requests.ConnectionError("down"))
    c = _client(fs)
    assert c.health() is False


# ── A2. empty token → no request issued (fail-closed) ───────────────────────────
def test_empty_token_skips_request_returns_empty():
    fs = FakeSession()
    c = _client(fs, token="")
    out = c.search({"store": "yakima", "category": "flower"})
    assert out == {"results": []}
    assert fs.calls == []  # no request issued


def test_empty_base_url_skips_request():
    fs = FakeSession()
    c = _client(fs, base="")
    out = c.pair_for_sku("yakima", "SKU1")
    assert out["pairing"] is None
    assert fs.calls == []


# ── A3. timeout / connection error → typed graceful-empty, never raised ─────────
def test_timeout_returns_graceful_empty_search():
    fs = FakeSession(raise_exc=requests.Timeout("slow"))
    out = _client(fs).search({"store": "yakima", "category": "flower"})
    assert out == {"results": []}


def test_connection_error_returns_graceful_empty_pairing():
    fs = FakeSession(raise_exc=requests.ConnectionError("down"))
    out = _client(fs).pair_for_sku("yakima", "SKU1")
    assert out == {"pairing": None, "reason_code": "none", "reason_text": "", "strength": 0.0}


def test_non_2xx_returns_graceful_empty():
    fs = FakeSession()
    fs.queue("/products/search/", {"error": "boom"}, status=500)
    out = _client(fs).search({"store": "yakima", "category": "flower"})
    assert out == {"results": []}


def test_resume_by_phone_graceful_miss():
    fs = FakeSession(raise_exc=requests.Timeout("slow"))
    out = _client(fs).resume_by_phone("+15095551234")
    assert out["profile_summary"]["has_history"] is False
    assert out["session_token"] is None


# ── B. selection switch (margin vs taste) — request body presence of phone ──────
def test_search_omits_phone_when_anonymous():
    fs = FakeSession(default={"results": []})
    _client(fs).search({"store": "yakima", "category": "flower"}, limit=3)
    assert "phone" not in fs.calls[0]["body"]  # anonymous → W_ANON (margin-first)
    assert fs.calls[0]["body"]["limit"] == 3


def test_search_includes_phone_when_known():
    fs = FakeSession(default={"results": []})
    _client(fs).search({"store": "yakima", "category": "flower"}, phone="+15095551234")
    assert fs.calls[0]["body"]["phone"] == "+15095551234"  # known → W_KNOWN (taste-first)


def test_search_forwards_exclude_skus():
    fs = FakeSession(default={"results": []})
    _client(fs).search({"store": "yakima", "category": "flower"}, exclude_skus=["A", "B"])
    assert fs.calls[0]["body"]["exclude_skus"] == ["A", "B"]


# ── check_sku: search-and-filter, OTD price, never the raw pre-tax price ────────
def test_check_sku_filters_by_sku_and_computes_otd():
    fs = FakeSession()
    fs.queue(
        "/products/search/",
        {"results": [{"sku": "SKU1", "name": "X", "price": 38.0, "stock_on_hand": 14}]},
    )
    out = _client(fs).check_sku("yakima", "SKU1")
    assert out["in_stock"] is True
    assert out["sku"] == "SKU1"
    assert out["price_otd"] == 56.43  # 38 * 1.48508 (Yakima OTD)
    assert "price" not in out  # the raw pre-tax price never surfaces


def test_check_sku_absent_is_not_in_stock():
    fs = FakeSession(default={"results": []})
    out = _client(fs).check_sku("yakima", "NOPE")
    assert out == {"in_stock": False}


# ── facets graceful shapes ──────────────────────────────────────────────────────
def test_facets_subtypes_returns_list():
    fs = FakeSession()
    fs.queue("/products/subtypes", {"subtypes": ["rosin", "live resin"]})
    assert _client(fs).facets_subtypes("yakima", "concentrate") == ["rosin", "live resin"]


def test_facets_empty_on_failure():
    fs = FakeSession(raise_exc=requests.Timeout("slow"))
    assert _client(fs).facets_sizes("yakima", "flower") == []


# ── H2/H3. the token never appears in a log line ────────────────────────────────
def test_token_not_logged_on_failure(caplog):
    fs = FakeSession(raise_exc=requests.Timeout("slow"))
    with caplog.at_level(logging.WARNING):
        _client(fs).search({"store": "yakima", "category": "flower"})
    assert TOKEN not in caplog.text
    assert "Bearer" not in caplog.text or TOKEN not in caplog.text


def test_pooled_session_reused_across_calls():
    """G1: the client uses ONE pooled session object across calls (keep-alive)."""
    c = _client()
    s1 = c._session
    s2 = c._session
    assert s1 is s2
    assert isinstance(c._session, requests.Session)
