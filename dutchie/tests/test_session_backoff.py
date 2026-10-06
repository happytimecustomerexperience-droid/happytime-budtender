"""PosClient.post: opt-in 429 back-off and transport retry (the backoffice READS pass idempotent=True).

Ported from the marketing dashboard's backoffice client. Everything new is OPT-IN, so the POS register's
writes keep exactly today's semantics: a 429 or a transport error on a call that did not say
`idempotent=True` still fails at once, because re-sending a non-idempotent body can double-apply it.
The 401/403 re-login (once) is unchanged. No network: login and HTTP are faked, sleeps are recorded.
"""
import pytest

from dutchie import session as sess
from dutchie.session import DutchieSessionExpired, DutchieUnavailable, PosClient, Store

STORE = Store(name="backoff-test", base_url="https://bo.example", pos_base_url="https://pos.example",
              org_id=1, lsp_id=2, loc_id=3, register_id=4, username="u", password="p")


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


OK = {"Result": True, "Data": {"x": 1}}


@pytest.fixture
def rig(monkeypatch):
    """A PosClient whose login and HTTP are scripted. rig.script = [Resp | Exception, ...]"""
    PosClient._login_cache.clear()

    class Rig:
        script: list = []
        logins = 0
        posts = 0
        sleeps: list = []

    r = Rig()
    r.script, r.sleeps = [], []

    def fake_login(*a, **k):
        r.logins += 1
        return ("cookie", f"SID-{r.logins}", 9)

    def fake_post(url, json=None, headers=None, cookies=None, timeout=30):
        r.posts += 1
        item = r.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sess, "login_employee", fake_login)
    monkeypatch.setattr(sess, "http_post", fake_post)
    monkeypatch.setattr(sess.time, "sleep", r.sleeps.append)
    client = PosClient(STORE)
    client.base_origin = "https://bo.example"
    r.client = client
    yield r
    PosClient._login_cache.clear()


# ── 429 ──────────────────────────────────────────────────────────────────────
def test_a_429_then_success_is_retried_after_the_retry_after_hint(rig):
    rig.script = [Resp(429, headers={"Retry-After": "7"}), Resp(200, OK)]
    assert rig.client.post("/x", {}, idempotent=True) == OK
    assert rig.posts == 2 and rig.sleeps == [7.0]
    assert rig.logins == 1  # a throttle is not a login problem


def test_retry_after_zero_never_becomes_a_busy_loop(rig):
    rig.script = [Resp(429, headers={"Retry-After": "0"}), Resp(429, headers={"Retry-After": "0"}), Resp(200, OK)]
    rig.client.post("/x", {}, idempotent=True)
    assert rig.sleeps == [2.0, 4.0]  # the exponential floor, not 0


def test_a_missing_or_unparseable_retry_after_falls_back_to_exponential_backoff(rig):
    rig.script = [Resp(429), Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), Resp(200, OK)]
    rig.client.post("/x", {}, idempotent=True)
    assert rig.sleeps == [2.0, 4.0]


def test_a_huge_retry_after_is_capped(rig):
    rig.script = [Resp(429, headers={"Retry-After": "86400"}), Resp(200, OK)]
    rig.client.post("/x", {}, idempotent=True)
    assert rig.sleeps == [120.0]


def test_a_429_that_never_clears_raises_a_clear_error_after_bounded_retries(rig):
    rig.script = [Resp(429, text="Too many requests - only 60 per minute allowed") for _ in range(20)]
    with pytest.raises(DutchieUnavailable) as err:
        rig.client.post("/x", {}, idempotent=True)
    msg = str(err.value)
    assert "429" in msg and "back-off" in msg.lower()
    assert "Too many requests" in msg                    # the vendor's own words survive for the caller
    assert rig.posts == 1 + sess._MAX_THROTTLE_RETRIES   # bounded
    assert rig.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_without_idempotent_a_429_is_not_retried_today_s_behaviour(rig):
    rig.script = [Resp(429, {"Result": False, "Message": "Too many requests"}), Resp(200, OK)]
    with pytest.raises(DutchieUnavailable, match="Result=false"):
        rig.client.post("/x", {})
    assert rig.posts == 1 and rig.sleeps == []


# ── transport errors ─────────────────────────────────────────────────────────
def test_a_transport_error_is_retried_only_when_the_caller_says_idempotent(rig):
    rig.script = [ConnectionError("tls reset"), Resp(200, OK)]
    assert rig.client.post("/x", {}, idempotent=True) == OK
    assert rig.posts == 2 and rig.sleeps == [1]
    assert rig.logins == 1  # no re-login for a dropped connection


def test_a_transport_error_without_idempotent_fails_at_once(rig):
    rig.script = [ConnectionError("tls reset"), Resp(200, OK)]
    with pytest.raises(DutchieUnavailable, match="tls reset"):
        rig.client.post("/x", {})
    assert rig.posts == 1 and rig.sleeps == []


def test_transport_retries_are_bounded(rig):
    rig.script = [ConnectionError("down")] * 10
    with pytest.raises(DutchieUnavailable):
        rig.client.post("/x", {}, idempotent=True)
    assert rig.posts == 1 + sess._TRANSPORT_RETRIES


def test_a_server_error_is_not_retried_even_when_idempotent(rig):
    rig.script = [Resp(503), Resp(200, OK)]
    with pytest.raises(DutchieUnavailable, match="503"):
        rig.client.post("/x", {}, idempotent=True)
    assert rig.posts == 1


# ── session: 401/403 re-login exactly once (unchanged) ───────────────────────
def test_a_401_logs_in_again_exactly_once_and_retries(rig):
    rig.script = [Resp(401), Resp(200, OK)]
    assert rig.client.post("/x", {}) == OK
    assert rig.logins == 2 and rig.posts == 2


def test_a_second_401_raises_without_a_third_login(rig):
    rig.script = [Resp(401), Resp(403), Resp(200, OK)]
    with pytest.raises(DutchieSessionExpired):
        rig.client.post("/x", {})
    assert rig.logins == 2 and rig.posts == 2


def test_the_relogin_is_the_same_with_idempotent_on(rig):
    rig.script = [Resp(403), Resp(200, OK)]
    assert rig.client.post("/x", {}, idempotent=True) == OK
    assert rig.logins == 2 and rig.posts == 2


def test_a_429_after_a_relogin_keeps_backing_off_on_the_new_session(rig):
    rig.script = [Resp(401), Resp(429, headers={"Retry-After": "3"}), Resp(200, OK)]
    assert rig.client.post("/x", {}, idempotent=True) == OK
    assert rig.logins == 2 and rig.sleeps == [3.0]


def test_one_login_serves_a_whole_run_of_calls(rig):
    rig.script = [Resp(200, OK)] * 5
    for _ in range(5):
        rig.client.post("/x", {}, idempotent=True)
    assert rig.logins == 1 and rig.posts == 5


# ── defaults untouched ───────────────────────────────────────────────────────
def test_the_default_post_still_returns_raw_bodies_and_raises_on_result_false(rig):
    rig.script = [Resp(200, {"Result": False, "Message": "nope"})]
    with pytest.raises(DutchieUnavailable, match="Result=false"):
        rig.client.post("/x", {})
    rig.script = [Resp(200, {"Result": False})]
    assert rig.client.post("/x", {}, raw=True) == {"Result": False}
