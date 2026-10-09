"""Call metrics for the analytics page, straight from the durable ``VoiceCall`` log.

Six grouped queries whatever the volume (outcomes, stores, per-day, day-of-week x hour, durations, transfer
dispositions); nothing is looped per row. Days and hours are bucketed in the project's TIME_ZONE (the
stores' clock), never UTC, so a call at 23:50 Pacific lands on its own day.

Every number is a count of real rows. A window with no calls returns zeros that mean "no calls", which is
authoritative here (the voice DB is local), unlike the budtender-backed sections that can be unreachable.
"""

from __future__ import annotations

from datetime import date, timedelta
from math import ceil
from statistics import median

from django.db.models import Count
from django.db.models.functions import ExtractHour, ExtractWeekDay, TruncDate
from django.utils import timezone

from voice.models import VoiceCall

from .monitor import call_outcome_badge

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# A transfer was attempted when ``escalated`` is set; the disposition says how it ended.
TRANSFER_LABELS = {
    "connected": "Connected",
    "no_answer": "No answer",
    "declined": "Declined",
    "voicemail": "Went to voicemail",
    "unavailable": "Person unavailable",
    "not_attempted": "Ended without a clear result",
}


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return f"{s // 60}m {s % 60:02d}s"


def percentile(sorted_values: list[int], q: float) -> int | None:
    """Nearest-rank percentile (q in 0..1) of an already sorted list."""
    if not sorted_values:
        return None
    return sorted_values[max(ceil(q * len(sorted_values)) - 1, 0)]


def call_metrics(since, store: str = "") -> dict:
    """Everything the Calls section shows, for calls created at or after ``since`` (optionally one store)."""
    tz = timezone.get_current_timezone()
    qs = VoiceCall.objects.filter(created_at__gte=since)
    if store:
        qs = qs.filter(store=store)

    outcome_counts = {r["outcome"]: r["n"] for r in qs.values("outcome").annotate(n=Count("id"))}
    total = sum(outcome_counts.values())

    by_store = list(qs.exclude(store="").values("store").annotate(n=Count("id")).order_by("-n", "store"))

    by_day = {r["d"]: r["n"] for r in qs.annotate(d=TruncDate("created_at", tzinfo=tz)).values("d").annotate(n=Count("id"))}

    heat = [[0] * 24 for _ in DAYS]
    for r in (qs.annotate(dow=ExtractWeekDay("created_at", tzinfo=tz), hr=ExtractHour("created_at", tzinfo=tz))
              .values("dow", "hr").annotate(n=Count("id"))):
        heat[(r["dow"] - 2) % 7][r["hr"]] += r["n"]  # Django: 1 = Sunday ... 7 = Saturday -> Monday first

    durations = sorted(qs.exclude(duration_s__isnull=True).values_list("duration_s", flat=True))

    dispositions = {r["transfer_disposition"] or "not_attempted": r["n"]
                    for r in qs.filter(escalated=True).values("transfer_disposition").annotate(n=Count("id"))}
    attempts = sum(dispositions.values())
    connected = dispositions.get("connected", 0)

    outcomes = []
    for key, n in sorted(outcome_counts.items(), key=lambda kv: -kv[1]):
        label, color = call_outcome_badge(key)
        outcomes.append({"key": key, "label": label, "color": color, "n": n})

    return {
        "total": total,
        "outcomes": outcomes,
        "vendor_callbacks": outcome_counts.get("vendor_callback", 0),
        "vendor_direct": outcome_counts.get("vendor_direct", 0),
        "transfer_unavailable": outcome_counts.get("transfer_unavailable", 0),
        "by_store": by_store,
        "by_day": by_day,
        "heat": heat,
        "peak": _peak(heat),
        "duration": {"n": len(durations), "median": median(durations) if durations else None,
                     "p90": percentile(durations, 0.9)},
        "transfers": {
            "attempts": attempts,
            "connected": connected,
            "rate": round(connected / attempts, 4) if attempts else None,
            "rows": [{"key": k, "label": TRANSFER_LABELS.get(k, k), "n": n}
                     for k, n in sorted(dispositions.items(), key=lambda kv: -kv[1])],
        },
    }


def _peak(heat: list[list[int]]) -> dict | None:
    best = max(((n, d, h) for d, row in enumerate(heat) for h, n in enumerate(row)), default=(0, 0, 0))
    if best[0] == 0:
        return None
    return {"n": best[0], "day": DAYS[best[1]], "hour": best[2]}


def daily_series(by_day: dict[date, int], start: date, end: date) -> list[tuple[date, int]]:
    """Zero-filled (day, calls) for every local day in start..end: a day with no calls IS zero calls."""
    out, day = [], start
    while day <= end:
        out.append((day, by_day.get(day, 0)))
        day += timedelta(days=1)
    return out
