"""Who is this person? Phone -> profile, first names, new-profile creation, the weekly merge.

A profile is keyed by phone. Dutchie only ever creates one for a phone it knows; a caller or
visitor we have never sold to gets a row created HERE (``source`` "voice" / "web") and nothing is
ever written to Dutchie. The same person can still end up on two rows (a number the store never
had, then a Dutchie guest on another number), so ``merge_duplicates`` folds a never-purchased
shell into the Dutchie row, and only on a stable id: a Dutchie account id that staff or sync tied to
both numbers. Never a name.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from .models import ChatSession, CustomerProfile, PhoneCartDraft, SuggestedProduct

logger = logging.getLogger(__name__)

# A phone that folds this many distinct Dutchie customer ids is a SHARED number (a store's own line
# typed in for walk-in guests, a "0000000000" placeholder): the row is not one person, so it never
# supplies a name or a taste. One person legitimately holds 1-3 ids (guest + account + a merge).
SHARED_DUTCHIE_IDS = 4
MAX_HOPS = 5  # a merge chain is one hop; the cap only stops a corrupt loop spinning forever
# One word of letters (any script), with ' or - inside. Anything else is not a first name.
_FIRST = re.compile(r"[^\W\d_](?:[^\W\d_]|['’-]){0,29}")


def first_name(name: object) -> str:
    """The first word of a stored name if it reads like a first name, else "" (a business, a
    "Last, First" row, digits). A caller is never greeted with something we are unsure of."""
    parts = str(name or "").split()
    tok = parts[0] if parts else ""
    if not _FIRST.fullmatch(tok):
        return ""
    return tok.capitalize() if tok.isupper() or tok.islower() else tok


def follow(profile: CustomerProfile | None) -> CustomerProfile | None:
    """The row a lookup should land on: the merge target, not the pointer a merge left behind."""
    for _ in range(MAX_HOPS):
        if profile is None or not profile.merged_into_id:
            break
        profile = profile.merged_into
    return profile


def _blocked_numbers() -> set[str]:
    """E.164 numbers that name no one: every store's own line plus ``HHT_NON_IDENTIFYING_PHONES``."""
    from django.conf import settings

    from .tasks import _normalize_phone

    raw = list(getattr(settings, "HHT_NON_IDENTIFYING_PHONES", None) or [])
    try:
        from bundles.catalog import STORES as STORE_FACTS

        raw += [s.get("phone", "") for s in STORE_FACTS.values()]
    except Exception:  # noqa: BLE001 - the junk-pattern checks below still apply
        logger.warning("identity: store phones unavailable for the non-identifying list", exc_info=True)
    return {n for n in (_normalize_phone(r) for r in raw) if n}


def non_identifying_phone(phone: object) -> bool:
    """True when this number cannot name one person: blank or not a US number, a number NANP never
    issues (area code or exchange starting 0/1), all one digit, a counting sequence, or a store's own
    line. A visitor typing one of these (or a stale default) must be treated as anonymous."""
    from .tasks import _normalize_phone

    e164 = _normalize_phone(phone or "")
    if not e164:
        return True
    d = e164[2:]
    if d[0] in "01" or d[3] in "01":
        return True
    if len(set(d)) == 1 or d in ("1234567890", "0123456789", "9876543210"):
        return True
    return e164 in _blocked_numbers()


def shared_profile(profile: CustomerProfile | None) -> bool:
    """A row that several Dutchie customers fold into: not a person, so never a name or a taste."""
    return profile is not None and len(profile.dutchie_ids or []) >= SHARED_DUTCHIE_IDS


def trusted(profile: CustomerProfile | None) -> CustomerProfile | None:
    """The profile only if it may be used to personalise or greet; None for a shared row."""
    return None if shared_profile(profile) else profile


def profile_for_phone(phone: object) -> CustomerProfile | None:
    """The person behind this phone, or None. None for a blank, junk, store or shared number: such a
    lookup must never land on a row and so never yields a name."""
    from .tasks import _normalize_phone  # tasks imports this module

    e164 = _normalize_phone(phone or "")
    if not e164 or non_identifying_phone(e164):
        return None
    return trusted(follow(CustomerProfile.objects.filter(phone=e164).select_related("merged_into").first()))


def ensure_profile(phone: object, source: str, name: object = "") -> tuple[CustomerProfile | None, bool]:
    """The profile for this phone, created as ``source`` when we have never seen it. A name is
    written only when the row has none: Dutchie's name wins whenever Dutchie has one."""
    from .tasks import _normalize_phone

    e164 = _normalize_phone(phone or "")
    if not e164 or non_identifying_phone(e164):
        return None, False
    profile = profile_for_phone(e164)
    created = False
    if profile is None:
        profile, created = CustomerProfile.objects.get_or_create(phone=e164, defaults={"source": source})
        profile = trusted(follow(profile))
        if profile is None:  # the number already belongs to a shared row: stay anonymous
            return None, False
    fn = first_name(name)
    if fn and not profile.name:
        profile.name = fn
        profile.save(update_fields=["name"])
    return profile, created


