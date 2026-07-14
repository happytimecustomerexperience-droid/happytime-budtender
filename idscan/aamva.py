"""Parse the AAMVA PDF417 barcode payload (back of a US driver license / state ID)
into the id-scan field dict. Fully LOCAL — the barcode carries the same fields the
cloud OCR+LLM used to guess at, but exact and machine-readable.

Ref: AAMVA DL/ID Card Design Standard — data element IDs (DCS/DAC/DBB/DAQ/...).
"""
from __future__ import annotations

# AAMVA element ID -> our field. '_'-prefixed values are post-processed (date/sex).
_ELEMENTS = {
    "DCS": "last_name",    # family / last name
    "DAC": "first_name",   # first name
    "DAD": "middle_name",  # middle name(s)
    "DBB": "_dob",         # date of birth
    "DBA": "_exp",         # expiry
    "DAQ": "id_number",    # license / ID number
    "DAG": "address",      # street address
    "DAH": "address2",     # street address line 2
    "DAI": "city",
    "DAJ": "state",
    "DAK": "postal_code",
    "DBC": "_sex",         # 1=male 2=female
    "DAA": "_full_name",   # legacy: whole name in one field
}
_SEX = {"1": "male", "2": "female"}


def _date(v: str | None) -> str | None:
    """AAMVA 8-digit date -> YYYY-MM-DD. US is MMDDCCYY; Canada/newer CCYYMMDD."""
    v = (v or "").strip()
    if len(v) == 8 and v.isdigit():
        if v[:2] in ("19", "20"):            # CCYYMMDD
            return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
        return f"{v[4:8]}-{v[0:2]}-{v[2:4]}"  # MMDDCCYY (US)
    return None


def _clean(s):
    return s.strip().rstrip(",").strip() if isinstance(s, str) else s


def _postal_code(value: str | None) -> str | None:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    if len(digits) >= 9:
        return f"{digits[:5]}-{digits[5:9]}"
    return digits[:5] or None


def _split_full_name(full: str) -> tuple[str | None, str | None, str | None]:
    """DAA legacy field: 'LAST,FIRST,MIDDLE' or 'LAST FIRST MIDDLE'."""
    parts = [p.strip() for p in full.replace(",", " ").split() if p.strip()]
    if not parts:
        return None, None, None
    last = parts[0]
    first = parts[1] if len(parts) > 1 else None
    middle = " ".join(parts[2:]) or None
    return first, last, middle


def parse_aamva(payload: str) -> dict | None:
    """AAMVA payload string -> id-scan field dict, or None if not a parseable ID."""
    if not payload:
        return None
    raw: dict[str, str] = {}
    for line in payload.replace("\r", "\n").split("\n"):
        line = line.strip()
        if len(line) < 3:
            continue
        code, val = line[:3], line[3:].strip()
        field = _ELEMENTS.get(code)
        if field and val and field not in raw:
            raw[field] = val

    first, last, middle = raw.get("first_name"), raw.get("last_name"), raw.get("middle_name")
    if (not first or not last) and raw.get("_full_name"):
        f2, l2, m2 = _split_full_name(raw["_full_name"])
        first, last, middle = first or f2, last or l2, middle or m2
    if not first and not last:
        return None

    first, last, middle = _clean(first), _clean(last), _clean(middle)
    idno = _clean(raw.get("id_number"))
    return {
        "first_name": first,
        "last_name": last,
        "middle_name": middle,
        "birth_date": _date(raw.get("_dob")),
        "id_number": idno,
        # A driver's-license number is not a medical-registry ID.
        "mjstateidno": None,
        "id_expiration": _date(raw.get("_exp")),
        "state": _clean(raw.get("state")),
        "address": _clean(raw.get("address")),
        "city": _clean(raw.get("city")),
        "postal_code": _postal_code(raw.get("postal_code")),
        "address2": _clean(raw.get("address2")),
        "phone": None,
        "email": None,
        "gender": _SEX.get(raw.get("_sex", ""), None),
        "id_type": "driver_license",
        "accts_name": " ".join(filter(None, [first, last])),
    }
