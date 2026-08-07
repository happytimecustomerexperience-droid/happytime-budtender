"""Sign in to the POS with DUTCHIE credentials, not Django ones.

The rule the owner asked for: you get access only if Dutchie says you are who you
say you are, at the store you picked. So Django's own password is never the gate —
every POS user is created with an UNUSABLE local password, which means there is no
Django credential to guess, phish or reset into the till.

Ordering is forced by Dutchie's API, not by preference: `EmployeeLogin` takes
`LocId`/`LspId`, so we cannot validate anybody until we know which store. Store
first, then username and password.

WHAT THIS BUYS AND WHAT IT DOES NOT. This is an IDENTITY GATE at shift start: we
prove the person against Dutchie, record their Dutchie `UserId` on the staff
session, and run the shift on the store's service credential. Dutchie's own audit
log will therefore still show the service account, not the individual — ours shows
the individual. Holding each budtender's personal Dutchie session for their whole
shift would fix that, but it would mean retaining their password to re-mint an
expired session, and a till that demands a password mid-sale. Not worth it.
"""
from __future__ import annotations

import logging

from django.contrib.auth.models import User

from dutchie.login import (DutchieAuthRejected, DutchieAuthUnavailable,
                           authenticate_employee)
from dutchie.stores import get_store

logger = logging.getLogger(__name__)

# One message for "no such user" and for "wrong password". Distinguishing them turns
# the login form into a free tool for discovering which employees exist.
BAD_CREDENTIALS = "That Dutchie username or password wasn't accepted for this store."
UNAVAILABLE = ("Can't reach Dutchie to verify your sign-in. This is not a wrong "
               "password — try again, and tell a manager if it persists.")


class LoginRejected(Exception):
    """Definite no. Safe to show the user."""


class LoginUnavailable(Exception):
    """No answer from Dutchie. MUST NOT fall back to a local password."""


def verify(store_key: str, username: str, password: str) -> dict:
    """Prove this person against Dutchie for this store, or raise.

    Fails closed on every ambiguous outcome. There is deliberately no local-password
    fallback: a fallback would mean an attacker who can make Dutchie unreachable can
    downgrade the whole POS to whatever Django passwords happen to exist.
    """
    username = (username or "").strip()
    if not username or not password:
        raise LoginRejected(BAD_CREDENTIALS)
    try:
        store = get_store(store_key)
    except Exception:
        raise LoginRejected("Unknown store.") from None

    try:
        got = authenticate_employee(
            store.pos_base_url or store.base_url, username, password,
            int(store.loc_id), int(store.lsp_id))
    except DutchieAuthRejected:
        # Log the attempt, never the credential.
        logger.info("dutchie auth rejected for store=%s", store_key)
        raise LoginRejected(BAD_CREDENTIALS) from None
    except DutchieAuthUnavailable as exc:
        logger.warning("dutchie auth unavailable for store=%s: %s", store_key, exc)
        raise LoginUnavailable(UNAVAILABLE) from None
    return got


def local_user_for(username: str) -> User:
    """The Django row that carries the session, with no usable password.

    Casefolded: `Ann` and `ann` are one employee, and letting them become two Users
    would split the shift log and the sales attribution.
    """
    handle = (username or "").strip().lower()[:150]
    user, created = User.objects.get_or_create(
        username=handle, defaults={"is_staff": False, "is_superuser": False})
    if created or user.has_usable_password():
        # Also strips a usable password off any pre-existing row, closing the old
        # Django-password door for accounts that predate Dutchie sign-in.
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user
