"""`authenticate_employee` must fail CLOSED. This is the door to the till.

The flaw it replaces: `login_employee` returned `("", "", 0)` when Dutchie answered
200 with a rejection — a NON-EMPTY, therefore TRUTHY, tuple that sailed past its
caller's `if not raw:` guard. Every other Dutchie call in this repo checks
`Result is False`; login was the one that didn't, and it is the one that decides who
gets in. So the test that matters most here is
`test_a_200_carrying_a_rejection_is_a_rejection`.
"""
import json
from pathlib import Path

import pytest

from dutchie.login import (DutchieAuthRejected, DutchieAuthUnavailable,
                           authenticate_employee)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class FakeResp:
    def __init__(self, status=200, body=None, cookies=None, text=""):
        self.status_code = status
        self._body = body
        self.cookies = cookies or {}
        self.headers = {}
        # Realistic: an HTML error page HAS content. The first version of this fake
        # gave a 401 an empty body, so the code took the "empty 2xx" path instead of
        # the non-JSON one and the test failed on the fixture, not the logic.
        self.content = b"x" if (body is not None or text) else b""
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _run(monkeypatch, resp):
    monkeypatch.setattr("dutchie.login.http_post", lambda *a, **k: resp)
    return authenticate_employee("https://pos.example", "ann", "pw", 1, 2)


OK_BODY = {"Result": True, "Data": {"SessionGId": "abc", "UserId": 55}}


def test_a_real_success_is_accepted(monkeypatch):
    got = _run(monkeypatch, FakeResp(body=OK_BODY, cookies={"auth": "1"}))
    assert got["user_id"] == 55 and got["session_gid"] == "abc"


def test_a_200_carrying_a_rejection_is_a_rejection(monkeypatch):
    """THE REGRESSION. This used to return a truthy ("", "", 0) and let anyone in."""
    with pytest.raises(DutchieAuthRejected):
        _run(monkeypatch, FakeResp(body={"Result": False, "Message": "bad password"}))


def test_a_200_with_no_session_is_a_rejection(monkeypatch):
    for body in ({"Data": {"SessionGId": "", "UserId": 55}},
                 {"Data": {"SessionGId": "abc", "UserId": 0}}):
        with pytest.raises(DutchieAuthRejected):
            _run(monkeypatch, FakeResp(body=body, cookies={"auth": "1"}))


def test_a_success_without_cookies_is_a_rejection(monkeypatch):
    # No cookie means no session to replay — success on paper only.
    with pytest.raises(DutchieAuthRejected):
        _run(monkeypatch, FakeResp(body=OK_BODY, cookies={}))


def test_a_401_with_json_is_a_rejection(monkeypatch):
    with pytest.raises(DutchieAuthRejected):
        _run(monkeypatch, FakeResp(status=401, body={"Message": "nope"}))


def test_a_401_without_json_is_unavailable_not_a_rejection(monkeypatch):
    # An edge proxy answering 401 with HTML is not Dutchie judging the password.
    with pytest.raises(DutchieAuthUnavailable):
        _run(monkeypatch, FakeResp(status=401, body=None, text="<html>denied</html>"))


def test_a_403_is_unavailable(monkeypatch):
    # Ambiguous: Dutchie denying the employee, or Cloudflare denying our TLS
    # fingerprint (the reason transport.py exists). Ambiguous is never "rejected".
    with pytest.raises(DutchieAuthUnavailable):
        _run(monkeypatch, FakeResp(status=403, body=None, text="<html>cf</html>"))


def test_a_5xx_is_unavailable(monkeypatch):
    with pytest.raises(DutchieAuthUnavailable):
        _run(monkeypatch, FakeResp(status=500, body=None))


def test_a_transport_error_is_unavailable(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection reset")
    monkeypatch.setattr("dutchie.login.http_post", boom)
    with pytest.raises(DutchieAuthUnavailable):
        authenticate_employee("https://pos.example", "ann", "pw", 1, 2)


def test_a_non_json_200_is_unavailable(monkeypatch):
    with pytest.raises(DutchieAuthUnavailable):
        _run(monkeypatch, FakeResp(status=200, body=None, text="<html>"))


# ── the same questions, against what Dutchie ACTUALLY sent ────────────────────
# Everything above is a shape I reasoned my way to. These two run the classifier
# over bodies captured from the live API on 2026-08-07, so the success and
# rejection paths are pinned to reality rather than to my guess about it.

def _fixture(name):
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def test_the_real_rejection_dutchie_sends_is_a_rejection(monkeypatch):
    """Dutchie rejects with **HTTP 200 + Result:false**, not a 401.

    That is precisely the body the old `login_employee` converted into a truthy
    ("", "", 0) — so the hole this class replaced was live, not hypothetical. The
    real response also carries a __cf_bm cookie, which means "got a cookie" alone
    was never evidence of a session.
    """
    with pytest.raises(DutchieAuthRejected):
        _run(monkeypatch, FakeResp(status=200, body=_fixture("employee_login_rejected.json"),
                                   cookies={"__cf_bm": "x"}))


def test_the_real_success_dutchie_sends_is_accepted(monkeypatch):
    got = _run(monkeypatch, FakeResp(
        status=200, body=_fixture("employee_login_success.json"),
        cookies={"LLSession": "x", ".AspNet.SharedCookie": "y", "__cf_bm": "z"}))
    assert got["user_id"] == 95602
    assert got["session_gid"] == "REDACTED-GUID"


def test_dutchie_echoes_the_password_back_so_nothing_may_log_the_body():
    """The rejection body contains the submitted password IN CLEAR.

    Not a hypothetical: it is in the captured fixture. This test exists so that
    anyone who later adds `logger.warning(..., resp.text)` to the login path has to
    delete an explicitly-named guard to do it.
    """
    body = json.loads((FIXTURES / "employee_login_rejected.json").read_text(encoding="utf-8"))
    assert "password" in body["Data"], "fixture no longer proves the echo — re-capture"

    import inspect

    from dutchie import login as login_mod
    src = inspect.getsource(login_mod.authenticate_employee)
    assert "logger." not in src, "authenticate_employee must never log — the body carries the password"


def test_a_login_at_one_store_is_not_proof_of_belonging_to_it():
    """We asked for LocId 3501 (Yakima); Dutchie answered LocId 3498.

    3498 is none of our three stores (3499 / 3500 / 3501), so EmployeeLogin does NOT
    scope to the location we send. A successful sign-in therefore proves WHO someone
    is, never WHERE they are entitled to stand. Anything that later tries to treat
    the store picker as an authorisation boundary has to contend with this.
    """
    data = _fixture("employee_login_success.json")["Data"]
    assert data["LocId"] not in (3499, 3500, 3501)
