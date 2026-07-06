"""Session retention + the base POS client (login cache, session block, 401 re-login).

Vendored/condensed from monorepo dutchie_backoffice_client.py::BackofficeClient
(_session / _session_block / _invalidate / _post, lines 240-426).

A `Store` carries the per-location Dutchie identifiers + one employee credential.
`PosClient` logs in once, caches the session (TTL 600s), builds the session block
every backoffice call needs, and re-logs-in exactly once on a 401/403.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .login import login_employee
from .secrets import decrypt_secret
from .transport import headers as _headers
from .transport import http_get, http_post

logger = logging.getLogger(__name__)


class DutchieUnavailable(RuntimeError):
    """Transport/5xx/non-JSON/Result=false — a retryable-or-report failure."""


class DutchieSessionExpired(RuntimeError):
    """401/403 even after a fresh re-login — credentials are bad."""


@dataclass
class Store:
    """One physical location + the employee credential we act as.

    base_url     backoffice origin used for EmployeeLogin (ash.backoffice.dutchie.com)
    pos_base_url POS register origin for the cart/guest calls (ash.pos.dutchie.com)
    """

    name: str
    base_url: str
    pos_base_url: str
    org_id: int
    lsp_id: int
    loc_id: int
    register_id: int
    username: str
    # repr=False so secrets never leak into tracebacks / logs / __repr__.
    password: str = field(repr=False)  # may be "enc:v1:..." — decrypted at login
    api_key: str = field(default="", repr=False)  # REST read key, optional


@dataclass
class EmployeeSession:
    cookie_header: str
    session_gid: str
    user_id: int


class PosClient:
    """Base client: holds a Store, manages the login session, posts JSON.

    Subclass and set `base_origin` to the host you call (POS register vs backoffice).
    """

    base_origin = ""  # set by subclass, e.g. https://ash.pos.dutchie.com
    _LOGIN_TTL = 600.0  # 10 min, matches the monorepo cookie TTL
    _login_cache: dict = {}  # (store_name, loc_id) -> (ts, EmployeeSession)

    def __init__(self, store: Store, timeout: float = 30.0):
        self.store = store
        self.timeout = timeout
        self._pinned: EmployeeSession | None = None

    # ── auth ────────────────────────────────────────────────────────────────
    def _login_base(self) -> str:
        """Origin to run EmployeeLogin against. POS register logs in at ash.pos
        directly (HAR-confirmed); override in subclasses."""
        return self.store.base_url

    def _session(self, force_refresh: bool = False) -> EmployeeSession:
        if not force_refresh and self._pinned is not None:
            return self._pinned
        key = (self.store.name, self.store.loc_id)
        now = time.time()
        if not force_refresh:
            cached = self._login_cache.get(key)
            if cached and (now - cached[0]) < self._LOGIN_TTL:
                self._pinned = cached[1]
                return cached[1]

        login_base = self._login_base()
        username = (self.store.username or "").strip()
        password = (decrypt_secret(self.store.password) or "").strip()
        if not (login_base and username and password):
            raise DutchieUnavailable(
                f"store={self.store.name} missing base_url/username/password"
            )
        raw = login_employee(
            login_base, username, password,
            int(self.store.loc_id), int(self.store.lsp_id),
        )
        if not raw:
            raise DutchieUnavailable(
                f"login_employee returned None for store={self.store.name} "
                f"loc_id={self.store.loc_id} — check creds + Cloudflare (need curl_cffi)"
            )
        cookie_header, session_gid, user_id = raw
        sess = EmployeeSession(cookie_header, session_gid, int(user_id or 0))
        self._login_cache[key] = (now, sess)
        self._pinned = sess
        return sess

    def session_block(self, with_register: bool = False) -> dict:
        """The block every backoffice/POS call merges into its body."""
        sess = self._session()
        block = {
            "SessionId": sess.session_gid,
            "LspId": str(self.store.lsp_id),
            "LocId": str(self.store.loc_id),
            "OrgId": str(self.store.org_id),
            "UserId": str(sess.user_id),
        }
        if with_register:
            block["Register"] = self.store.register_id
        return block

    def _invalidate(self) -> None:
        self._login_cache.pop((self.store.name, self.store.loc_id), None)
        self._pinned = None

    # ── transport (one re-login retry on 401/403) ────────────────────────────
    def post(self, path: str, body: dict, *, _retry: bool = False, raw: bool = False) -> dict:
        sess = self._session(force_refresh=_retry)
        h = _headers(self.base_origin, **{
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "cookie": sess.cookie_header,
        })
        url = self.base_origin + path
        try:
            resp = http_post(url, json=body, headers=h, timeout=self.timeout)
        except Exception as exc:
            raise DutchieUnavailable(f"POST {url}: {exc}") from exc

        if resp.status_code in (401, 403) and not _retry:
            logger.info("POS %s -> %s; re-logging in", path, resp.status_code)
            self._invalidate()
            return self.post(path, body, _retry=True, raw=raw)
        if resp.status_code in (401, 403):
            raise DutchieSessionExpired(f"{url} -> HTTP {resp.status_code} after re-login")
        if resp.status_code >= 500:
            raise DutchieUnavailable(f"{url} -> HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as exc:
            raise DutchieUnavailable(
                f"{url} non-JSON ({resp.status_code}): {getattr(resp, 'text', '')[:200]!r}"
            ) from exc
        if raw:
            return data
        if not isinstance(data, dict) or data.get("Result") is False:
            raise DutchieUnavailable(f"{url} Result=false: {(data or {}).get('Message')!r}")
        return data

    def get(self, path: str, params: dict | None = None) -> object:
        sess = self._session()
        h = _headers(self.base_origin, **{"Accept": "application/json, text/plain, */*",
                                          "cookie": sess.cookie_header})
        url = self.base_origin + path
        try:
            resp = http_get(url, headers=h, params=params, timeout=self.timeout)
        except Exception as exc:
            raise DutchieUnavailable(f"GET {url}: {exc}") from exc
        if resp.status_code in (401, 403):
            self._invalidate()
            sess = self._session(force_refresh=True)
            h["cookie"] = sess.cookie_header
            resp = http_get(url, headers=h, params=params, timeout=self.timeout)
        return {"status": resp.status_code, "text": getattr(resp, "text", "")[:500]}
