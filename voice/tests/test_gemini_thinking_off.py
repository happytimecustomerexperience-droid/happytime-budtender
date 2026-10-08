"""Owner rule: no server-side AI step thinks. Both generation paths send thinking_budget=0 by default."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def captured(monkeypatch):
    from core.services import gemini

    seen: dict = {}

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            seen["generate"] = config
            return SimpleNamespace(text="ok", usage_metadata=None)

        def generate_content_stream(self, *, model, contents, config):
            seen["stream"] = config
            yield SimpleNamespace(text="ok", usage_metadata=None)

    monkeypatch.setattr(
        gemini,
        "make_client",
        lambda api_key=None, *, force_api_key=False: (SimpleNamespace(models=FakeModels()), "api-key"),
    )
    return seen


def test_generate_and_generate_stream_both_turn_thinking_off(captured):
    from core.services import gemini

    gemini.generate("hi", model="gemini-2.5-flash")
    list(gemini.generate_stream("hi", model="gemini-2.5-flash"))

    assert captured["generate"]["thinking_config"] == {"thinking_budget": 0}
    assert captured["stream"]["thinking_config"] == {"thinking_budget": 0}


def test_generate_stream_budget_is_overridable_like_generate(captured):
    from core.services import gemini

    list(gemini.generate_stream("hi", model="gemini-2.5-flash", thinking_budget=512))
    assert captured["stream"]["thinking_config"] == {"thinking_budget": 512}
    list(gemini.generate_stream("hi", model="gemini-2.5-flash", thinking_budget=None))
    assert "thinking_config" not in captured["stream"]
