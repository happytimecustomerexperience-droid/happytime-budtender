"""Vapi REST client — the ONLY code that talks to ``api.vapi.ai`` (20-SPEC-vapi-deploy.md §5;
01-ARCHITECTURE.md §5).

Base ``https://api.vapi.ai``; ``Authorization: Bearer <VAPI_PRIVATE_KEY>``. CRUD on
``/assistant``, ``/squad``, ``/tool``, ``/phone-number``, ``/file`` and ``/workflow``. The
``/workflow`` endpoint is beta/undocumented but OWNER-AUTHORIZED for the reliable guided-
questionnaire agent (ADR-023 supersedes ADR-002); its live shape was pinned by a smoke probe.
The squad provisioner (``voice/provision.py``) still never touches ``/workflow`` — the workflow
agent is built + provisioned from its own module (``voice/workflow.py``) and runs in parallel.

Cross-cutting (§5.1):
  * Bearer auth header injected once; ``Content-Type``/``Accept``/``User-Agent`` set on the session.
  * Retries with exponential backoff + jitter on 429/5xx/transport errors, honoring ``Retry-After``
    (``VAPI_MAX_RETRIES`` attempts; BASE=0.5s, CAP=8s).
  * Pagination helper across list pages (cap ``VAPI_LIST_CAP``).
  * Secret redaction in every log line + ``VapiError`` body (``server.secret``/Bearer/keys → ``***``).
  * A typed ``VapiError(status, body, method, path)`` raised on any non-2xx after retries — never a
    silent swallow.
  * ``dry_run`` mode: non-GET writes are RECORDED (``recorded_calls``) and return a synthetic id,
    GETs still execute — so the reconcile logic runs without writing (§5.1.1).

The surface stays module-functional (``get``/``post``/``patch``/``delete`` + typed helpers) — the
idempotency primitive is ``find_*_by_name`` and ``auth_ok()`` powers ``/healthz``. ``kb/vapi_files.py``
calls these same primitives, so the module API is preserved.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("VAPI_BASE_URL", "https://api.vapi.ai")
_TIMEOUT = float(os.environ.get("VAPI_TIMEOUT", "20"))
_MAX_RETRIES = int(os.environ.get("VAPI_MAX_RETRIES", "4"))
_LIST_CAP = int(os.environ.get("VAPI_LIST_CAP", "2000"))
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0
_RETRY_STATUS = {429, 500, 502, 503, 504}

# ── dry-run recorder (§5.1.1) ─────────────────────────────────────────────────
# When dry_run is on, non-GET writes are recorded here (redacted) instead of issued, and a
# synthetic object is returned so the reconcile engine still runs. GETs execute normally.
_dry_run = False
recorded_calls: list[dict] = []


def set_dry_run(on: bool) -> None:
    """Toggle dry-run. Auto-engaged by the provisioner when VAPI_PRIVATE_KEY is unset."""
    global _dry_run
    _dry_run = bool(on)
    if on:
        recorded_calls.clear()


def is_dry_run() -> bool:
    return _dry_run


class VapiError(RuntimeError):
    """A Vapi REST call returned a non-2xx (or transport failed after retries).

    Carries ``.status`` / ``.body`` (redacted) / ``.method`` / ``.path``; ``str()`` is a one-line
    redacted summary safe to log."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        method: str = "",
        path: str = "",
    ):
        self.status = status
        self.body = _redact(body)
        self.method = method
        self.path = path
        super().__init__(_redact(message))


# ── secret redaction (§5.1) — one redactor, shared with logs + VapiError ──────
def _secret_values() -> list[str]:
    vals = []
    for name in ("VAPI_PRIVATE_KEY", "VAPI_WEBHOOK_SECRET", "HHT_BACKEND_TOKEN"):
        v = os.environ.get(name, "")
        if v:
            vals.append(v)
    return vals


def _redact(text: str) -> str:
    """Mask any live secret value + any Bearer token in a string before it is logged/raised."""
    if not text:
        return text
    out = str(text)
    for v in _secret_values():
        out = out.replace(v, "***")
    # Blanket-mask a Bearer header value even if the key isn't in env (e.g. a copied header).
    lowered = out.lower()
    idx = lowered.find("bearer ")
    while idx != -1:
        end = idx + len("bearer ")
        stop = end
        while stop < len(out) and out[stop] not in " \"'\n\r\t":
            stop += 1
        out = out[:end] + "***" + out[stop:]
        lowered = out.lower()
        idx = lowered.find("bearer ", end + 3)
    return out


def redact_payload(obj: Any) -> Any:
    """Deep-copy a payload masking ``server.secret`` (and any nested secret value) for logging /
    the dry-run dump — never mutate the original, never log a real secret."""
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if k == "secret" and isinstance(v, str):
                out[k] = "***"
            else:
                out[k] = redact_payload(v)
        return out
    if isinstance(obj, list):
        return [redact_payload(v) for v in obj]
    if isinstance(obj, str):
        return _redact(obj)
    return obj