def _top(weights: dict | None, n: int = 3) -> list[str]:
    return [k for k, _ in sorted((weights or {}).items(), key=lambda kv: kv[1], reverse=True)[:n]]


def context(profile: CustomerProfile | None, created: bool = False, *, web: bool = False,
            vouched: object = "") -> dict:
    """What an agent may know about this person: customer-facing taste only, no raw history, no
    phone, no cost/margin. ``known`` means they have bought from us (a shell has not).

    ``web`` is a visitor who TYPED their number (nobody verified it). Carrier caller-ID on the phone
    line is trustworthy; a typed number is not, so on the website a first name is only handed back
    when the row is backed by real purchases, or when the visitor typed that same name in this very
    request (``vouched``). A name another visitor typed onto a never-purchased row ("Jaime" on a number
    two people use) is never echoed to whoever types that number next."""
    if profile is None or shared_profile(profile):
        return {"known": False, "created": False, "first_name": "", "has_history": False, "orders": 0,
                "days_since_last": None, "top_categories": [], "price_tier": "", "brands": [],
                "flavors": [], "terpenes": []}
    last = profile.last_purchase_at
    name = first_name(profile.name)
    return {
        "known": profile.total_orders > 0,
        "created": created,
        "first_name": name if (profile.total_orders > 0 or not web or (name and name == first_name(vouched))) else "",
        "has_history": profile.total_orders > 0,
        "orders": profile.total_orders,
        "days_since_last": (timezone.now() - last).days if last else None,
        "top_categories": _top(profile.category_affinity),
        "price_tier": profile.price_tier or "",
        "brands": _top(profile.brand_affinity),
        "flavors": _top(profile.flavor_affinity),
        "terpenes": _top(profile.terpene_affinity),
    }


def shared_phone(phone: object) -> bool:
    """True when this number's row (after a merge) is a shared one: it names nobody, so it must not
    key a lookup of anyone's sessions either (a family landline, a walk-in number)."""
    from .tasks import _normalize_phone

    e164 = _normalize_phone(phone or "")
    row = CustomerProfile.objects.filter(phone=e164).select_related("merged_into").first() if e164 else None
    return shared_profile(follow(row))


# A phone call's session token, as the voice service sends it (``vc-<Vapi call id>``).
_CALL_TOKEN = re.compile(r"vc-[A-Za-z0-9_-]{1,61}")


def link_session(token: object, profile: CustomerProfile | None, e164: str, via: str, *,
                 create_call: bool = False) -> None:
    """Tie an existing chat session to the customer (no-op for an unknown token or no profile).

    ``create_call`` (caller-context only: backend token + carrier caller-ID) also creates the
    session for a ``vc-<call id>`` token we have not seen, so the call's end-of-call memory learn
    (``memory/learn`` by ``call_id``) can find who called. A session that changes hands (now names a
    different customer) loses what it learned about the previous one (``ChatSession.learned``)."""
    token = str(token or "").strip()
    if not (token and profile is not None and not non_identifying_phone(e164) and not shared_profile(profile)):
        return
    if create_call and _CALL_TOKEN.fullmatch(token):
        ChatSession.objects.get_or_create(session_token=token, defaults={"channel": "voice"})
    rows = ChatSession.objects.filter(session_token=token)
    rows.filter(customer__isnull=False).exclude(customer=profile).update(learned={})
    rows.update(customer=profile, phone=e164, identity_via=via, last_active_at=timezone.now())
    # The session's earlier ANONYMOUS suggestions become this customer's (suggestion-analytics-v1);
    # rows already attributed to someone else stay theirs. Never fails the link.
    from . import suggestions

    suggestions.attach_sessions_safely(rows, profile, via)


def unlink_session(token: object, via: str) -> int:
    """Drop a session's ``via`` identification (the visitor typed a number that names nobody, or
    skipped): whoever is typing now must not keep being treated as the person typed before them,
    and what the session learned about that person (``learned``) goes with it."""
    token = str(token or "").strip()
    if not token:
        return 0
    return ChatSession.objects.filter(session_token=token, identity_via=via).update(
        customer=None, phone="", identity_via="", learned={}, last_active_at=timezone.now()
    )


