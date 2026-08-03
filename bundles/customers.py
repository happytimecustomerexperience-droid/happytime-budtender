"""Wire an online order to a Dutchie customer.

`cart_submit` requires an AcctId — an order with no customer attached cannot be
checked out, so every online order has to end up pointing at a real Dutchie guest.

The split of work is deliberate:

  * **at order time** (public, unauthenticated) we only *look up* by phone. A read
    is safe to expose; it tells us and the budtender straight away whether this
    shopper already has an account.
  * **at claim time** (POS, behind staff auth) we *create* the guest if there
    wasn't one. Every other Dutchie write in this repo sits behind staff auth, and
    an unauthenticated create endpoint is a guest-record spam vector.

Nothing here ever raises: a Dutchie outage must not stop someone placing an order.
It degrades to `unresolved`, and the budtender searches by hand as they do today.
"""
from __future__ import annotations

import logging

from budtender.models import PhoneCartDraft
from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import get_store

from .catalog import store_key_for

logger = logging.getLogger(__name__)


def _digits(value: str) -> str:
    d = "".join(c for c in str(value or "") if c.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def split_name(full_name: str) -> tuple[str, str]:
    """'Sam Reyes' -> ('Sam', 'Reyes'); a single token becomes the first name."""
    parts = [p for p in str(full_name or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0][:60], ""
    return parts[0][:60], " ".join(parts[1:])[:60]


def _client(location_slug: str) -> PosRegisterClient:
    return PosRegisterClient(get_store(store_key_for(location_slug)))


def _rows(payload) -> list[dict]:
    data = (payload or {}).get("Data") if isinstance(payload, dict) else None
    return [r for r in (data or []) if isinstance(r, dict)]


def _acct_of(row: dict):
    return row.get("Guest_id") or row.get("AcctId") or row.get("acct_id") or row.get("id")


def _name_of(row: dict) -> str:
    name = row.get("Name") or row.get("name") or ""
    if not name:
        name = f"{row.get('FirstName', '')} {row.get('LastName', '')}".strip()
    return str(name)[:160]


def lookup_by_phone(location_slug: str, phone: str) -> tuple[str, str, str]:
    """Find an existing Dutchie guest by phone.

    Returns (acct_id, name, status) where status is one of matched / new /
    unresolved. Matching is on DIGITS, because Dutchie stores phones in whatever
    shape the guest was created with — '(509) 555-1212' and '5095551212' are the
    same person and a string compare would miss.
    """
    digits = _digits(phone)
    if not digits:
        return "", "", PhoneCartDraft.Customer.UNRESOLVED
    try:
        rows = _rows(_client(location_slug).guest_search(digits))
    except Exception:
        logger.warning("customer lookup unavailable for %s", location_slug, exc_info=True)
        return "", "", PhoneCartDraft.Customer.UNRESOLVED

    for row in rows:
        for key in ("PhoneNo", "Phone", "phone", "CellPhone"):
            if _digits(row.get(key)) == digits:
                acct = _acct_of(row)
                if acct:
                    return str(acct), _name_of(row), PhoneCartDraft.Customer.MATCHED
    # A successful search that returned no phone match genuinely means "no account".
    return "", "", PhoneCartDraft.Customer.NEW


def attach(draft: PhoneCartDraft) -> None:
    """Stamp the customer resolution onto a draft at order time. Never raises."""
    acct, name, status = lookup_by_phone(draft.location_slug, draft.contact_phone)
    draft.dutchie_acct_id = str(acct or "")
    draft.customer_name = name
    draft.customer_status = status


def ensure_customer(draft: PhoneCartDraft) -> tuple[str, str, str]:
    """Resolve the draft to a real AcctId at claim time, creating one if needed.

    Returns (acct_id, name, how) where how is matched / created / failed. Only
    called from the authenticated POS claim path.
    """
    if draft.dutchie_acct_id:
        return draft.dutchie_acct_id, draft.customer_name or draft.pickup_name, "matched"

    # Re-check before creating: the shopper may have walked in and been created at
    # the door between placing the order and a budtender claiming it. Creating a
    # duplicate guest is worse than a wasted lookup.
    acct, name, status = lookup_by_phone(draft.location_slug, draft.contact_phone)
    if acct:
        return str(acct), name, "matched"
    if status == PhoneCartDraft.Customer.UNRESOLVED:
        return "", "", "failed"

    first, last = split_name(draft.pickup_name)
    if not first:
        return "", "", "failed"
    try:
        # DOB is deliberately empty — we never collect it online. The customer
        # shows ID at the counter, and the POS `start` path already creates guests
        # this way.
        gid = _client(draft.location_slug).create_guest(
            first_name=first, last_name=last, dob="",
            phone=draft.contact_phone, email=draft.contact_email or "",
        )
    except Exception:
        logger.warning("create_guest failed for draft %s", draft.draft_token, exc_info=True)
        return "", "", "failed"
    if not gid:
        return "", "", "failed"
    return str(gid), draft.pickup_name, "created"
