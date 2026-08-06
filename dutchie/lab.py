"""Lab results (potency + terpenes) for the public storefront.

Two POS endpoints, same auth/session block as every other Dutchie call:

  POST /api/lab/GetLabDataFieldMapping  {session}              -> 59 rows
  POST /api/lab/LoadLabResultInventory  {BatchId, ...session}  -> 1 sample row

The mapping is the schema and the sample row is the data. A sample row carries
~200 columns and a real one is ~95% null, so normalisation is driven BY the
mapping (ValueField -> FriendlyName + UnitField + IsCannabinoid) and every null
is dropped. Rendering 180 empty rows is worse than rendering nothing.

The join key is `BatchId`, which every product_SearchV2 row already carries.

NOTE — the search row already has THCContent / CBDContent /
TotalTerpenesContent (+ their *UnitId). For a plain "how strong is it" badge you
do NOT need this module at all; call it only for the full cannabinoid/terpene
breakdown, the COA link, and the tested/harvest/package dates.

Fails soft by design: products without lab data are normal, so a miss, an error
or a bad session returns None rather than raising into a page render.
"""

from __future__ import annotations

import logging

from django.core.cache import cache

from .session import PosClient
from .stores import get_store

logger = logging.getLogger(__name__)

# The mapping is a static dictionary (it describes the schema, not a product),
# so it is cached hard. STALE_TTL keeps a Dutchie blip from blanking the menu.
MAP_TTL = 86_400        # 24h "fresh"
MAP_STALE_TTL = 604_800  # 7d fallback copy
MAP_LOCK_TTL = 30        # stampede lock: one worker pays for the cold fetch

# UNIT CODES — Dutchie sends these as integers (e.g. "THCUnit": 2), not strings.
#
# Only code 2 is resolved, and it is DERIVED, not guessed: in the captured
# sample THCAValue=48, THCValue=2.9 and TotalCannabinoidsValue=44.996, all unit
# 2. 48 * 0.877 + 2.9 = 44.996 exactly — the standard decarboxylation formula
# for total cannabinoids, which only closes at that magnitude if the figures are
# percentages of mass.
#
# EVERY OTHER CODE IS UNKNOWN and maps to "" on purpose. Do not fill these in
# from memory of other Dutchie tenants: a wrong unit on a potency number is a
# compliance problem, and a bare number with no unit is the safe failure.
_UNIT_CODES = {2: "%"}

# Pulled out of the lists and given their own top-level keys — they are summary
# figures, not another line item in a breakdown.
_TOTAL_FIELDS = {
    "TotalCannabinoidsValue": "total_cannabinoids",
    "TotalTerpenesValue": "total_terpenes",
}


class LabClient(PosClient):
    """POS host, same session/re-login/Result=false handling as the register."""

    base_origin = "https://ash.pos.dutchie.com"

    def _login_base(self) -> str:
        return self.store.pos_base_url or self.store.base_url

    def get_field_mapping(self) -> list[dict]:
        data = self.post("/api/lab/GetLabDataFieldMapping",
                         self.session_block(with_register=False))
        out = (data or {}).get("Data")
        return out if isinstance(out, list) else []

    def load_lab_result(self, batch_id: int) -> list[dict]:
        body = {"BatchId": int(batch_id), **self.session_block(with_register=False)}
        data = self.post("/api/lab/LoadLabResultInventory", body)
        out = (data or {}).get("Data")
        return out if isinstance(out, list) else []


def _client(store_key: str) -> LabClient:
    return LabClient(get_store(store_key))


def _unit(code) -> str:
    """Integer unit code -> display unit. Unknown code -> "" (never a guess)."""
    try:
        return _UNIT_CODES.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def _normalise_mapping(rows: list[dict]) -> dict[str, dict]:
    """GetLabDataFieldMapping rows -> {ValueField: {friendly, unit_field, is_cannabinoid}}."""
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        vf = (r.get("ValueField") or "").strip()
        if not vf:
            continue
        out[vf] = {
            "friendly": (r.get("FriendlyName") or vf).strip(),
            "unit_field": (r.get("UnitField") or "").strip(),  # "" for THCML/CBDML
            "is_cannabinoid": bool(r.get("IsCannabinoid")),
        }
    return out


def _map_key(store_key: str) -> str:
    return f"dutchie:labmap:{store_key}"


