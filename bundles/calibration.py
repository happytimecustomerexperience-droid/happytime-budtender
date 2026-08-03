"""Derive the online-order cap from what people actually buy.

A cap picked out of the air is either so low it rejects normal orders or so high
it isn't a control. So we compute it from real completed sales: pull detailed
transactions, sum each one into a basket total, and set the cap at a percentile of
that distribution.

The cap exists because nothing is paid up front — an unbounded order is unbounded
staff labour and real held inventory against no commitment. Setting it at p99 means
essentially every genuine basket goes through and only true outliers are stopped.

Stored per store in `Setting` (the model that exists for runtime-tunable knobs), so
the value tracks the business without a redeploy. `BUNDLE_MAX_ORDER_TOTAL` remains
the fallback for a store with no data yet, and a floor so a quiet week can never
calibrate the cap down to something that rejects ordinary orders.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from django.conf import settings

from budtender.models import Setting

logger = logging.getLogger(__name__)

KEY = "bundle_max_order_total"
WINDOW_DAYS = 90
PERCENTILE = 99
# Below this many baskets the percentile is noise, not a distribution.
MIN_SAMPLE = 50


def _fallback() -> float:
    return float(getattr(settings, "BUNDLE_MAX_ORDER_TOTAL", 300))


def _setting_key(location_slug: str) -> str:
    return f"{KEY}:{location_slug}"


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. No numpy — this runs in the web image.

    `math.ceil`, deliberately not `round()`: Python's round() is banker's rounding,
    so round(99.5) == 100, which made p99 silently return the maximum basket — the
    one value the cap most needs to exclude.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(pct / 100.0 * len(ordered))
    idx = max(0, min(len(ordered) - 1, rank - 1))
    return float(ordered[idx])


def basket_totals(rows: list[dict]) -> list[float]:
    """Sum `/reporting/transactions?includeDetail=true` rows into basket totals.

    One row per transaction, each carrying its line items. Refunds and voids come
    back with negative or zero totals and are dropped — a return is not a basket.
    """
    totals: list[float] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        items = row.get("items") or row.get("lineItems") or row.get("details") or []
        total = 0.0
        if isinstance(items, list) and items:
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    unit = float(it.get("unitPrice") or it.get("price") or 0)
                    qty = float(it.get("quantity") or 1)
                except (TypeError, ValueError):
                    continue
                total += unit * qty
        else:
            # Header-only feed: fall back to whatever total the row carries.
            for key in ("total", "totalPrice", "subtotal", "grandTotal"):
                try:
                    total = float(row.get(key) or 0)
                except (TypeError, ValueError):
                    total = 0.0
                if total:
                    break
        if total > 0:
            totals.append(round(total, 2))
    return totals


def distribution(location_slug: str, days: int = WINDOW_DAYS) -> dict:
    """Basket-total percentiles for one store. Never raises."""
    from budtender import dutchie

    now = datetime.now(timezone.utc)
    try:
        rows = dutchie.get_transactions_detailed(
            location_slug,
            (now - timedelta(days=days)).isoformat(),
            now.isoformat(),
        )
    except Exception:
        logger.warning("cap calibration: transaction pull failed for %s",
                       location_slug, exc_info=True)
        return {"store": location_slug, "sample": 0, "error": "pull_failed"}

    totals = basket_totals(rows)
    if not totals:
        return {"store": location_slug, "sample": 0}
    return {
        "store": location_slug,
        "sample": len(totals),
        "p50": percentile(totals, 50),
        "p90": percentile(totals, 90),
        "p95": percentile(totals, 95),
        "p99": percentile(totals, 99),
        "max": max(totals),
    }


def calibrate(location_slug: str, days: int = WINDOW_DAYS) -> dict:
    """Compute and persist the cap for one store. Returns the distribution."""
    dist = distribution(location_slug, days)
    sample = dist.get("sample", 0)
    if sample < MIN_SAMPLE:
        dist["applied"] = None
        dist["reason"] = f"sample {sample} < {MIN_SAMPLE}, keeping the current cap"
        return dist

    # Never calibrate BELOW the configured floor: a quiet quarter must not produce
    # a cap that rejects ordinary orders.
    cap = max(round(dist[f"p{PERCENTILE}"], 2), _fallback())
    try:
        Setting.objects.update_or_create(key=_setting_key(location_slug),
                                         defaults={"value": {"cap": cap, "sample": sample,
                                                             "percentile": PERCENTILE,
                                                             "window_days": days}})
    except Exception:
        # An unmigrated DB must not crash the weekly task; cap_for() falls back.
        logger.warning("cap calibration: could not persist for %s", location_slug, exc_info=True)
        dist["applied"] = None
        dist["reason"] = "could not write the setting"
        return dist
    dist["applied"] = cap
    logger.info("cap calibration %s: p%d=%.2f over %d baskets -> cap %.2f",
                location_slug, PERCENTILE, dist[f"p{PERCENTILE}"], sample, cap)
    return dist


def cap_for(location_slug: str) -> float:
    """The live cap for a store. Falls back to the setting when uncalibrated."""
    try:
        row = Setting.objects.filter(key=_setting_key(location_slug)).first()
    except Exception:
        return _fallback()
    if row and isinstance(row.value, dict):
        try:
            return max(float(row.value.get("cap") or 0), _fallback())
        except (TypeError, ValueError):
            pass
    return _fallback()
