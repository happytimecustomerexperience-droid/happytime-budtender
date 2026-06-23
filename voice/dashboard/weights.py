"""Ranking-weights read/write + budtender push (14-P4 §4.6 / §6.3).

The tuner persists the ``RankingWeights`` singleton ALWAYS (locally) and pushes it to budtender's
admin surface when reachable. budtender owns the ranking; this is the one place P4 touches its
ADMIN surface (the data-plane reads are P1's). When budtender does NOT expose
``/api/v1/admin/ranking-weights`` (O-1), the push is a documented no-op and the tuner shows
"saved locally; budtender sync pending".
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.utils import timezone

from dashboard.models import RankingWeights

logger = logging.getLogger(__name__)


def get_weights() -> RankingWeights:
    """The singleton, seeded with budtender's defaults on first load."""
    return RankingWeights.load()


def save_weights(*, w_anon: dict, w_known: dict, margin_emphasis: float) -> RankingWeights:
    """Persist the weights locally (owner override always wins — the form only WARNS on sum≠1)."""
    obj = RankingWeights.load()
    obj.w_anon = w_anon
    obj.w_known = w_known
    obj.margin_emphasis = margin_emphasis
    obj.save()
    return obj


def normalize(weights: dict) -> dict:
    """The normalized preview budtender will apply (sum→1.0). Owner override is saved RAW; this is
    only shown so the operator sees the effective weighting."""
    total = sum(float(v) for v in weights.values()) or 1.0
    return {k: round(float(v) / total, 4) for k, v in weights.items()}


def push_to_budtender(weights: RankingWeights) -> dict:
    """``POST {HHT_BUDTENDER_BASE_URL}/api/v1/admin/ranking-weights`` (Bearer HHT_BACKEND_TOKEN).

    Returns ``{"ok": bool, "reason": str, "applied": {...}}``. Degrades to a local-only no-op when
    the base URL/token is unset OR budtender doesn't expose the admin endpoint (404, O-1) — the
    weights stay persisted locally and the tuner shows "sync pending". Never raises."""
    base = (getattr(settings, "HHT_BUDTENDER_BASE_URL", "") or "").rstrip("/")
    token = getattr(settings, "HHT_BACKEND_TOKEN", "") or ""
    if not base or not token:
        return {"ok": False, "reason": "budtender not configured", "applied": {}}
    body = {
        "w_anon": weights.w_anon,
        "w_known": weights.w_known,
        "margin_emphasis": weights.margin_emphasis,
    }
    try:
        resp = requests.post(
            f"{base}/api/v1/admin/ranking-weights",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=(2.0, int(getattr(settings, "HHT_BUDTENDER_TIMEOUT", 8) or 8)),
        )
        if resp.status_code == 404:
            return {"ok": False, "reason": "endpoint not available", "applied": {}}
        if resp.status_code >= 300:
            return {"ok": False, "reason": f"budtender HTTP {resp.status_code}", "applied": {}}
        out = resp.json() if resp.content else {}
        weights.last_synced_at = timezone.now()
        weights.save(update_fields=["last_synced_at", "updated_at"])
        return {"ok": True, "reason": "synced", "applied": out.get("applied", body)}
    except (requests.Timeout, requests.ConnectionError) as exc:
        return {
            "ok": False,
            "reason": f"budtender unreachable ({type(exc).__name__})",
            "applied": {},
        }
    except Exception:  # noqa: BLE001 — a sync failure must never crash the tuner
        logger.warning("budtender weights push failed", exc_info=True)
        return {"ok": False, "reason": "push failed", "applied": {}}
