"""Gemini client — Vertex AI (preferred, EU residency) with API-key dev fallback.

Adapted from hermes/creds/gemini.py and EXTENDED for this project (plan §10):
  - Files API upload (full-PDF-in-context)
  - context-cache create (the cost lever, plan §1/§12)
  - streaming generation (the inherited copilot path was non-streaming)
  - cached_content_token_count parsing + fail-loud cost via core.constants

Auth resolution (unchanged from hermes): GOOGLE_CLOUD_PROJECT -> Vertex;
else GEMINI_API_KEY -> consumer API (dev only); else error.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time as _time
from dataclasses import dataclass, field
from typing import Any

from core import constants

logger = logging.getLogger(__name__)


# ── Clock-drift immunity for Vertex SA JWT auth ───────────────────────
# google-auth signs the SA JWT with iat/exp from the system wall clock; if that
# clock is off by more than Google's tolerance the token endpoint rejects it
# (`invalid_grant: Invalid JWT ... iat/exp`). Windows/Docker clocks drift and
# FLAP, so we anchor real UTC to time.monotonic() (immune to wall-clock jumps)
# via ONE network Date-header reading, and patch google.auth's time source.
# Ported from django_base/apps/automation/ingestion/gemini_client.py.
_anchor_real_utc: _dt.datetime | None = None
_anchor_monotonic: float = 0.0
_ANCHOR_TTL = 600.0


def _measure_real_utc(timeout: float = 5.0) -> _dt.datetime | None:
    import email.utils
    import urllib.request

    for url in ("https://oauth2.googleapis.com/", "https://www.google.com/"):
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                date_hdr = r.headers.get("Date")
            if date_hdr:
                real = email.utils.parsedate_to_datetime(date_hdr)
                return real.astimezone(_dt.UTC).replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            continue
    return None


def _system_utcnow() -> _dt.datetime:
    """Naive UTC from the system wall clock (no deprecation; replaces utcnow())."""
    return _dt.datetime.now(_dt.UTC).replace(tzinfo=None)


def _corrected_utcnow() -> _dt.datetime:
    if _anchor_real_utc is None:
        return _system_utcnow()
    return _anchor_real_utc + _dt.timedelta(seconds=_time.monotonic() - _anchor_monotonic)


def ensure_clock_correction(force: bool = False) -> None:
    """Anchor real UTC to the monotonic clock and patch google-auth's time source
    so JWT iat/exp are real-UTC, immune to wall-clock drift. Cheap; safe to call
    before every Vertex client build."""
    global _anchor_real_utc, _anchor_monotonic
    now_mono = _time.monotonic()
    if force or _anchor_real_utc is None or (now_mono - _anchor_monotonic) > _ANCHOR_TTL:
        real = _measure_real_utc()
        if real is not None:
            _anchor_real_utc = real
            _anchor_monotonic = _time.monotonic()
            drift = (real - _system_utcnow()).total_seconds()
            if abs(drift) > 30:
                logger.warning(
                    "Gemini auth: system clock off by %.0fs from real UTC — "
                    "signing JWTs from monotonic-anchored real UTC instead.", drift
                )
    try:
        import google.auth._helpers as _h

        if not getattr(_h, "_clock_patched", False):
            _h.utcnow = _corrected_utcnow
            _h._clock_patched = True
    except Exception:  # noqa: BLE001
        logger.debug("Gemini auth: could not patch google.auth._helpers.utcnow", exc_info=True)


@dataclass
class GeminiResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    auth_mode: str = "unknown"  # "vertex" | "api-key"
    raw: Any = field(default=None, repr=False)


def _resolve_project_and_location() -> tuple[str | None, str]:
    project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("VERTEX_PROJECT_ID")
    )
    location = (
        os.environ.get("GOOGLE_CLOUD_LOCATION")
        or os.environ.get("GCP_LOCATION")
        or "europe-north1"  # EU data residency for Swedish customers (plan §6)
    )
    return project, location


def _vertex_explicitly_requested() -> bool:
    return os.environ.get("GEMINI_USE_VERTEX", "").strip().lower() in ("true", "1", "yes", "on")


def make_client(api_key: str | None = None):
    """Return (client, auth_mode). Vertex preferred; API key is a dev fallback."""
    from google import genai

    project, location = _resolve_project_and_location()
    if _vertex_explicitly_requested() and not project:
        raise RuntimeError(
            "GEMINI_USE_VERTEX=True but GOOGLE_CLOUD_PROJECT is unset. "
            "Set GOOGLE_CLOUD_PROJECT to enable Vertex AI."
        )
    if project:
        logger.info("Gemini: Vertex mode (project=%s location=%s)", project, location)
        ensure_clock_correction()  # make SA JWT signing immune to wall-clock drift
        try:
            return genai.Client(vertexai=True, project=project, location=location), "vertex"
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Vertex AI client init failed (project={project}, location={location}): {e}. "
                "Fix: `gcloud auth application-default login` OR set "
                "GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON."
            ) from e

    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "No Gemini auth configured. Set GOOGLE_CLOUD_PROJECT (Vertex; recommended) "
            "OR GEMINI_API_KEY (consumer API; dev only)."
        )
    logger.info("Gemini: API-key mode (dev fallback)")
    return genai.Client(api_key=key), "api-key"


def _usage(resp) -> tuple[int, int, int]:
    """Return (prompt_tokens, completion_tokens, cached_tokens) from usage_metadata."""
    u = getattr(resp, "usage_metadata", None)
    if not u:
        return 0, 0, 0
    p_in = getattr(u, "prompt_token_count", 0) or 0
    p_out = getattr(u, "candidates_token_count", 0) or 0
    cached = getattr(u, "cached_content_token_count", 0) or 0
    return p_in, p_out, cached


def generate(
    contents,
    *,
    model: str,
    system_instruction: str | None = None,
    cached_content: str | None = None,
    max_output_tokens: int = 1024,
    temperature: float = 0.4,
    response_mime_type: str | None = None,
    thinking_budget: int | None = 0,
    api_key: str | None = None,
) -> GeminiResponse:
    """Single-turn generation. `contents` may be a string or a list of parts
    (text + uploaded files for the full-PDF-in-context path).

    thinking_budget defaults to 0: Gemini 2.5 'thinking' otherwise consumes the
    output-token budget before the visible answer and truncates structured JSON
    (found via the live demo). Our accuracy comes from the manual in context, not
    from extended model reasoning, so we disable it. Pass a budget to re-enable."""
    client, mode = make_client(api_key)
    cfg: dict[str, Any] = {"max_output_tokens": max_output_tokens, "temperature": temperature}
    if system_instruction:
        cfg["system_instruction"] = system_instruction
    if cached_content:
        cfg["cached_content"] = cached_content
    if response_mime_type:
        cfg["response_mime_type"] = response_mime_type
    if thinking_budget is not None:
        cfg["thinking_config"] = {"thinking_budget": thinking_budget}

    resp = client.models.generate_content(model=model, contents=contents, config=cfg)
    text = (getattr(resp, "text", None) or "").strip()
    p_in, p_out, cached = _usage(resp)
    return GeminiResponse(
        text=text,
        model=model,
        prompt_tokens=p_in,
        completion_tokens=p_out,
        cached_tokens=cached,
        cost_usd=constants.cost_usd(
            model, prompt_tokens=p_in, completion_tokens=p_out, cached_tokens=cached
        ),
        auth_mode=mode,
        raw=resp,
    )


def generate_stream(contents, *, model: str, system_instruction=None, cached_content=None,
                    max_output_tokens: int = 1024, temperature: float = 0.4, api_key=None):
    """Yield text chunks as they arrive. Final chunk carries usage on `.usage_metadata`.
    Callers compute cost from the last chunk's usage via core.constants."""
    client, _ = make_client(api_key)
    cfg: dict[str, Any] = {"max_output_tokens": max_output_tokens, "temperature": temperature}
    if system_instruction:
        cfg["system_instruction"] = system_instruction
    if cached_content:
        cfg["cached_content"] = cached_content
    yield from client.models.generate_content_stream(model=model, contents=contents, config=cfg)