# ── auth + transport ──────────────────────────────────────────────────────────
def _private_key() -> str:
    return os.environ.get("VAPI_PRIVATE_KEY", "")


def configured() -> bool:
    """True iff a Vapi private key is present (the auth-config presence check)."""
    return bool(_private_key())


def _client() -> httpx.Client:
    key = _private_key()
    if not key:
        raise VapiError("VAPI_PRIVATE_KEY not configured")
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "happytime-voice/0.1",
        },
        timeout=_TIMEOUT,
    )


def _sleep_for(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return min(_BACKOFF_BASE * (2**attempt), _BACKOFF_CAP) + random.uniform(0, _BACKOFF_BASE)


def _request(method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
    """The one funnel: dry-run recording + retry/backoff + redacted fail-loud (§5.1)."""
    method = method.upper()

    if _dry_run:
        if method != "GET":
            recorded_calls.append({"method": method, "path": path, "json": redact_payload(json)})
            logger.info("vapi DRY-RUN %s %s", method, path)
            return {"id": f"dryrun-{path.strip('/').replace('/', '-')}"}
        # Dry-run GETs (reads) still execute against a live key (so reconcile sees real objects).
        # With NO key configured there is nothing to read → return empty so reconcile plans a
        # create rather than crashing on an unconfigured client (the offline `--dry-run` path).
        if not configured():
            return None

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with _client() as client:
                resp = client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:  # transport-level failure → retry, then fail loud
            last_exc = exc
            if attempt < _MAX_RETRIES:
                time.sleep(_sleep_for(attempt, None))
                continue
            raise VapiError(
                f"Vapi transport error on {method} {path}: {exc}", method=method, path=path
            ) from exc

        if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
            wait = _sleep_for(attempt, resp.headers.get("Retry-After"))
            logger.warning(
                "vapi %s %s → HTTP %s (retry %s/%s in %.2fs)",
                method,
                path,
                resp.status_code,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait)
            continue

        if resp.status_code >= 300:
            raise VapiError(
                f"Vapi {method} {path} → HTTP {resp.status_code}",
                status=resp.status_code,
                body=resp.text[:500],
                method=method,
                path=path,
            )
        if not resp.content:
            return None
        return resp.json()

    # Unreachable in practice (the loop either returns or raises) — fail loud if it ever is.
    raise VapiError(f"Vapi {method} {path} exhausted retries: {last_exc}", method=method, path=path)


# ── Verb primitives ───────────────────────────────────────────────────────────
def get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params=params)


def post(path: str, json: Any) -> Any:
    return _request("POST", path, json=json)


def patch(path: str, json: Any) -> Any:
    return _request("PATCH", path, json=json)


def delete(path: str) -> Any:
    return _request("DELETE", path)


def _paginated(path: str, params: dict | None = None) -> list[dict]:
    """Yield items across Vapi's list pages until exhausted (cap ``VAPI_LIST_CAP``).

    Vapi list endpoints return a bare array today; if a future version wraps the page in
    ``{results, metadata.{nextCursor|hasMore}}`` we follow the cursor. Either shape is handled in
    this ONE place so ``find_*_by_name`` never misses an object on a large account."""
    items: list[dict] = []
    cursor: str | None = None
    pages = 0
    while True:
        page_params = dict(params or {})
        page_params.setdefault("limit", 100)
        if cursor:
            page_params["cursor"] = cursor
        page = get(path, params=page_params)
        if isinstance(page, dict):
            batch = page.get("results") or page.get("items") or []
            meta = page.get("metadata") or {}
            cursor = meta.get("nextCursor") or meta.get("next") or None
            has_more = bool(meta.get("hasMore"))
        else:
            batch = page or []
            cursor = None
            has_more = False
        items.extend([b for b in batch if isinstance(b, dict)])
        pages += 1
        if len(items) >= _LIST_CAP or (not cursor and not has_more) or not batch:
            break
    return items[:_LIST_CAP]


# ── Assistants ────────────────────────────────────────────────────────────────
def list_assistants(**filters) -> list[dict]:
    return _paginated("/assistant", filters)


def get_assistant(assistant_id: str) -> dict:
    return get(f"/assistant/{assistant_id}")


def create_assistant(body: dict) -> dict:
    return post("/assistant", body)


def patch_assistant(assistant_id: str, body: dict) -> dict:
    return patch(f"/assistant/{assistant_id}", body)


def delete_assistant(assistant_id: str) -> None:
    delete(f"/assistant/{assistant_id}")


def find_assistant_by_name(name: str) -> dict | None:
    return _find_by_name(list_assistants(), name)


# ── Squads ────────────────────────────────────────────────────────────────────
def list_squads(**filters) -> list[dict]:
    return _paginated("/squad", filters)


def get_squad(squad_id: str) -> dict:
    return get(f"/squad/{squad_id}")


def create_squad(body: dict) -> dict:
    return post("/squad", body)


def patch_squad(squad_id: str, body: dict) -> dict:
    return patch(f"/squad/{squad_id}", body)


def delete_squad(squad_id: str) -> None:
    delete(f"/squad/{squad_id}")


def find_squad_by_name(name: str) -> dict | None:
    return _find_by_name(list_squads(), name)


# ── Workflows (beta, OWNER-AUTHORIZED — ADR-023 supersedes ADR-002) ───────────
# The reliable guided-questionnaire agent lives here; built/provisioned from voice/workflow.py.
def list_workflows(**filters) -> list[dict]:
    return _paginated("/workflow", filters)


def get_workflow(workflow_id: str) -> dict:
    return get(f"/workflow/{workflow_id}")


def create_workflow(body: dict) -> dict:
    return post("/workflow", body)


def patch_workflow(workflow_id: str, body: dict) -> dict:
    return patch(f"/workflow/{workflow_id}", body)


def delete_workflow(workflow_id: str) -> None:
    delete(f"/workflow/{workflow_id}")


def find_workflow_by_name(name: str) -> dict | None:
    return _find_by_name(list_workflows(), name)


# ── Tools ─────────────────────────────────────────────────────────────────────
def list_tools(**filters) -> list[dict]:
    return _paginated("/tool", filters)


def get_tool(tool_id: str) -> dict:
    return get(f"/tool/{tool_id}")


def create_tool(body: dict) -> dict:
    return post("/tool", body)


def patch_tool(tool_id: str, body: dict) -> dict:
    return patch(f"/tool/{tool_id}", body)


def delete_tool(tool_id: str) -> None:
    delete(f"/tool/{tool_id}")


def find_tool_by_name(name: str) -> dict | None:
    """Custom function/query tools name lives under ``function.name`` (or top-level ``name``)."""
    for obj in list_tools():
        fn = (obj or {}).get("function") or {}
        if fn.get("name") == name or obj.get("name") == name:
            return obj
    return None


# ── Phone numbers (no create — owner-provisioned in Vapi; only PATCH to attach) ──
def list_phone_numbers(**filters) -> list[dict]:
    return _paginated("/phone-number", filters)


def get_phone_number(number_id: str) -> dict:
    return get(f"/phone-number/{number_id}")


def patch_phone_number(number_id: str, body: dict) -> dict:
    return patch(f"/phone-number/{number_id}", body)


def find_phone_number(id_or_e164: str) -> dict | None:
    for obj in list_phone_numbers():
        if (obj or {}).get("id") == id_or_e164 or (obj or {}).get("number") == id_or_e164:
            return obj
    return None


# ── Files (KB mirror) ──────────────────────────────────────────────────────────
def list_files(**filters) -> list[dict]:
    return _paginated("/file", filters)


def get_file(file_id: str) -> dict:
    return get(f"/file/{file_id}")


def upload_file(name: str, content: str, mime: str = "text/markdown") -> dict:
    """Idempotent upload: replace-by-name (delete the prior file of that name, then create).

    P0 sends the file as a JSON body; if a future Vapi version requires multipart, this is the
    one place to adapt. ``kb/vapi_files.py`` is the caller."""
    prior = find_file_by_name(name)
    if prior and prior.get("id"):
        try:
            delete_file(prior["id"])
        except VapiError:
            logger.warning("vapi: could not delete prior file %s", name, exc_info=True)
    return post("/file", {"name": name, "content": content, "mimeType": mime}) or {}


def delete_file(file_id: str) -> None:
    delete(f"/file/{file_id}")


def find_file_by_name(name: str) -> dict | None:
    return _find_by_name(list_files(), name)


# ── shared helpers ──────────────────────────────────────────────────────────────
def _find_by_name(items: list[dict], name: str) -> dict | None:
    for obj in items or []:
        if (obj or {}).get("name") == name:
            return obj
    return None


def auth_ok() -> dict:
    """Cheap reachability/auth probe for ``/healthz``: ``GET /assistant?limit=1``.

    Returns ``{"ok": bool, "configured": bool, "error": str}``. Never raises — a missing key
    degrades to ``ok=false`` (the app still boots; 10-P0 D2 / B1)."""
    if not configured():
        return {"ok": False, "configured": False, "error": "VAPI_PRIVATE_KEY not configured"}
    try:
        get("/assistant", params={"limit": 1})
        return {"ok": True, "configured": True, "error": ""}
    except VapiError as exc:
        return {"ok": False, "configured": True, "error": str(exc)}
