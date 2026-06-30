"""``manage.py import_customer_profiles`` — load the POS analytics customer export into the
dashboard's CustomerProfile browse (P6).

Reads the analytics ``customers.json`` (``customerProfiles`` + ``customerRichDetail``) and upserts
one CustomerProfile per customer. Idempotent (keyed on customer name). ``--limit N`` imports the
top-N by spend (handy for a quick load); omit it to import all. The source files are external (POS
export, not committed — they carry customer names) so the owner passes ``--customers PATH``.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


def _cadence_days(first: str, last: str, orders: int) -> int | None:
    """Average days between orders across the first→last span (None if not computable)."""
    if not (first and last) or not orders or orders < 2:
        return None
    try:
        from datetime import date

        f = date.fromisoformat(first[:10])
        ll = date.fromisoformat(last[:10])
    except (ValueError, TypeError):
        return None
    span = (ll - f).days
    return max(1, round(span / (orders - 1))) if span > 0 else None


class Command(BaseCommand):
    help = "Import customer profiles from the analytics customers.json (+ optional baskets.json)."

    def add_arguments(self, parser):
        parser.add_argument("--customers", required=True, help="Path to customers.json")
        parser.add_argument("--limit", type=int, default=0, help="Import only the top-N by spend.")

    def handle(self, *args, **opts):
        from crm.models import CustomerProfile

        path = Path(opts["customers"])
        if not path.exists():
            raise CommandError(f"customers.json not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles = data.get("customerProfiles") or {}
        rich = data.get("customerRichDetail") or {}
        if not profiles:
            raise CommandError("no 'customerProfiles' key in the file")

        items = list(profiles.items())
        # Top-N by spend when --limit is given (else all).
        if opts["limit"]:
            items.sort(key=lambda kv: kv[1].get("TotalSpend", 0), reverse=True)
            items = items[: opts["limit"]]

        n = 0
        for name, p in items:
            r = rich.get(name) or {}
            CustomerProfile.objects.update_or_create(
                customer_key=name[:160],
                defaults={
                    "name": name[:160],
                    "orders": int(p.get("Orders", 0) or 0),
                    "total_spend": float(p.get("TotalSpend", 0) or 0),
                    "aov": float(p.get("AOV", 0) or 0),
                    "recency_days": _as_int(p.get("Recency")),
                    "cadence_days": _cadence_days(
                        str(p.get("FirstOrder", "")), str(p.get("LastOrder", "")),
                        int(p.get("Orders", 0) or 0),
                    ),
                    "segment": str(p.get("Segment", ""))[:40],
                    "persona": str(p.get("PersonaName", ""))[:80],
                    "cohort_month": str(p.get("CohortMonth", ""))[:16],
                    "medical_share": float(p.get("MedicalShare", 0) or 0),
                    "is_medical": float(p.get("MedicalShare", 0) or 0) >= 0.5,
                    "top_brand": str(p.get("TopBrand", ""))[:120],
                    "top_vendor": str(p.get("TopVendor", ""))[:120],
                    "first_order": str(p.get("FirstOrder", ""))[:32],
                    "last_order": str(p.get("LastOrder", ""))[:32],
                    "top_categories": p.get("TopCategories") or [],
                    "tier_by_category": p.get("TierByCategory") or {},
                    "favorites": _norm_favorites(r.get("topSkus")),
                    "favorite_brands": r.get("topBrands") or [],
                    "hourly_pattern": r.get("hourlyPattern") or [],
                    "day_pattern": r.get("dayOfWeekPattern") or [],
                    "store_affinity": r.get("storeAffinity") or [],
                },
            )
            n += 1
            if n % 1000 == 0:
                self.stdout.write(f"  …{n} imported")

        self.stdout.write(self.style.SUCCESS(f"Imported {n} customer profiles ({len(rich)} with rich detail)."))


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm_favorites(sku_list) -> list[dict]:
    """Normalize topSkus entries (real export uses 'Product Name'/'Brand'/'Units'/'Orders') to the
    flat lowercase shape the template + suggestion feed read: {product, brand, units, orders}."""
    out = []
    for s in sku_list or []:
        if not isinstance(s, dict):
            continue
        out.append({
            "product": s.get("Product Name") or s.get("product") or s.get("name") or "",
            "brand": s.get("Brand") or s.get("brand") or "",
            # Coerce to int — the POS export may ship these as strings; build_feed does arithmetic
            # on units, so a bare "6" would TypeError and crash the customer page render.
            "units": _as_int(s.get("Units") or s.get("units")) or 0,
            "orders": _as_int(s.get("Orders") or s.get("orders")) or 0,
        })
    return out
