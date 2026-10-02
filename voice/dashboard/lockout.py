"""Brute-force lockout for the staff login (``/admin/login/`` — every dashboard view is
``staff_member_required``, so the admin login is the only door).

Failed logins are counted per username and per client IP in the shared cache, off Django's
``user_login_failed`` signal. ``MAX_FAILURES`` inside ``WINDOW_S`` locks that username, or that IP,
for ``LOCK_S``. ``LockoutModelBackend`` is the ONLY entry in ``AUTHENTICATION_BACKENDS``: while a
lock stands it returns None — so a plain ``ModelBackend`` listed after it would let a locked login
straight through, and must never be added back.

Trade-off: anyone can lock a known username out for ``LOCK_S`` by failing ten times — the price of
not letting its password be guessed.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.signals import user_login_failed
from django.core.cache import cache
from django.dispatch import receiver

logger = logging.getLogger(__name__)

MAX_FAILURES = 10
WINDOW_S = 15 * 60
LOCK_S = 15 * 60


def _client_ip(request) -> str:
    """The proxy's view of the client: the last X-Forwarded-For hop, else REMOTE_ADDR. Never a
    caller-supplied header like ``X-HHT-Client-IP`` — this door is unauthenticated."""
    if request is None:
        return ""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (xff.split(",")[-1].strip() if xff else request.META.get("REMOTE_ADDR", "")) or ""


def _subjects(username, request) -> list[str]:
    subjects = []
    if username:
        subjects.append(f"user:{str(username).strip().casefold()[:150]}")
    ip = _client_ip(request)
    if ip:
        subjects.append(f"ip:{ip[:64]}")
    return subjects


def is_locked(username, request) -> bool:
    return any(cache.get(f"login_lock:{s}") for s in _subjects(username, request))


@receiver(user_login_failed)
def _count_failure(sender, credentials=None, request=None, **kwargs):
    username = (credentials or {}).get(get_user_model().USERNAME_FIELD)
    for subject in _subjects(username, request):
        key = f"login_fail:{subject}"
        try:
            cache.add(key, 0, timeout=WINDOW_S)
            count = cache.incr(key)
        except ValueError:  # expired between add and incr
            cache.set(key, 1, timeout=WINDOW_S)
            count = 1
        if count >= MAX_FAILURES and cache.add(f"login_lock:{subject}", True, timeout=LOCK_S):
            logger.warning("staff login locked for %ss after %s failures: %s", LOCK_S, count, subject)


class LockoutModelBackend(ModelBackend):
    """``ModelBackend`` that refuses to authenticate a locked username or client IP."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if is_locked(username, request):
            return None
        return super().authenticate(request, username=username, password=password, **kwargs)
