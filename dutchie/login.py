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
            logger.warning(
                "Dutchie employee login HTTP %s — first 200 bytes: %s",
                resp.status_code, getattr(resp, "text", "")[:200],
            )
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
