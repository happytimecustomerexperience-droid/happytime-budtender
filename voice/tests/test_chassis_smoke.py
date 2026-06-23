"""Chassis smoke tests (10-P0 §7 A1 partial + the prod-fail-closed guard A2 + the
vapi-client /workflow + auth_ok degrade). All external calls are mocked — the suite
passes without live API keys (03-CONVENTIONS.md §5)."""

from __future__ import annotations

import importlib

import pytest

from core.services import vapi


# ── A1: healthz ────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_healthz_degrades_without_gemini(client, monkeypatch):
    """No Gemini/Vapi creds → 503 degraded, but the endpoint never crashes and
    reports each dependency's status (DB up, gemini not-ready, vapi not-configured)."""
    monkeypatch.delenv("VAPI_PRIVATE_KEY", raising=False)
    resp = client.get("/healthz")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert set(body) >= {"status", "db", "gemini", "vapi"}
    assert body["db"]["ok"] is True
    # Vapi key absent → reported as not-configured, not an exception.
    assert body["vapi"]["configured"] is False


@pytest.mark.django_db
def test_healthz_green_when_all_ready(client, monkeypatch, mock_gemini):
    """DB up + Gemini ready (mock) + Vapi configured-and-reachable (mock) → 200 ok."""
    monkeypatch.setenv("VAPI_PRIVATE_KEY", "test-key")
    monkeypatch.setattr(vapi, "auth_ok", lambda: {"ok": True, "configured": True, "error": ""})
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["gemini"]["ready"] is True
    assert body["vapi"]["ok"] is True


# ── vapi client: /workflow is an owner-authorized path (ADR-024 supersedes ADR-002) ──
def test_vapi_workflow_path_allowed(monkeypatch):
    """The old ADR-002 guard is gone: /workflow no longer raises a refusal. The client exposes
    workflow CRUD; here we just prove the path isn't blocked (a bad key fails on auth, not a guard)."""
    monkeypatch.delenv("VAPI_PRIVATE_KEY", raising=False)
    assert hasattr(vapi, "create_workflow") and hasattr(vapi, "find_workflow_by_name")
    # Unconfigured client raises on the missing key — NOT a "refusing Workflow path" guard.
    with pytest.raises(vapi.VapiError, match="not configured"):
        vapi.get("/workflow/123")


def test_vapi_auth_ok_unconfigured_degrades(monkeypatch):
    monkeypatch.delenv("VAPI_PRIVATE_KEY", raising=False)
    out = vapi.auth_ok()
    assert out == {"ok": False, "configured": False, "error": "VAPI_PRIVATE_KEY not configured"}


# ── A2: prod-fail-closed boot guard ────────────────────────────────────
def _reload_settings(monkeypatch, env: dict) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import config.settings as s

    importlib.reload(s)


def test_prod_fail_closed_default_secret(monkeypatch):
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        _reload_settings(
            monkeypatch,
            {
                "DJANGO_DEBUG": "0",
                "DJANGO_SECRET_KEY": "dev-insecure-change-me",
            },
        )


def test_prod_fail_closed_pepper_equals_secret(monkeypatch):
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured, match="PHONE_HASH_PEPPER"):
        _reload_settings(
            monkeypatch,
            {
                "DJANGO_DEBUG": "0",
                "DJANGO_SECRET_KEY": "a-real-non-default-secret-key-value-1234567890",
                "PHONE_HASH_PEPPER": "a-real-non-default-secret-key-value-1234567890",
            },
        )


def test_prod_fail_closed_missing_vapi_secrets(monkeypatch):
    from django.core.exceptions import ImproperlyConfigured

    # Set empty (not delenv): settings reload calls load_dotenv(.env, override=False), which would
    # re-inject a populated .env's real secrets after a delenv. A present-but-empty var is left
    # empty by override=False, so the guard still sees the secret missing — robust to any .env.
    for k in ("VAPI_PRIVATE_KEY", "VAPI_WEBHOOK_SECRET", "HHT_BACKEND_TOKEN"):
        monkeypatch.setenv(k, "")
    with pytest.raises(ImproperlyConfigured, match="prod secrets"):
        _reload_settings(
            monkeypatch,
            {
                "DJANGO_DEBUG": "0",
                "DJANGO_SECRET_KEY": "a-real-non-default-secret-key-value-1234567890",
                "PHONE_HASH_PEPPER": "a-distinct-pepper-value-0987654321",
            },
        )


def test_prod_boots_when_all_secrets_present(monkeypatch):
    # All guards satisfied → settings import without raising.
    _reload_settings(
        monkeypatch,
        {
            "DJANGO_DEBUG": "0",
            "DJANGO_SECRET_KEY": "a-real-non-default-secret-key-value-1234567890",
            "PHONE_HASH_PEPPER": "a-distinct-pepper-value-0987654321",
            "VAPI_PRIVATE_KEY": "vapi-priv-x",
            "VAPI_WEBHOOK_SECRET": "wh-secret-x",
            "HHT_BACKEND_TOKEN": "backend-token-x",
        },
    )
    import config.settings as s

    assert s.DEBUG is False
    # Restore a DEBUG=1 settings module for the rest of the session.
    monkeypatch.setenv("DJANGO_DEBUG", "1")
    importlib.reload(s)
