"""The REAL `PosClient.post` through the REAL `new_drops.BackofficeClient` (no `PosClient.post` mock).

Round 2 added retry kwargs to `PosClient.post` that re-call themselves, but `BackofficeClient.post`
declared a fixed signature, so every 401/403 re-login and every 429/transport retry raised TypeError
(New Drops included). These tests script only the HTTP layer and the login, and run the whole call.
"""
import pytest

from budtender import new_drops
from dutchie import session as sess
from dutchie.session import (DutchieSessionExpired, DutchieThrottled, DutchieUnavailable, PosClient, Store)

STORE = Store(name="real-post-test", base_url="https://bo.example", pos_base_url="https://pos.example",
              org_id=1, lsp_id=2, loc_id=3, register_id=4, username="u", password="p")
OK = {"Result": True, "Data": {"x": 1}}


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def rig(monkeypatch):
    PosClient._login_cache.clear()
    new_drops.BackofficeClient._last_call = 0.0

    class Rig:
        script: list
        logins = 0
        requests: list
        sleeps: list

    r = Rig()
    r.script, r.requests, r.sleeps = [], [], []

    def fake_login(*a, **k):
        r.logins += 1
        return ("cookie", f"SID-{r.logins}", 90 + r.logins)

    def fake_post(url, json=None, headers=None, cookies=None, timeout=30):
        r.requests.append({"url": url, "json": json, "cookie": (headers or {}).get("cookie")})
        item = r.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sess, "login_employee", fake_login)
    monkeypatch.setattr(sess, "http_post", fake_post)
    monkeypatch.setattr(sess.time, "sleep", r.sleeps.append)   # the same `time` module new_drops uses
    r.client = new_drops.BackofficeClient(STORE)
    yield r
    PosClient._login_cache.clear()
    new_drops.BackofficeClient._last_call = 0.0


def _body(client):
    return {**client.session_block(), "BatchId": 7}


# ── 401/403: the re-login ────────────────────────────────────────────────────
def test_a_401_relogs_in_once_and_retries_through_the_backoffice_client(rig):
    rig.script = [Resp(401), Resp(200, OK)]
    assert rig.client.post("/api/x", _body(rig.client)) == OK
    assert rig.logins == 2 and len(rig.requests) == 2


def test_the_same_through_an_idempotent_read(rig):
    rig.script = [Resp(403), Resp(200, OK)]
    assert rig.client.post("/api/x", _body(rig.client), idempotent=True) == OK
    assert rig.logins == 2


def test_after_a_relogin_the_retried_body_carries_the_NEW_session_not_the_old_one(rig):
    body = _body(rig.client)                       # built from session_block() BEFORE post: SID-1 / user 91
    assert (body["SessionId"], body["UserId"]) == ("SID-1", "91")
    rig.script = [Resp(401), Resp(200, OK)]
    rig.client.post("/api/x", body)
    first, second = rig.requests
    assert first["json"]["SessionId"] == "SID-1" and first["cookie"] == "cookie"
    assert second["json"]["SessionId"] == "SID-2" and second["json"]["UserId"] == "92"
    assert second["json"]["BatchId"] == 7 and second["json"]["LocId"] == "3"   # everything else untouched
    assert body["SessionId"] == "SID-1"            # the caller's dict is not mutated


def test_a_body_with_no_session_fields_is_sent_as_given(rig):
    rig.script = [Resp(401), Resp(200, OK)]
    rig.client.post("/api/x", {"Anything": 1})
    assert [r["json"] for r in rig.requests] == [{"Anything": 1}, {"Anything": 1}]


def test_a_second_rejection_raises_without_a_third_login(rig):
    rig.script = [Resp(401), Resp(401), Resp(200, OK)]
    with pytest.raises(DutchieSessionExpired):
        rig.client.post("/api/x", _body(rig.client), idempotent=True)
    assert rig.logins == 2


# ── 429 ──────────────────────────────────────────────────────────────────────
def test_a_429_then_success_through_the_backoffice_client(rig):
    rig.script = [Resp(429, headers={"Retry-After": "3"}), Resp(200, OK)]
    assert rig.client.post("/api/x", _body(rig.client), idempotent=True) == OK
    assert 3.0 in rig.sleeps and len(rig.requests) == 2
    assert rig.logins == 1


def test_an_exhausted_429_raises_throttled_and_does_not_wait_again_in_the_outer_wrapper(rig):
    rig.script = [Resp(429, text="Too many requests - only 60 per minute allowed") for _ in range(20)]
    with pytest.raises(DutchieThrottled) as err:
        rig.client.post("/api/x", _body(rig.client), idempotent=True)
    assert err.value.backed_off is True
    assert 61 not in rig.sleeps                                  # the wrapper's own 61 s wait did NOT fire on top
    assert [s for s in rig.sleeps if s >= 2] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert len(rig.requests) == 1 + sess._MAX_THROTTLE_RETRIES


def test_a_non_idempotent_result_false_throttle_keeps_todays_single_61_second_retry(rig):
    rig.script = [Resp(200, {"Result": False, "Message": "Too many requests - only 60 per minute allowed"}),
                  Resp(200, OK)]
    assert rig.client.post("/api/x", _body(rig.client)) == OK
    assert 61 in rig.sleeps and len(rig.requests) == 2


def test_a_result_false_throttle_is_typed_throttled_and_still_a_DutchieUnavailable(rig):
    rig.script = [Resp(200, {"Result": False, "Message": "Too many requests"})] * 2
    with pytest.raises(DutchieThrottled) as err:
        rig.client.post("/api/x", _body(rig.client))
    assert isinstance(err.value, DutchieUnavailable) and err.value.backed_off is False


# ── transport ────────────────────────────────────────────────────────────────
def test_a_transport_error_then_success_through_the_backoffice_client_when_idempotent(rig):
    rig.script = [ConnectionError("tls reset"), Resp(200, OK)]
    assert rig.client.post("/api/x", _body(rig.client), idempotent=True) == OK
    assert len(rig.requests) == 2 and rig.logins == 1


def test_a_transport_error_still_fails_at_once_when_not_idempotent(rig):
    rig.script = [ConnectionError("tls reset"), Resp(200, OK)]
    with pytest.raises(DutchieUnavailable, match="tls reset"):
        rig.client.post("/api/x", _body(rig.client))
    assert len(rig.requests) == 1


def test_the_retry_kwargs_never_leak_into_a_signature_the_wrapper_forgot(rig):
    # the exact regression: the wrapper must accept whatever PosClient.post re-calls itself with
    rig.script = [Resp(429), ConnectionError("x"), Resp(401), Resp(200, OK)]
    assert rig.client.post("/api/x", _body(rig.client), idempotent=True) == OK
