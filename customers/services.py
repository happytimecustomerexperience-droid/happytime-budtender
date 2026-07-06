"""Customer upsert + Dutchie-write audit helpers."""

import re

from .models import Customer, DutchieWriteAudit

_DOB_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")

# Scan keys we copy into the Customer record (only fills blanks).
_SCAN_FIELDS = (
    "first_name", "last_name", "phone", "mjstateidno",
    "address", "city", "state", "postal_code", "email",
)


def _phone_key(phone: str) -> str:
    # ponytail: Dutchie checkout phones are US; keep stdlib normalization until international data exists.
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else (phone or "").strip()


def upsert_customer(scan: dict, dutchie_acct_id=None) -> Customer:
    """Get-or-create a Customer by phone (preferred) or acct_id, filling blanks
    from the scan. `scan` is the raw OCR/lookup dict; stored verbatim in raw_scan.
    """
    scan = scan or {}
    phone = _phone_key(scan.get("phone") or "")
    if phone:
        scan = {**scan, "phone": phone}

    matches = []
    if phone:
        # ponytail: local Customer cache is small; replace with a DB phone_key column if this grows.
        matches = [c for c in Customer.objects.exclude(phone="").order_by("pk") if _phone_key(c.phone) == phone]
    obj = matches[0] if matches else None
    if obj is None and dutchie_acct_id is not None:
        obj = Customer.objects.filter(dutchie_acct_id=dutchie_acct_id).first()
    if obj is None:
        obj = Customer()

    if dutchie_acct_id is not None:
        obj.dutchie_acct_id = dutchie_acct_id
    if phone:
        obj.phone = phone

    for dup in matches[1:]:
        for field in _SCAN_FIELDS:
            if field == "phone":
                continue
            if not getattr(obj, field, "") and getattr(dup, field, ""):
                setattr(obj, field, getattr(dup, field))
        if obj.birth_date is None and dup.birth_date:
            obj.birth_date = dup.birth_date
        if obj.over_21 is None and dup.over_21 is not None:
            obj.over_21 = dup.over_21

    # Fill only blank string fields from the scan (don't clobber known data).
    for field in _SCAN_FIELDS:
        val = (scan.get(field) or "").strip()
        if val and not getattr(obj, field, ""):
            setattr(obj, field, val)

    if scan.get("birth_date") and obj.birth_date is None:
        obj.birth_date = scan["birth_date"]
    if scan.get("over_21") is not None and obj.over_21 is None:
        obj.over_21 = bool(scan["over_21"])

    if scan:
        obj.raw_scan = scan
    obj.save()
    for dup in matches[1:]:
        dup.delete()
    return obj


def record_write(store, action, ok, acct_id=None, shipment_id=None, summary="", username="") -> DutchieWriteAudit:
    """Append an immutable Dutchie-write audit row.

    `summary` must be PII-free; we truncate to 500 and strip obvious DOB-like
    tokens (YYYY-MM-DD / MM/DD/YYYY) defensively.
    """
    summary = _scrub(summary)[:500]
    return DutchieWriteAudit.objects.create(
        store=(store or "")[:120],
        action=(action or "")[:40],
        acct_id=acct_id,
        shipment_id=shipment_id,
        summary=summary,
        ok=bool(ok),
        username=(username or "")[:150],
    )


def _scrub(text: str) -> str:
    return _DOB_RE.sub("[redacted]", text or "")
