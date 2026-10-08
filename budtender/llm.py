"""The ONE place budtender calls a model (customer-memory notes, summaries, consolidation).

Owner rule: no AI step uses "thinking". Every request built here carries an explicit thinking-off
config for the configured model family, checked against the installed google-genai SDK types:
  * Gemini 2.5 Flash / Flash-Lite: ``ThinkingConfig(thinking_budget=0)`` (thinking disabled);
  * Gemini 3 Flash family:          ``ThinkingConfig(thinking_level=MINIMAL)`` (that family's "off";
                                     it has no budget-0 switch);
  * anything else (2.5 Pro and 3 Pro cannot turn thinking off; unknown/alias ids): REFUSED, so a
    model swap can never silently re-enable thinking. The caller skips the step.
``budtender/tests/test_memory_summaries.py`` fails if any other module in budtender/ or core/ calls
``generate_content`` itself. Only ever called from Celery tasks, never in a request path.
"""
from __future__ import annotations

import os
import re

DEFAULT_MODEL = "gemini-2.5-flash"
TIMEOUT_MS = 20_000

_BUDGET_ZERO = re.compile(r"^(models/)?gemini-2\.5-flash(-lite)?(-preview[\w.-]*|-\d{2,3}[\w.-]*)?$")
_LEVEL_MINIMAL = re.compile(r"^(models/)?gemini-3(\.\d+)?-flash(-lite)?([\w.-]*)?$")


def model_name() -> str:
    return (os.environ.get("HHT_MEMORY_LLM_MODEL") or DEFAULT_MODEL).strip()


def _key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def configured() -> bool:
    """A Gemini key is present (the memory steps are skipped silently without one)."""
    return bool(_key())


def thinking_off(model: str):
    """The thinking-OFF config for this model id; raises for a model that cannot turn it off."""
    from google.genai import types

    m = (model or "").strip().lower()
    if _BUDGET_ZERO.match(m):
        return types.ThinkingConfig(thinking_budget=0)
    if _LEVEL_MINIMAL.match(m):
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
    raise RuntimeError(f"model {model!r} has no known thinking-off switch; refusing to call it")


def generate_json(*, system: str, prompt: str, schema: dict, max_output_tokens: int,
                  temperature: float = 0.1) -> str:
    """One strict-JSON, thinking-off Gemini call. Returns the raw text; raises on any problem
    (callers swallow it and skip the step)."""
    from google import genai
    from google.genai import types

    key = _key()
    if not key:
        raise RuntimeError("no Gemini key")
    model = model_name()
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=thinking_off(model),
    )
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=TIMEOUT_MS))
    resp = client.models.generate_content(model=model, contents=prompt, config=config)
    return getattr(resp, "text", None) or ""
