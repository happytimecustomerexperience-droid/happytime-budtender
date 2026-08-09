"""Sign in to the POS with DUTCHIE credentials, not Django ones.

The rule the owner asked for: you get access only if Dutchie says you are who you
say you are. So Django's own password is never the gate — every POS user is created
with an UNUSABLE local password, which means there is no Django credential to guess,
phish or reset into the till.

Ordering is forced by Dutchie's API, not by preference: `EmployeeLogin` takes
`LocId`/`LspId`, so we cannot even call it until we know which store. Store first,
then username and password.

THE STORE PICKER IS NOT A PERMISSION. A live probe sent LocId 3501 and got LocId
3498 back — Dutchie does not scope EmployeeLogin to the location we pass, so a
success says WHO someone is and nothing about WHERE they may stand. Treating the
picker as an entitlement would be inventing a guarantee the API never gave.

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

from dutchie.login import (LOGIN_TO_POS, DutchieAuthRejected, DutchieAuthUnavailable,
                           authenticate_employee, employee_permissions)
from dutchie.stores import get_store

logger = logging.getLogger(__name__)

# One message for "no such user" and for "wrong password". Distinguishing them turns
# the login form into a free tool for discovering which employees exist.
BAD_CREDENTIALS = "That Dutchie username or password wasn't accepted for this store."
UNAVAILABLE = ("Can't reach Dutchie to verify your sign-in. This is not a wrong "
               "password — try again, and tell a manager if it persists.")
# Distinct from BAD_CREDENTIALS on purpose: the password was RIGHT. Telling someone
# to re-type it would waste their shift on a problem only a manager can fix.
NO_POS_ACCESS = ("Your Dutchie account isn't allowed to use a register at this store. "
                 "Ask a manager to grant POS access in Dutchie.")


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

    # One call on the employee's OWN session, before we drop it. This is the only
    # moment we can ask — Dutchie refuses to report permissions for anyone but the
    # authenticated user, so the shared service credential cannot look staff up.
    perms = employee_permissions(store.base_url, got, lsp_id=int(store.lsp_id),
                                 loc_id=int(store.loc_id), org_id=int(store.org_id))
    if perms is not None and LOGIN_TO_POS not in perms:
        # A DEFINITE no from Dutchie: this account is not allowed on a register.
        logger.info("dutchie denies %s POS access at store=%s", "user", store_key)
        raise LoginRejected(NO_POS_ACCESS)
    got["permissions"] = sorted(perms) if perms is not None else []
    got["permissions_known"] = perms is not None
    # The session was borrowed for that one question; the shift runs on the store's
    # service credential from here.
    got.pop("cookie_header", None)
    return got


# Membership of this group PINS someone to the door, whatever their browser posts.
# Managed from Django admin (Groups) — the migration creates it empty, so adding it
# changed nothing for anyone until a manager actually puts a name in it.
DOOR_ONLY_GROUP = "door-only"


def role_for(user: User, requested: str) -> str:
    """The role this shift actually runs at. Never simply the browser's word.

    The picker on the sign-in form is a MODE, not a permission: `budtender` is the
    default and always was, so anyone with valid Dutchie credentials could reach
    checkout by not choosing `door`. The eight `_require_not_door` guards on cart,
    claim and checkout only ever bound people who opted into being bound.

    CORRECTED 2026-08-09. An earlier version of this docstring said Dutchie has no
    permission data. That was true of the EmployeeLogin RESPONSE and false of
    Dutchie: `/api/permissions/getV2` returns a real per-user permission set (408
    entries for our service account). `verify()` now reads it at sign-in and refuses
    anyone Dutchie says may not use a register.

    It still cannot settle door-vs-budtender, for a concrete reason: we have exactly
    one Dutchie credential to look at, and it holds `Administrator`. Until a real
    door employee signs in we do not know WHICH permission separates them — every
    candidate (`SaveOrders`, `POSManager`, `EditPOSCustomerStatus`) is a guess, and a
    guessed permission gate either locks out budtenders or admits door staff. The
    permission set is recorded on every shift so the answer arrives as data. Until
    then the group below is the honest source, and it is one a manager controls.

    Deliberately one-directional: the group can only ever REMOVE the ability to
    sell. There is no group that grants it, because that would mean a mistake in
    Django admin silently hands someone the till.
    """
    if user.is_superuser:
        return "admin"                       # server-side, never the client's word
    if user.groups.filter(name=DOOR_ONLY_GROUP).exists():
        return "door"                        # pinned; posting role=budtender does nothing
    return requested if requested in ("budtender", "door") else "budtender"


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