def field_mapping(store_key: str, *, force: bool = False) -> dict[str, dict]:
    """The 59-row lab field dictionary, cached. Never raises; {} on failure.

    Same shape as budtender.live_stock: a short "fresh" flag over a long-lived
    value, plus a stampede lock so a cold cache costs exactly one POS call.
    """
    ck = _map_key(store_key)
    try:
        if not force and cache.get(ck + ":fresh"):
            cached = cache.get(ck)
            if cached is not None:
                return cached
    except Exception:
        logger.debug("lab mapping cache read failed for %s", store_key, exc_info=True)

    lock = ck + ":lock"
    try:
        got_lock = force or cache.add(lock, "1", MAP_LOCK_TTL)
    except Exception:
        got_lock = True
    if not got_lock:
        try:
            stale = cache.get(ck)
        except Exception:
            stale = None
        return stale if stale else {}

    try:
        mapping = _normalise_mapping(_client(store_key).get_field_mapping())
    except Exception:
        logger.warning("lab field_mapping failed for %s", store_key, exc_info=True)
        mapping = {}
    finally:
        try:
            cache.delete(lock)
        except Exception:
            pass

    if mapping:
        try:
            cache.set(ck, mapping, MAP_STALE_TTL)
            cache.set(ck + ":fresh", "1", MAP_TTL)
        except Exception:
            logger.debug("lab mapping cache write failed for %s", store_key, exc_info=True)
        return mapping

    try:
        stale = cache.get(ck)
    except Exception:
        stale = None
    return stale if stale else {}


def _clean(value):
    """None and blank strings are 'not tested'. 0 is a real measurement — keep it."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _sort_key(entry: dict) -> float:
    try:
        return -float(entry["value"])
    except (TypeError, ValueError):
        return 0.0


def normalise(row: dict, mapping: dict[str, dict]) -> dict:
    """One LoadLabResultInventory row + the mapping -> the storefront dict.

    Driven entirely by the mapping, so a new analyte Dutchie adds shows up
    without a code change. Every null is dropped, at both levels.
    """
    cannabinoids: list[dict] = []
    terpenes: list[dict] = []
    totals: dict[str, dict] = {}

    for value_field, m in mapping.items():
        value = _clean(row.get(value_field))
        if value is None:
            continue
        entry = {"name": m["friendly"], "value": value,
                 "unit": _unit(row.get(m["unit_field"])) if m["unit_field"] else ""}
        total_key = _TOTAL_FIELDS.get(value_field)
        if total_key:
            totals[total_key] = entry
        elif m["is_cannabinoid"]:
            cannabinoids.append(entry)
        else:
            terpenes.append(entry)

    cannabinoids.sort(key=_sort_key)
    terpenes.sort(key=_sort_key)

    out = {
        "batch_id": _clean(row.get("BatchId")),
        "sample_id": _clean(row.get("SampleId")),
        "cannabinoids": cannabinoids,
        "terpenes": terpenes,
        # TestedDate is the COA date; THCDate is when the potency was posted and
        # is what actually gets populated in practice, so it is the fallback.
        "tested_date": _clean(row.get("TestedDate")) or _clean(row.get("THCDate")),
        "harvest_date": _clean(row.get("HarvestDate")),
        "package_date": _clean(row.get("PackageDate")),
        "expires": _clean(row.get("ExpirationDate")),
        "lab_name": _clean(row.get("LabName")),
        "lab_license": _clean(row.get("LabLicenseNumber")),
        "coa_url": _clean(row.get("LabResultUrl")),
        **totals,
    }
    # Drop the empty keys too — the UI checks `if lab.terpenes`, not lengths.
    return {k: v for k, v in out.items() if v not in (None, [], {}, "")}


def lab_result(store_key: str, batch_id) -> dict | None:
    """Normalised lab data for one BatchId, or None. NEVER raises.

    None means "no lab data for this batch", which is a completely normal state
    for a large share of inventory — the caller renders nothing and moves on.
    """
    if not batch_id:
        return None
    mapping = field_mapping(store_key)
    if not mapping:
        return None
    try:
        rows = _client(store_key).load_lab_result(int(batch_id))
    except Exception:
        logger.info("lab_result(%s, %s) failed", store_key, batch_id, exc_info=True)
        return None
    row = rows[0] if rows and isinstance(rows[0], dict) else None
    if not row:
        return None
    out = normalise(row, mapping)
    # Dates alone are not lab results; a card with no numbers is noise.
    if not (out.get("cannabinoids") or out.get("terpenes")
            or out.get("total_cannabinoids") or out.get("total_terpenes")):
        return None
    out.setdefault("batch_id", int(batch_id))
    return out
