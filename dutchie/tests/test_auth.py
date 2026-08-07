"""`authenticate_employee` must fail CLOSED. This is the door to the till.

The flaw it replaces: `login_employee` returned `("", "", 0)` when Dutchie answered
200 with a rejection — a NON-EMPTY, therefore TRUTHY, tuple that sailed past its
caller's `if not raw:` guard. Every other Dutchie call in this repo checks
`Result is False`; login was the one that didn't, and it is the one that decides who
gets in. So the test that matters most here is
`test_a_200_carrying_a_rejection_is_a_rejection`.
"""
import pytest

from dutchie.login import (DutchieAuthRejected, DutchieAuthUnavailable,
                           authenticate_employee)


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