def _guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".pdf": "application/pdf", ".txt": "text/plain",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(ext, "application/octet-stream")


def file_part(path: str, *, mime_type: str | None = None):
    """Return an inline Part for a PDF/image to put into `contents` or a cache.

    Vertex AI does NOT support the Files API (`files.upload` is Developer-API
    only — confirmed in the Phase 0 spike), so we inline the bytes. For very
    large manuals on Vertex, switch to a GCS URI via Part.from_uri(gs://...)."""
    from google.genai import types

    with open(path, "rb") as fh:
        data = fh.read()
    return types.Part.from_bytes(data=data, mime_type=mime_type or _guess_mime(path))


def create_cache(*, model: str, contents, system_instruction=None, ttl_seconds: int = 3600,
                 display_name: str | None = None, api_key=None) -> str:
    """Create a context cache over `contents` (the machine PDF parts). Returns the
    cache resource name to pass as `cached_content`. Cache PDFs only — the small,
    editable system instruction is sent fresh per turn (plan §8, resolves crit 0.4)."""
    client, _ = make_client(api_key)
    cfg: dict[str, Any] = {"contents": contents, "ttl": f"{ttl_seconds}s"}
    if system_instruction:
        cfg["system_instruction"] = system_instruction
    if display_name:
        cfg["display_name"] = display_name
    cache = client.caches.create(model=model, config=cfg)
    return cache.name