def forget_session(token: object) -> int:
    """"Forget me": drop everything this session learned (``ChatSession.learned``), whatever its tier."""
    token = str(token or "").strip()
    return ChatSession.objects.filter(session_token=token).update(learned={}) if token else 0


# ── trust tier (docs/contracts/customer-memory-v1.md) ────────────────────────
# Carrier caller-ID (voice) and a future SMS-verified website number are proof of identity; a typed
# website number is not. The tier decides what memory a session may READ and where its learned facts
# are WRITTEN (budtender.memory / memory_learn): only "trusted" ever touches CustomerProfile.memory.
TRUSTED_VIA = ("caller_id", "web_verified")


def tier(session: ChatSession | None) -> str:
    """"trusted" | "unverified" | "anonymous" for this session, from ``identity_via`` alone (never
    from a name or a phone in a request). A shared or junk row is anonymous whatever the link says;
    a typed phone is "unverified" only while the owner's HHT_WEB_PHONE_IDENTITY switch is on."""
    from django.conf import settings

    if session is None or not session.customer_id:
        return "anonymous"
    if trusted(follow(session.customer)) is None or non_identifying_phone(session.phone):
        return "anonymous"
    via = session.identity_via or ""
    if via in TRUSTED_VIA:
        return "trusted"
    if via == "web_phone" and settings.HHT_WEB_PHONE_IDENTITY:
        return "unverified"
    return "anonymous"


# ── weekly merge ─────────────────────────────────────────────────────────────

def _merge(primary: CustomerProfile, secondary: CustomerProfile) -> None:
    with transaction.atomic():
        if not primary.name and secondary.name:
            primary.name = secondary.name
            primary.save(update_fields=["name"])
        if secondary.memory:  # what the same person told us on the shell row (trusted writes only)
            from . import memory

            learned = {k: v for k, v in (secondary.memory or {}).items() if k != "derived"}
            primary.memory = memory.merge(primary.memory, learned)
            primary.memory_updated_at = timezone.now()
            primary.save(update_fields=["memory", "memory_updated_at"])
            secondary.memory = {}
            secondary.save(update_fields=["memory"])
        ChatSession.objects.filter(customer=secondary).update(customer=primary)
        SuggestedProduct.objects.filter(customer=secondary).update(customer=primary)
        secondary.merged_into = primary
        secondary.save(update_fields=["merged_into"])


def merge_duplicates() -> dict:
    """Fold each never-purchased shell row into the Dutchie row it provably belongs to.

    Evidence for "same person" is a Dutchie account id seen with both phones: sync records the ids
    that fold into each Dutchie row; the POS scan cache and staff-claimed web/phone orders record
    which phone an account was matched to. Anything that is not exactly one Dutchie owner for an
    account, or a secondary that has history or Dutchie ids of its own, is left alone and counted.
    Re-running merges nothing new. The secondary's phone stays as a pointer, so a lookup by either
    number lands on one row and a Dutchie rebuild cannot resurrect the duplicate."""
    from customers.models import Customer as ScanCustomer

    from .tasks import _normalize_phone

    owners: dict[str, set[int]] = defaultdict(set)
    for p in CustomerProfile.objects.filter(merged_into__isnull=True).iterator():
        for acct in p.dutchie_ids or []:
            owners[str(acct)].add(p.pk)

    phones: dict[str, set[str]] = defaultdict(set)
    for acct, ph in (ScanCustomer.objects.exclude(dutchie_acct_id__isnull=True).exclude(phone="")
                     .values_list("dutchie_acct_id", "phone")):
        phones[str(acct)].add(_normalize_phone(ph))
    for acct, ph in (PhoneCartDraft.objects.exclude(dutchie_acct_id="").exclude(contact_phone="")
                     .values_list("dutchie_acct_id", "contact_phone")):
        phones[str(acct)].add(_normalize_phone(ph))

    merged = ambiguous = skipped = 0
    for acct, pks in owners.items():
        cands = {p for p in phones.get(acct, ()) if p}
        if not cands:
            continue
        if len(pks) != 1:
            ambiguous += 1
            continue
        primary = CustomerProfile.objects.get(pk=next(iter(pks)))
        for ph in cands - {primary.phone}:
            sec = CustomerProfile.objects.filter(phone=ph, merged_into__isnull=True).first()
            if sec is None or sec.pk == primary.pk:
                continue
            if sec.dutchie_ids or sec.purchase_history or sec.total_orders:
                skipped += 1  # has a purchase identity of its own: not a shell, never auto-merged
                continue
            _merge(primary, sec)
            merged += 1
            logger.info("merged profile ...%s into %s", ph[-4:], primary.pk)
    return {"merged": merged, "ambiguous": ambiguous, "skipped": skipped}
