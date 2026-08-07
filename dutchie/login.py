"""Dutchie employee login — vendored from monorepo dutchie_client.py::login_employee.

POST {base}/api/posv3/user/EmployeeLogin -> (cookie_header, session_gid, user_id).
The cookie_header carries the auth/session cookies we replay on every later call.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

from .transport import headers as _headers
from .transport import http_post

logger = logging.getLogger(__name__)


class DutchieAuthRejected(Exception):
    """Dutchie says these credentials are wrong. A definite NO."""


class DutchieAuthUnavailable(Exception):
    """We could not get an answer — network, Cloudflare, 5xx, malformed body.

    Distinct from rejection ON PURPOSE. Anything that authenticates a human must be
    able to tell "wrong password" from "no answer", or a network blip silently
    becomes an authorisation decision.
    """


def authenticate_employee(base_url: str, username: str, password: str,
                          loc_id: int, lsp_id: int, timeout: int = 30) -> dict:
    """Validate ONE employee's Dutchie credentials. Fails closed.

    Returns {"user_id", "session_gid", "cookie_header"} on success.
    Raises DutchieAuthRejected (definite no) or DutchieAuthUnavailable (no answer).

    WHY THIS EXISTS SEPARATELY FROM `login_employee`: that function collapses every
    outcome into `None`... except the worst one. Below HTTP 400 it never checked
    `Result`, so a rejection carried in a 200 body produced `("", "", 0)` — a
    NON-EMPTY, therefore TRUTHY, tuple. Its caller's `if not raw:` guard sails past
    it. Every other Dutchie call in this codebase checks `Result is False`
    (session.py); login was the one that didn't, and it is the one that decides who
    gets in. Success here demands positive evidence: Result not false, a non-empty
    SessionGId, a non-zero UserId, and at least one cookie.

    CONFIRMED AGAINST THE LIVE API (2026-08-07, fixtures/employee_login_*.json):
    Dutchie rejects with **HTTP 200 + Result:false**, never a 401 — so the truthy
    ("", "", 0) hole was reachable in production, not theoretical. The rejection body
    also ECHOES THE SUBMITTED PASSWORD, which is why nothing in here logs.

    NOT confirmed, and deliberately not enforced: we sent LocId 3501 and Dutchie
    answered LocId 3498. EmployeeLogin does not scope to the location we ask for, so
    a success proves WHO, never WHERE.
    """
    url = urljoin(base_url.rstrip("/") + "/", "api/posv3/user/EmployeeLogin")
    payload = {"UserName": username, "Password": password, "AppId": 2,
               "LocId": int(loc_id), "LspId": int(lsp_id)}
    try:
        resp = http_post(
            url, json=payload,
            headers=_headers(base_url, **{"Accept": "application/json, text/plain, */*",
                                          "Content-Type": "application/json"}),
            timeout=timeout)
    except Exception as exc:
        # Never log the payload — it carries the password.
        raise DutchieAuthUnavailable(f"{type(exc).__name__}: {exc}") from None

    status = resp.status_code
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = None

    if status == 401:
        # 401 with a JSON body is Dutchie answering. A 401 from an edge proxy with an
        # HTML body is not, and must not read as "wrong password".
        if isinstance(data, dict):
            raise DutchieAuthRejected("bad credentials")
        raise DutchieAuthUnavailable("401 without a JSON body (edge, not Dutchie)")
    if status >= 400:
        # 403 is ambiguous by design here: Dutchie denying the employee, or Cloudflare
        # denying our TLS fingerprint (the reason transport.py exists at all). Ambiguous
        # means unavailable, never "rejected".
        raise DutchieAuthUnavailable(f"HTTP {status}")
    if not isinstance(data, dict):
        raise DutchieAuthUnavailable("non-JSON body on a 2xx")
    if data.get("Result") is False:
        raise DutchieAuthRejected(str(data.get("Message") or "rejected"))

    inner = data.get("Data") or data.get("body") or data
    if not isinstance(inner, dict):
        raise DutchieAuthUnavailable("unrecognised body shape")
    session_gid = str(inner.get("SessionGId") or "")
    try:
        user_id = int(inner.get("UserId") or 0)
    except (TypeError, ValueError):
        user_id = 0
    cookie_header = _cookies_from(resp)

    # Positive evidence only. An empty session or a zero user id is a rejection
    # wearing a 200 — the exact shape that used to authenticate anybody.
    if not session_gid or user_id <= 0 or not cookie_header:
        raise DutchieAuthRejected("no session issued")
    return {"user_id": user_id, "session_gid": session_gid, "cookie_header": cookie_header}


def _cookies_from(resp) -> str:
    parts: list[str] = []
    try:
        for k, v in (resp.cookies or {}).items():
            if k and v:
                parts.append(f"{k}={v}")
    except Exception:
        pass
    if not parts:
        raw = resp.headers.get("Set-Cookie") or resp.headers.get("set-cookie") or ""
        parts = [p.split(";")[0].strip() for p in str(raw).split(",") if "=" in p]
    return "; ".join(parts)


def login_employee(
    base_url: str,
    username: str,
    password: str,
    loc_id: int,
    lsp_id: int,
    timeout: int = 30,
) -> tuple[str, str, int] | None:
    """Return (cookie_header, session_gid, user_id) or None on failure.

    base_url is the backoffice origin (e.g. https://ash.backoffice.dutchie.com).
    """
    url = urljoin(base_url.rstrip("/") + "/", "api/posv3/user/EmployeeLogin")
    payload = {
        "UserName": username,
        "Password": password,
        "AppId": 2,
        "LocId": int(loc_id),
        "LspId": int(lsp_id),
    }
    try:
        resp = http_post(
            url,
            json=payload,
            headers=_headers(
                base_url,
                **{"Accept": "application/json, text/plain, */*",
                   "Content-Type": "application/json"},
            ),
            timeout=timeout,
        )
        if resp.status_code >= 400:
            # Status ONLY. A live capture shows Dutchie echoing the submitted
            # password back inside the response body on a failed login, so the body
            # of anything on this path is credential material.
            logger.warning("Dutchie employee login HTTP %s", resp.status_code)
            return None
        try:
            data = resp.json() if resp.content else {}
        except Exception:
            data = {}
        inner = (data or {}).get("Data") or (data or {}).get("body") or data or {}
        session_gid = (inner or {}).get("SessionGId") or ""
        user_id = int((inner or {}).get("UserId") or 0)

        # Pull every cookie the auth set (incl. any CF tokens).
        cookie_parts: list[str] = []
        try:
            for k, v in (resp.cookies or {}).items():
                if k and v:
                    cookie_parts.append(f"{k}={v}")
        except Exception:
            pass
        if not cookie_parts:  # fallback: parse Set-Cookie header manually
            set_cookie = resp.headers.get("Set-Cookie") or resp.headers.get("set-cookie") or ""
            cookie_parts = [p.split(";")[0].strip()
                            for p in str(set_cookie).split(",") if "=" in p]
        return "; ".join(cookie_parts), session_gid, user_id
    except Exception as exc:
        logger.warning("Dutchie employee login failed: %s", exc)
        return None
