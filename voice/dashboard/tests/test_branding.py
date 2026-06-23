"""P5 — brand theming scaffold (15-P5 §6 AC-3; §7.1).

Asserts: ``load_brand_tokens()`` parses ``brand/tokens.json``; ``brand_css_vars()`` emits valid
``--brand-*`` CSS variables; the ``provisional:true`` path renders the neutral fallback (never a
blocker); a missing/partial tokens file degrades safely; the context processor injects ``brand`` +
``brand_css_vars``. Real Happy Time hex/fonts/logo are DEFERRED (owner) — these tests cover the
scaffold, not the captured values. Expected values hand-authored (03-CONVENTIONS.md §5).
"""

from __future__ import annotations

import json

import pytest

from dashboard import branding


@pytest.fixture(autouse=True)
def _reset_brand_cache():
    branding.reset_cache()
    yield
    branding.reset_cache()


def test_load_brand_tokens_parses_repo_file():
    """The shipped brand/tokens.json parses and carries all the color keys + fonts + provisional."""
    tokens = branding.load_brand_tokens()
    assert "colors" in tokens and "fonts" in tokens
    for key in branding._HEX_KEYS:
        assert key in tokens["colors"]
    # The repo ships provisional (real assets owner-gated — brand/CAPTURE.md).
    assert tokens["provisional"] is True


def test_load_brand_tokens_cached():
    """Read once (module cache) — the second call returns the same object."""
    first = branding.load_brand_tokens()
    second = branding.load_brand_tokens()
    assert first is second


def test_brand_css_vars_emits_brand_variables():
    """brand_css_vars renders a :root block with every --brand-<color> + the font vars."""
    css = branding.brand_css_vars()
    assert css.startswith(":root{")
    assert "--brand-primary:" in css
    assert "--brand-bg:" in css
    assert "--brand-primary-fg:" in css  # underscore→hyphen normalization
    assert "--brand-font-heading:" in css
    assert "--brand-font-body:" in css
    assert css.rstrip().endswith("}")


def test_missing_file_degrades_to_fallback(monkeypatch):
    """A missing tokens file → the neutral fallback (never raises, never a blank theme)."""
    from pathlib import Path

    monkeypatch.setattr(branding, "_TOKENS_PATH", Path("/no/such/brand/tokens.json"))
    branding.reset_cache()
    tokens = branding.load_brand_tokens()
    assert tokens["colors"]["primary"] == branding._FALLBACK_TOKENS["colors"]["primary"]
    assert tokens["provisional"] is True


def test_partial_tokens_backfilled(tmp_path, monkeypatch):
    """A partially-filled tokens file (only primary captured) backfills the rest from the fallback —
    the renderer never KeyErrors mid-capture."""
    p = tmp_path / "tokens.json"
    p.write_text(json.dumps({"provisional": False, "colors": {"primary": "#abcdef"}}))
    monkeypatch.setattr(branding, "_TOKENS_PATH", p)
    branding.reset_cache()
    tokens = branding.load_brand_tokens()
    assert tokens["colors"]["primary"] == "#abcdef"  # the captured value
    assert tokens["colors"]["bg"] == branding._FALLBACK_TOKENS["colors"]["bg"]  # backfilled
    assert tokens["provisional"] is False


def test_malformed_json_degrades_to_fallback(tmp_path, monkeypatch):
    """A corrupt tokens file → the neutral fallback, never an exception."""
    p = tmp_path / "tokens.json"
    p.write_text("{ this is not json ")
    monkeypatch.setattr(branding, "_TOKENS_PATH", p)
    branding.reset_cache()
    tokens = branding.load_brand_tokens()
    assert tokens["colors"]["primary"] == branding._FALLBACK_TOKENS["colors"]["primary"]


def test_context_processor_shape():
    """The context processor returns both keys the templates need."""
    ctx = branding.context_processor(request=None)
    assert "brand" in ctx
    assert "brand_css_vars" in ctx
    assert isinstance(ctx["brand_css_vars"], str)


def test_css_vars_never_leak_cost_or_margin():
    """Leak-safe: the brand block is purely presentational — no cost/margin substring ever."""
    css = branding.brand_css_vars()
    assert "cost" not in css and "margin" not in css
