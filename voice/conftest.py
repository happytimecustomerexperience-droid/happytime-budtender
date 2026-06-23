"""Pytest harness for happytime-voice. External calls (Gemini, Vapi) are mocked by
default so the suite runs offline, deterministic, and free — tests MUST pass without
live API keys (03-CONVENTIONS.md §5). A ``live`` marker opts into real calls.

Adapted from swedish-bot/conftest.py (the offline-by-default discipline), slimmed to
the voice domain.
"""

from __future__ import annotations

import os

# The fast offline test plane uses an in-memory SQLite DB (no DB server, no network)
# and dev settings (so the prod-fail-closed guard doesn't trip during collection).
# These MUST be set before settings is first imported below.
os.environ.setdefault("HHT_TEST_SQLITE", "1")
os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("ALLOW_NON_EU_RESIDENCY", "1")

import hashlib  # noqa: E402
import math  # noqa: E402

import pytest  # noqa: E402

from core.services import gemini as gemini_mod  # noqa: E402


class FakeGemini:
    """Deterministic, offline stand-in for core.services.gemini (no network)."""

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, contents, *, model, system_instruction=None, **kw):
        self.calls.append({"model": model, "contents": contents, "kw": kw})
        text = "OK"
        return gemini_mod.GeminiResponse(
            text=text,
            model=model,
            prompt_tokens=200,
            completion_tokens=max(len(text) // 4, 1),
            cached_tokens=0,
            cost_usd=0.0,
            auth_mode="mock",
        )

    def generate_stream(self, contents, *, model, **kw):
        class _Chunk:
            def __init__(self, t):
                self.text = t
                self.usage_metadata = None

        yield _Chunk("OK")

    def embed(
        self,
        texts,
        *,
        model=None,
        task_type="RETRIEVAL_DOCUMENT",
        output_dimensionality=None,
        api_key=None,
    ):
        dim = output_dimensionality or 768
        one = isinstance(texts, str)
        items = [texts] if one else list(texts)

        def _vec(t):
            buf, i = [], 0
            while len(buf) < dim:  # full-entropy: keep hashing to fill `dim`
                h = hashlib.sha256(f"{t}:{i}".encode()).digest()
                buf.extend((b / 255.0) * 2 - 1 for b in h)
                i += 1
            v = buf[:dim]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            return [x / n for x in v]  # normalized, like the real model

        out = [_vec(t) for t in items]
        return out[0] if one else out

    def health_check(self):
        return {"mode": "mock", "ready": True, "reason": "mock"}

    def active_embedding_model(self):
        return "mock-embedding"


@pytest.fixture
def mock_gemini(monkeypatch):
    fake = FakeGemini()
    for name in ("generate", "generate_stream", "embed", "health_check", "active_embedding_model"):
        monkeypatch.setattr(gemini_mod, name, getattr(fake, name))
    return fake


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield


@pytest.fixture(autouse=True)
def _semantic_off(settings):
    # Embedding search is ON in production but OFF by default in tests so unit tests
    # stay offline + deterministic. Semantic tests opt in explicitly.
    settings.SEMANTIC_SEARCH_ENABLED = False
