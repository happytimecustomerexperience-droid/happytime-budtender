"""
Shared ORM read layer — the customer taste profile + product enrichment that the
in-store POS reads out of the same Postgres this app owns.

Before the merge the POS reached happytime over a raw ``psycopg`` connection
(``CUSTOMER_DB_DSN``); now it's one app + one database, so these are plain ORM
reads of ``CustomerProfile`` / ``Product``. Output shapes are byte-identical to
the old readers so ``pos.catalog`` / ``pos.views`` / the insights dashboards are
unchanged. Absent profile → ``None`` (the POS degrades to "history unavailable").
"""
from __future__ import annotations

import logging
import re

from django.core.cache import cache
from django.db.models import F

from .models import CustomerProfile, Product

logger = logging.getLogger(__name__)

_MISS = object()
_PROFILE_TTL = 300  # the menu re-renders per filter change; cache the taste profile

# POS store key -> happytime location_slug.
_HHT_LOC = {"yakima": "yakima", "pullman": "pullman", "mtvernon": "mount-vernon"}


def _phone_candidates(phone: str) -> list[str]:
    """Normalize a Dutchie phone to the E.164 formats happytime might store."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return []
    out = {phone.strip()}
    if len(digits) == 10:
        out.update({f"+1{digits}", f"1{digits}", digits})
    elif len(digits) == 11 and digits.startswith("1"):
        out.update({f"+{digits}", digits, digits[1:]})
    else:
        out.update({digits, f"+{digits}"})
    return [p for p in out if p]


def _top(affinity: dict, n: int = 5) -> list[str]:
    if not isinstance(affinity, dict):
        return []
    return [k for k, _ in sorted(affinity.items(), key=lambda kv: kv[1] or 0, reverse=True)[:n]]


def load_product_enrichment(store_key: str) -> dict:
    """``{str(product_id) | sku: {strain_type, terpene, effects, bucket, velocity,
    margin_pct, price_z, subcategory, image_url, price_was, flavors, potency_mg,
    thc_percent}}`` from ``budtender_product`` for this store. ``{}`` on error so
    the menu still renders from the live register rows."""
    slug = _HHT_LOC.get(store_key, store_key)
    try:
        rows = (Product.objects.filter(location_slug=slug)
                .values("product_id", "sku", "strain_type", "dominant_terpene", "effects",
                        "bucket", "velocity", "margin_pct", "price_z", "subcategory",
                        "image_url", "price_was", "flavors", "potency_mg", "thc_percent"))
        out: dict = {}
        for r in rows:
            rec = {
                "strain_type": r["strain_type"] or "", "terpene": r["dominant_terpene"] or "",
                "effects": r["effects"] or [], "bucket": r["bucket"] or "",
                "velocity": r["velocity"], "margin_pct": r["margin_pct"], "price_z": r["price_z"],
                "subcategory": r["subcategory"] or "", "image_url": r["image_url"] or "",
                "price_was": float(r["price_was"]) if r["price_was"] is not None else None,
                "flavors": r["flavors"] or [], "potency_mg": r["potency_mg"], "thc_percent": r["thc_percent"],
            }
            if r["product_id"]:
                out[str(r["product_id"])] = rec
            if r["sku"]:
                out.setdefault(str(r["sku"]), rec)
        return out
    except Exception:
        logger.debug("load_product_enrichment failed", exc_info=True)
        return {}


def load_profile_full(phone: str) -> dict | None:
    """Full affinity profile (for ranking + suggestions), or None. Keys match the
    engine / ``pos.suggest`` expectations."""
    cands = _phone_candidates(phone or "")
    if not cands:
        return None
    try:
        p = (CustomerProfile.objects.filter(phone__in=cands)
             .values("total_orders", "last_purchase_at", "price_tier", "novelty_score",
                     "brand_affinity", "category_affinity", "strain_type_affinity",
                     "subcategory_affinity", "terpene_affinity", "flavor_affinity",
                     "bucket_mix", "thc_min", "thc_max", "purchase_history").first())
    except Exception:
        logger.debug("load_profile_full failed", exc_info=True)
        return None
    if not p:
        return None
    return {
        "orders": int(p["total_orders"] or 0),
        "last_purchase": p["last_purchase_at"].isoformat()[:10] if p["last_purchase_at"] else None,
        "price_tier": p["price_tier"] or "",
        "novelty_score": p["novelty_score"],
        "brand_affinity": p["brand_affinity"] or {},
        "category_affinity": p["category_affinity"] or {},
        "strain_type_affinity": p["strain_type_affinity"] or {},
        "subcategory_affinity": p["subcategory_affinity"] or {},
        "terpene_affinity": p["terpene_affinity"] or {},
        "flavor_affinity": p["flavor_affinity"] or {},
        "bucket_mix": p["bucket_mix"] or {},
        "thc_min": p["thc_min"],
        "thc_max": p["thc_max"],
        "purchase_history": p["purchase_history"] or [],
    }


def load_customer_history(acct_id=None, phone=None, name=None):
    """Compact customer-360 panel dict (orders / last purchase / top cats+brands /
    recent items), or None. ``acct_id`` / ``name`` kept for signature stability."""
    cands = _phone_candidates(phone or "")
    if not cands:
        return None
    try:
        p = (CustomerProfile.objects.filter(phone__in=cands)
             .values("total_orders", "last_purchase_at", "price_tier", "novelty_score",
                     "brand_affinity", "category_affinity", "purchase_history").first())
    except Exception:
        logger.debug("load_customer_history failed", exc_info=True)
        return None
    if not p:
        return None
    hist = p["purchase_history"] or []
    recent = []
    if isinstance(hist, list):
        hist_sorted = sorted(hist, key=lambda h: (h or {}).get("last_bought_at") or "", reverse=True)
        for h in hist_sorted[:10]:
            if isinstance(h, dict):
                recent.append({
                    "product": h.get("sku") or h.get("product") or h.get("brand") or "—",
                    "brand": h.get("brand") or "",
                    "times": h.get("times_bought") or h.get("qty") or "",
                })
    return {
        "source": "happytime",
        "orders": int(p["total_orders"] or 0),
        "last_purchase": p["last_purchase_at"].isoformat()[:10] if p["last_purchase_at"] else None,
        "price_tier": p["price_tier"] or "",
        "novelty": round(float(p["novelty_score"]), 2) if p["novelty_score"] is not None else None,
        "top_categories": _top(p["category_affinity"]),
        "top_brands": _top(p["brand_affinity"]),
        "recent": recent,
        "matched_by": "phone",
    }


def load_all_profiles(limit: int = 250_000, ttl: int = 300) -> list:
    """All customer taste profiles for AGGREGATE manager analytics, or [] on error.
    Streams the FULL customer base (was capped at 5000) with .iterator so the whole
    book is aggregated, not a 5000-row sample; `limit` is only a memory safety valve.
    Cached (admin-only, infrequent), best-effort — never raises."""
    key = f"allprof:{int(limit)}"
    try:
        hit = cache.get(key, _MISS)
        if hit is not _MISS:
            return hit
    except Exception:
        pass
    try:
        rows = (CustomerProfile.objects.order_by(F("total_orders").desc(nulls_last=True))
                .values("total_orders", "last_purchase_at", "price_tier", "novelty_score",
                        "brand_affinity", "category_affinity", "strain_type_affinity",
                        "terpene_affinity", "flavor_affinity", "thc_min", "thc_max"))
        out = []
        for r in rows.iterator(chunk_size=2000):
            out.append({
                "orders": int(r["total_orders"] or 0),
                "last_purchase": r["last_purchase_at"].isoformat()[:10] if r["last_purchase_at"] else None,
                "price_tier": r["price_tier"] or "",
                "novelty_score": r["novelty_score"],
                "brand_affinity": r["brand_affinity"] or {},
                "category_affinity": r["category_affinity"] or {},
                "strain_type_affinity": r["strain_type_affinity"] or {},
                "terpene_affinity": r["terpene_affinity"] or {},
                "flavor_affinity": r["flavor_affinity"] or {},
                "thc_min": r["thc_min"],
                "thc_max": r["thc_max"],
            })
            if len(out) >= limit:   # ponytail: hard ceiling so a pathological DB can't OOM
                break
    except Exception:
        logger.debug("load_all_profiles failed", exc_info=True)
        return []
    try:
        cache.set(key, out, ttl)
    except Exception:
        pass
    return out
