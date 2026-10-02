"""W5b fix 9 — the staff login (/admin/login/) locks out after 10 failures in 15 minutes."""

from __future__ import annotations

import time
import types

import pytest
from django.conf import settings
from django.urls import reverse

from dashboard import lockout

PASSWORD = "the-right-password-123"


@pytest.fixture
def owner(django_user_model, settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # speed only
    return django_user_model.objects.create_user(
        username="owner", password=PASSWORD, is_staff=True, is_superuser=True
    )


def _login(client, username, password, ip="203.0.113.5"):
    return client.post(
        reverse("admin:login"),
        {"username": username, "password": password, "next": "/admin/"},
        REMOTE_ADDR=ip,
    )


def _logged_in(client) -> bool:
    return "_auth_user_id" in client.session


@pytest.mark.django_db
def test_ten_bad_passwords_lock_the_account_even_for_the_right_one(client, owner):
    for _ in range(lockout.MAX_FAILURES):
        assert _login(client, "owner", "guess").status_code == 200
    resp = _login(client, "owner", PASSWORD, ip="198.51.100.77")  # a fresh IP: the username is locked
    assert resp.status_code == 200 and not _logged_in(client), "locked: the right password is refused"


@pytest.mark.django_db
def test_the_lock_lifts_after_the_window(client, owner, monkeypatch):
    for _ in range(lockout.MAX_FAILURES):
        _login(client, "owner", "guess")
    assert not _logged_in(client)

    later = time.time() + lockout.LOCK_S + 1
    monkeypatch.setattr("django.core.cache.backends.locmem.time", types.SimpleNamespace(time=lambda: later))
    resp = _login(client, "owner", PASSWORD)
    assert resp.status_code == 302 and _logged_in(client)


@pytest.mark.django_db
def test_one_ip_spraying_usernames_is_locked_too(client, owner):
    for i in range(lockout.MAX_FAILURES):
        _login(client, f"user{i}", "guess", ip="203.0.113.9")
    assert not _logged_in(client)
    _login(client, "owner", PASSWORD, ip="203.0.113.9")
    assert not _logged_in(client), "the spraying IP is locked"
    assert _login(client, "owner", PASSWORD, ip="198.51.100.1").status_code == 302 and _logged_in(client)


@pytest.mark.django_db
def test_nine_failures_still_allow_the_right_password(client, owner):
    for _ in range(lockout.MAX_FAILURES - 1):
        _login(client, "owner", "guess")
    assert _login(client, "owner", PASSWORD).status_code == 302 and _logged_in(client)


def test_lockout_is_the_only_backend_and_sessions_are_12h():
    assert settings.AUTHENTICATION_BACKENDS == ["dashboard.lockout.LockoutModelBackend"]
    assert settings.SESSION_COOKIE_AGE == 12 * 60 * 60


@pytest.mark.django_db
def test_dashboard_has_a_post_logout_control(client, owner):
    client.force_login(owner)
    body = client.get(reverse("dash-overview")).content.decode()
    form = body[body.index(f'action="{reverse("admin:logout")}"') - 40 :][:300]
    assert 'method="post"' in form and "csrfmiddlewaretoken" in form
