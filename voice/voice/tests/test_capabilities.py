"""The capability switchboard (voice/voice/capabilities.py): defaults, persistence, fail-closed,
and the guard that every declared switch is actually enforced somewhere."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from voice import capabilities as caps

VOICE_ROOT = Path(__file__).resolve().parents[2]  # voice/
_SKIP_DIRS = {"tests", "migrations", "__pycache__", "evals"}


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.delete(caps._CACHE_KEY)
    yield
    cache.delete(caps._CACHE_KEY)


def test_declared_keys_are_unique_and_well_formed():
    keys = [c.key for c in caps.CAPABILITIES]
    assert len(keys) == len(set(keys))
    for c in caps.CAPABILITIES:
        assert re.fullmatch(r"[a-z]+\.[a-z0-9_]+", c.key), c.key
        assert c.label and c.does and c.when_off, c.key


def test_every_gated_tool_exists():
    from voice.tools import TOOL_REGISTRY

    missing = [c.tool for c in caps.CAPABILITIES if c.tool and c.tool not in TOOL_REGISTRY]
    assert not missing, f"capabilities gate tools that are not registered: {missing}"


@pytest.mark.django_db
def test_defaults_apply_when_no_row_exists():
    for c in caps.CAPABILITIES:
        assert caps.is_enabled(c.key) is c.default, c.key


@pytest.mark.django_db
def test_set_enabled_persists_and_clears_the_cache():
    key = "tool.pair_upsell"
    assert caps.is_enabled(key) is True
    caps.set_enabled(key, False, by="owner")
    assert caps.is_enabled(key) is False
    assert caps.tool_allowed("pair_upsell") is False
    caps.set_enabled(key, True)
    assert caps.is_enabled(key) is True


@pytest.mark.django_db
def test_unknown_key_is_off_and_cannot_be_written():
    assert caps.is_enabled("tool.does_not_exist") is False
    with pytest.raises(KeyError):
        caps.set_enabled("tool.does_not_exist", True)


def test_unreadable_state_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(caps, "states", boom)
    assert caps.is_enabled("tool.faq_lookup") is False


def test_ungated_tool_is_allowed():
    assert caps.tool_allowed("notify_n8n") is True


def _source_files():
    for path in VOICE_ROOT.rglob("*.py"):
        if _SKIP_DIRS & set(path.relative_to(VOICE_ROOT).parts):
            continue
        if path.name == "capabilities.py":
            continue
        yield path


def test_every_capability_is_enforced():
    """A declared switch that no code checks would tell the owner something is off when it is
    not. Tool switches are enforced once, in the dispatcher (``tool_allowed``); every other key
    must appear as ``is_enabled("<key>")`` in real (non-test) code."""
    blob = "\n".join(p.read_text(encoding="utf-8") for p in _source_files())
    dispatcher = (VOICE_ROOT / "voice" / "tools" / "__init__.py").read_text(encoding="utf-8")
    unenforced = []
    for c in caps.CAPABILITIES:
        if c.tool:
            if "tool_allowed(" not in dispatcher:
                unenforced.append(c.key)
        elif f'is_enabled("{c.key}")' not in blob:
            unenforced.append(c.key)
    assert not unenforced, f"declared but never enforced: {unenforced}"
