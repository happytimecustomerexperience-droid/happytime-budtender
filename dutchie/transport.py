"""HTTP transport — curl_cffi Chrome impersonation, requests fallback.

Cloudflare WAF on *.dutchie.com 403s plain python-requests (JA3 TLS fingerprint).
curl_cffi impersonates Chrome's real TLS+HTTP/2+header-order fingerprint so the
request looks like the browser. Falls back to requests if curl_cffi isn't installed
(dev host) so the rest of the pipeline keeps working.

Vendored from monorepo: apps/automation/ingestion/dutchie_client.py (19-62).
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as _cffi_requests  # type: ignore[import-not-found]

    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover - depends on host
    _cffi_requests = None
    HAS_CURL_CFFI = False


# Chrome UA + Origin keep the request self-consistent; curl_cffi handles TLS/HTTP2.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


def headers(origin: str, **extra) -> dict:
    h = dict(BROWSER_HEADERS)
    h["Origin"] = origin
    h["Referer"] = origin.rstrip("/") + "/"
    h.update(extra)
    return h


def http_post(url, json=None, headers=None, cookies=None, timeout=30):
    """POST impersonating Chrome when curl_cffi is available."""
    if HAS_CURL_CFFI:
        return _cffi_requests.post(
            url, json=json, headers=headers, cookies=cookies,
            timeout=timeout, impersonate="chrome",
        )
    return requests.post(url, json=json, headers=headers, cookies=cookies, timeout=timeout)


def http_get(url, headers=None, cookies=None, params=None, timeout=30):
    if HAS_CURL_CFFI:
        return _cffi_requests.get(
            url, headers=headers, cookies=cookies, params=params,
            timeout=timeout, impersonate="chrome",
        )
    return requests.get(url, headers=headers, cookies=cookies, params=params, timeout=timeout)
