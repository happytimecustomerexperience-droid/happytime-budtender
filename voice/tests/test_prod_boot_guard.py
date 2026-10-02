"""The production (DEBUG=0) boot guard in config/settings.py, exercised in a fresh interpreter.

W5b: the default PHONE_HASH_PEPPER ("dev-pepper-change-me", public in this repo) passed the guard,
and nothing stopped the website's HHT_VOICE_TOKEN from being the budtender-DB token itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

VOICE_DIR = Path(__file__).resolve().parent.parent

PROD_ENV = {
    "DJANGO_DEBUG": "0",
    "DJANGO_SECRET_KEY": "prod-secret-key-for-the-boot-test",
    "VAPI_PRIVATE_KEY": "vapi-private",
    "VAPI_WEBHOOK_SECRET": "vapi-webhook",
    "HHT_BACKEND_TOKEN": "backend-token",
    "HHT_VOICE_TOKEN": "",
    "PHONE_HASH_PEPPER": "a-real-pepper-distinct-from-the-key",
}


def _boot(**overrides) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "PYTEST"))}
    env.update(PROD_ENV, **overrides)
    return subprocess.run(
        [sys.executable, "-c", "import config.settings"],
        cwd=VOICE_DIR, env=env, capture_output=True, text=True, timeout=60,
    )


def test_control_a_correct_prod_env_boots():
    result = _boot()
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("pepper", ["dev-pepper-change-me", ""])
def test_default_or_empty_pepper_refuses_to_boot(pepper):
    result = _boot(PHONE_HASH_PEPPER=pepper)
    assert result.returncode != 0
    assert "PHONE_HASH_PEPPER must be set" in result.stderr


def test_website_token_equal_to_backend_token_refuses_to_boot():
    result = _boot(HHT_VOICE_TOKEN="backend-token")
    assert result.returncode != 0
    assert "HHT_VOICE_TOKEN must differ from HHT_BACKEND_TOKEN" in result.stderr
    assert _boot(HHT_VOICE_TOKEN="website-token").returncode == 0
