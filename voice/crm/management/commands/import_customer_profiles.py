"""``manage.py import_customer_profiles`` — load the POS analytics customer export into the
dashboard's CustomerProfile browse (P6).

Reads the analytics ``customers.json`` (``customerProfiles`` + ``customerRichDetail``) and upserts
one CustomerProfile per customer. Idempotent (keyed on customer name). ``--limit N`` imports the
top-N by spend (handy for a quick load); omit it to import all. The source files are external (POS
export, not committed — they carry customer names) so the owner passes ``--customers PATH``.
"""

from __future__ import annotations

import json
from collections import Counter
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
        parser.add_argument("--customers", default=None, help="Path to a local customers.json")
        parser.add_argument("--url", default=None,
                            help="Fetch customers.json from this URL (e.g. the analytics repo's raw "
                                 "GitHub link). Defaults to the CUSTOMERS_JSON_URL env var — set that "
                                 "+ a nightly cron to auto-refresh the snapshot.")
        parser.add_argument("--limit", type=int, default=0, help="Import only the top-N by spend.")

    def handle(self, *args, **opts):
        import os

        from crm.models import CustomerProfile

        url = opts.get("url") or os.environ.get("CUSTOMERS_JSON_URL", "")
        if url:
            data = self._fetch(url)
        elif opts.get("customers"):
            path = Path(opts["customers"])
            if not path.exists():
                raise CommandError(f"customers.json not found: {path}")
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            raise CommandError("provide --customers PATH, --url URL, or set CUSTOMERS_JSON_URL")
        profiles = data.get("customerProfiles") or {}
        rich = data.get("customerRichDetail") or {}
        if not profiles:
            raise CommandError("no 'customerProfiles' key in the file")

        items = _dedupe_items_by_phone(profiles, rich)
        # Top-N by spend when --limit is given (else all), after phone dedupe.
        if opts["limit"]:
            items.sort(key=lambda kv: kv[1].get("profile", {}).get("TotalSpend", 0), reverse=True)
            items = items[: opts["limit"]]

        n = 0
        for name, grouped in items:
            p = grouped["profile"]
            r = grouped["rich"]
            phash = grouped.get("phone_hash")
            key = f"phone:{phash}" if phash else name[:160]
            lookup = {"phone_hash": phash} if phash else {"customer_key": key}
            stale = CustomerProfile.objects.filter(customer_key__in=grouped.get("source_keys", []))
            if phash:
                stale = stale.exclude(phone_hash=phash)
                stale.delete()
            CustomerProfile.objects.update_or_create(
                **lookup,
                defaults={
                    "customer_key": key,
                    "phone_hash": phash,
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

    def _fetch(self, url: str) -> dict:
        """Download customers.json from a URL (the analytics repo regenerates + commits it nightly)."""
        import requests

        self.stdout.write(f"Fetching customers.json from {url} …")
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as exc:
            raise CommandError(f"URL did not return JSON: {exc}") from exc


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _dedupe_items_by_phone(profiles: dict, rich: dict) -> list[tuple[str, dict]]:
    """Group export rows by hashed phone when present; otherwise keep name-key rows."""
    from crm.models import phone_hash

    groups: dict[tuple[str, str], list[tuple[str, dict, dict]]] = {}
    for name, p in profiles.items():
        r = rich.get(name) or {}
        raw_phone = _extract_phone(p) or _extract_phone(r)
        phash = phone_hash(raw_phone) if raw_phone else ""
        key = ("phone", phash) if phash else ("name", name)
        groups.setdefault(key, []).append((name, p or {}, r or {}))

    out = []
    for (kind, value), rows in groups.items():
        name, profile, rich_detail = _merge_rows(rows)
        out.append((name, {
            "profile": profile,
            "rich": rich_detail,
            "phone_hash": value if kind == "phone" else None,
            "source_keys": [n[:160] for n, _, _ in rows],
        }))
    return out


def _extract_phone(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    for key, value in row.items():
        k = str(key).lower()
        if "phone" not in k and "mobile" not in k and "cell" not in k:
            continue
        # ponytail: current POS export is US-only; use libphonenumber if international data shows up.
        digits = "".join(c for c in str(value or "") if c.isdigit())
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
    return ""


def _merge_rows(rows: list[tuple[str, dict, dict]]) -> tuple[str, dict, dict]:
    best_name, best_p, _ = max(rows, key=lambda row: float(row[1].get("TotalSpend", 0) or 0))
    orders = sum(int(p.get("Orders", 0) or 0) for _, p, _ in rows)
    spend = sum(float(p.get("TotalSpend", 0) or 0) for _, p, _ in rows)
    weighted_med = sum(float(p.get("MedicalShare", 0) or 0) * int(p.get("Orders", 0) or 0)
                       for _, p, _ in rows)
    profile = dict(best_p)
    profile.update({
        "Orders": orders,
        "TotalSpend": spend,
        "AOV": (spend / orders) if orders else float(best_p.get("AOV", 0) or 0),
        "Recency": min((v for _, p, _ in rows if (v := _as_int(p.get("Recency"))) is not None), default=None),
        "FirstOrder": min((str(p.get("FirstOrder")) for _, p, _ in rows if p.get("FirstOrder")), default=""),
        "LastOrder": max((str(p.get("LastOrder")) for _, p, _ in rows if p.get("LastOrder")), default=""),
        "MedicalShare": (weighted_med / orders) if orders else float(best_p.get("MedicalShare", 0) or 0),
        "TopCategories": _merge_top_categories(rows, spend),
        "TierByCategory": _merge_dicts(p.get("TierByCategory") for _, p, _ in rows),
    })
    rich_detail = _merge_rich([r for _, _, r in rows])
    return best_name, profile, rich_detail


def _merge_dicts(dicts) -> dict:
    merged = {}
    for d in dicts:
        if isinstance(d, dict):
            merged.update(d)
    return merged


def _merge_top_categories(rows: list[tuple[str, dict, dict]], total_spend: float) -> list[dict]:
    scores: Counter = Counter()
    for _, p, _ in rows:
        spend = float(p.get("TotalSpend", 0) or 0)
        for cat in p.get("TopCategories") or []:
            name = cat.get("category") or cat.get("Category")
            if not name:
                continue
            score = cat.get("revenue") or cat.get("Revenue")
            if score is None:
                score = spend * float(cat.get("share", cat.get("Share", 0)) or 0) / 100
            scores[str(name)] += float(score or 0)
    total = total_spend or sum(scores.values()) or 1
    return [{"category": k, "share": round(v / total * 100, 2)} for k, v in scores.most_common()]


def _merge_rich(rows: list[dict]) -> dict:
    best = next((r for r in rows if r), {})
    return {
        **best,
        "topSkus": _merge_favorites(r.get("topSkus") for r in rows),
        "topBrands": _merge_brands(r.get("topBrands") for r in rows),
    }


def _merge_favorites(lists) -> list[dict]:
    by_product: dict[tuple[str, str], dict] = {}
    for items in lists:
        for s in items or []:
            if not isinstance(s, dict):
                continue
            product = s.get("Product Name") or s.get("product") or s.get("name") or ""
            brand = s.get("Brand") or s.get("brand") or ""
            if not product:
                continue
            row = by_product.setdefault((product, brand), {"product": product, "brand": brand, "units": 0, "orders": 0})
            row["units"] += _as_int(s.get("Units") or s.get("units")) or 0
            row["orders"] += _as_int(s.get("Orders") or s.get("orders")) or 0
    return sorted(by_product.values(), key=lambda r: (r["units"], r["orders"]), reverse=True)


def _merge_brands(lists) -> list[dict]:
    counts: Counter = Counter()
    for items in lists:
        for b in items or []:
            name = (b.get("brand") or b.get("Brand")) if isinstance(b, dict) else b
            if name:
                counts[str(name)] += 1
    return [{"brand": k, "count": v} for k, v in counts.most_common()]


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