def _l2_normalize(v: list[float]) -> list[float]:
    import math
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


# The default embedding model actually in use, after fallback resolution (a newer
# model may not be provisioned on every project — Phase 0 lesson). Cached so we
# don't re-probe a 404 on every call.
_RESOLVED_EMBED_MODEL: str | None = None


def active_embedding_model() -> str:
    """The embedding model in use (post-fallback), else the configured preferred one."""
    return _RESOLVED_EMBED_MODEL or constants.MODELS["embedding"]


def _is_not_found(exc) -> bool:
    s = str(exc)
    return getattr(exc, "code", None) == 404 or "404" in s or "NOT_FOUND" in s.upper()


def embed(
    texts,
    *,
    model: str | None = None,
    task_type: str = "RETRIEVAL_DOCUMENT",
    output_dimensionality: int | None = None,
    api_key: str | None = None,
):
    """Embed text with the Gemini embedding model (Vertex). Multilingual — handles
    Swedish. Pass a single str -> returns one vector; pass a list -> returns a list
    of vectors. `task_type` tunes the embedding space: RETRIEVAL_DOCUMENT for stored
    content, RETRIEVAL_QUERY for a user query, SEMANTIC_SIMILARITY for clustering.

    Uses constants.MODELS['embedding'] with graceful fallback through EMBED_FALLBACKS
    when a model 404s (not provisioned on this project); the winner is cached. An
    explicit `model` is used as-is. Truncated (dim < 3072) vectors are L2-normalized."""
    global _RESOLVED_EMBED_MODEL
    from google.genai import types

    one = isinstance(texts, str)
    items = [texts] if one else list(texts)
    if not items:
        return [] if not one else []
    dim = output_dimensionality or constants.EMBED_DIM
    client, _ = make_client(api_key)
    cfg = types.EmbedContentConfig(task_type=task_type, output_dimensionality=dim)

    if model:
        candidates = [model]
    elif _RESOLVED_EMBED_MODEL:
        candidates = [_RESOLVED_EMBED_MODEL]
    else:
        candidates = [constants.MODELS["embedding"], *constants.EMBED_FALLBACKS]

    last_exc = None
    for idx, cand in enumerate(candidates):
        try:
            vecs: list[list[float]] = []
            for i in range(0, len(items), 100):  # chunk: stay under Vertex per-request cap
                resp = client.models.embed_content(model=cand, contents=items[i:i + 100], config=cfg)
                vecs.extend(list(e.values) for e in resp.embeddings)
            if not model:
                _RESOLVED_EMBED_MODEL = cand  # remember the working default
            if dim < 3072:  # Matryoshka truncation -> renormalize (Google guidance)
                vecs = [_l2_normalize(v) for v in vecs]
            return vecs[0] if one else vecs
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc) and idx < len(candidates) - 1:
                logger.warning("Embedding model %r unavailable (404); falling back.", cand)
                last_exc = exc
                continue
            raise
    raise last_exc  # pragma: no cover


def health_check() -> dict:
    """JSON-serializable Gemini auth status (used by /healthz)."""
    project, location = _resolve_project_and_location()
    sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    has_adc = os.path.exists(
        os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    )
    has_sa = bool(sa_path) and os.path.exists(sa_path)
    has_key = bool(os.environ.get("GEMINI_API_KEY"))

    if project:
        mode, ready = "vertex", (has_adc or has_sa)
        reason = "OK" if ready else "ADC or GOOGLE_APPLICATION_CREDENTIALS required"
    elif has_key:
        mode, ready, reason = "api-key", True, "consumer API key (dev only)"
    else:
        mode, ready, reason = "none", False, "set GOOGLE_CLOUD_PROJECT or GEMINI_API_KEY"

    return {
        "mode": mode, "ready": ready, "reason": reason,
        "project": project, "location": location,
        "has_adc": has_adc, "has_sa_creds": has_sa, "has_api_key": has_key,
        "llm_model": constants.MODELS["flash"],
        "embedding_model": constants.MODELS["embedding"],          # configured / preferred
        # Actually-resolved model for THIS worker (null until its first embed call;
        # resolution is per-process). Differs from preferred only after a 404 fallback.
        "embedding_model_active": _RESOLVED_EMBED_MODEL,
        "embedding_dim": constants.EMBED_DIM,
    }
