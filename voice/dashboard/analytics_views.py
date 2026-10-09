"""The staff analytics page (``/dashboard/analytics/``): do AI suggestions turn into purchases, plus calls,
the chat funnel and demand signals, on one page behind one filter row.

Sources (nothing is recomputed that another service already computes):
  * Suggestion outcomes  - budtender ``POST /api/v1/analytics/suggestions`` (docs/contracts/suggestion-analytics-v1.md).
    ``conversion_rate = bought_any / (bought_any + not_bought)``; ``unattributable`` is excluded and ``pending``
    is not decided yet, so neither is ever folded into "not bought".
  * Chat funnel and zero-result searches - budtender ``POST /api/v1/analytics/funnel`` (the same call the
    Chat funnel page makes).
  * Calls - the local ``VoiceCall`` log (``analytics_calls``).
A budtender-backed section that cannot be fetched says so; it never shows zeros as if they were data.

What an operator can act on, and so what this page leads with (a short pass over dispensary KPI write-ups,
2026-10): (1) conversion of recommendations to purchases, split exact vs sibling, because that is the only
number that says the AI earns its keep - it is by channel, store, category and rank so a bad channel or a
weak rank-3 pick is visible; (2) which products we push and which are pushed but never bought; (3) when
the phones are busy (day x hour in store time) and how many calls end in a person, a callback or a miss;
(4) what shoppers look for and do not find (zero-result searches). Deliberately NOT here because the data
cannot support it: revenue per recommendation (only the matched line total exists), margin or cost (never
exposed), repeat-customer rate, and period-over-period deltas (no prior-window fetch). Top FAQ gaps and
deals asked have no existing source (``faq_lookup`` logs mix real gaps with deliberate refusals), so they
are not shown.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from math import ceil

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone

from . import analytics_calls, views

STORES = (("yakima", "Yakima"), ("mount-vernon", "Mount Vernon"), ("pullman", "Pullman"))
CHANNELS = (("phone", "Phone"), ("chat", "Chat"), ("questionnaire", "Questionnaire"),
            ("similar", "Find similar"), ("pairing", "Pairing"))
_CHANNEL_LABELS = {**dict(CHANNELS), "menu": "Menu", "unknown": "Unknown"}
_STORE_LABELS = dict(STORES)
_KIND_LABELS = {"primary": "Main pick", "pairing": "Pairing"}
PRESETS = (7, 30, 90)
WEEKLY_AFTER_DAYS = 120  # a daily column needs >= ~5px of its own; beyond this the trend is drawn per week

# (api key, label, css class) - bottom to top in a stack, left to right in the legend.
STATUSES = (
    ("bought_exact", "Bought exact", "c-exact"),
    ("bought_sibling", "Bought sibling", "c-sibling"),
    ("not_bought", "Not bought", "c-not"),
    ("pending", "Pending", "c-pending"),
    ("unattributable", "Unattributable", "c-unattr"),
)
# How the session was matched to a customer (budtender ``identity_via``) and whether to trust it.
_IDENTITY = {"caller_id": ("Caller ID", True), "web_verified": ("Verified on website", True),
             "web_phone": ("Typed on website", False)}
_PRODUCT_SORTS = {
    "times_suggested": "times_suggested", "customers": "customers", "name": "name", "brand": "brand",
    "category": "category", "price": "price", "thc": "thc_percent", "bought_exact": "bought_exact",
    "bought_sibling": "bought_sibling", "not_bought": "not_bought", "conversion": "conversion_rate",
    "last": "last_suggested_at",
}
SUGGESTIONS_PATH = "/api/v1/analytics/suggestions"
FUNNEL_PATH = "/api/v1/analytics/funnel"


# ── filters ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Filters:
    days: int
    store: str
    channel: str

    @property
    def preset(self) -> str:
        return str(self.days) if self.days in PRESETS else "custom"

    def payload(self, **extra) -> dict:
        """The budtender request body: the days window plus every filter that is set (unset ones are omitted)."""
        body = {"days": self.days}
        if self.store:
            body["store"] = self.store
        if self.channel:
            body["channel"] = self.channel
        return {**body, **extra}


def parse_filters(params) -> Filters:
    """``days`` is 7/30/90, ``custom`` (then ``custom_days``) or any whole number (older links); bounded 1-365."""
    raw = (params.get("days") or "").strip()
    if raw == "custom":
        raw = params.get("custom_days") or ""
    store = (params.get("store") or "").strip()
    channel = (params.get("channel") or "").strip()
    return Filters(
        days=views._bounded_int(raw, default=30, lo=1, hi=365),
        store=store if store in _STORE_LABELS else "",
        channel=channel if channel in dict(CHANNELS) else "",
    )


# ── small formatters ──────────────────────────────────────────────────────────
def pct(rate) -> str:
    return "-" if rate is None else f"{rate * 100:.1f}%"


def conversion(bought_exact: int, bought_sibling: int, not_bought: int):
    """The contract's formula, for the rows this page sums itself: bought / (bought + not bought)."""
    decided = bought_exact + bought_sibling + not_bought
    return round((bought_exact + bought_sibling) / decided, 4) if decided else None


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return ""


def _thc(value) -> str:
    try:
        return f"{float(value):g}%"
    except (TypeError, ValueError):
        return ""


def _counts(row: dict) -> dict:
    return {key: _int(row.get(key)) for key, _label, _css in STATUSES} | {"suggested": _int(row.get("suggested"))}


# ── suggestion section ────────────────────────────────────────────────────────
def _group_rows(rows, labels: dict | None = None) -> list[dict]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        c = _counts(r)
        key = str(r.get("key", ""))
        rate = r.get("conversion_rate")
        out.append({**c, "key": key, "label": (labels or {}).get(key) or r.get("label") or key or "(none)",
                    "rate": pct(rate), "bar": round((rate or 0) * 100, 1), "has_rate": rate is not None})
    return out


def _rank_rows(rows) -> tuple[list[dict], dict | None]:
    """Per-rank rows plus a "ranks 1-3" subtotal (what a phone caller actually heard) from the same counts."""
    ranked = _group_rows(rows)
    top3 = [r for r in ranked if r["key"].isdigit() and 1 <= int(r["key"]) <= 3]
    if not top3:
        return ranked, None
    total = {k: sum(r[k] for r in top3) for k in ("suggested", *(s[0] for s in STATUSES))}
    rate = conversion(total["bought_exact"], total["bought_sibling"], total["not_bought"])
    return ranked, {**total, "label": "Ranks 1-3", "rate": pct(rate), "bar": round((rate or 0) * 100, 1),
                    "has_rate": rate is not None}


def _product_rows(rows) -> list[dict]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        rate = r.get("conversion_rate")
        out.append({
            "name": str(r.get("name") or r.get("sku") or ""), "brand": str(r.get("brand") or ""),
            "category": str(r.get("category") or ""), "strain": str(r.get("strain") or ""),
            "strain_type": str(r.get("strain_type") or ""), "size_label": str(r.get("size_label") or ""),
            "price": r.get("price"), "price_text": _money(r.get("price")),
            "thc_percent": r.get("thc_percent"), "thc_text": _thc(r.get("thc_percent")),
            "store": _STORE_LABELS.get(str(r.get("store") or ""), str(r.get("store") or "")),
            "partial": bool(r.get("snapshot_partial")),
            "times_suggested": _int(r.get("times_suggested")), "customers": _int(r.get("customers")),
            "bought_exact": _int(r.get("bought_exact")), "bought_sibling": _int(r.get("bought_sibling")),
            "not_bought": _int(r.get("not_bought")), "pending": _int(r.get("pending")),
            "unattributable": _int(r.get("unattributable")),
            "conversion_rate": rate, "rate": pct(rate),
            "last_suggested_at": str(r.get("last_suggested_at") or ""),
            "last": str(r.get("last_suggested_at") or "")[:10],
        })
    return out


# (sort key or None, header, text column?) - a text column sorts A-Z on first click, a number high-first.
_PRODUCT_COLUMNS = (
    ("name", "Product", True), ("brand", "Brand", True), ("category", "Category", True), ("price", "Price", False),
    ("thc", "THC", False), ("times_suggested", "Suggested", False), ("customers", "Customers", False),
    ("bought_exact", "Exact", False), ("bought_sibling", "Sibling", False), ("not_bought", "Not bought", False),
    (None, "Pending", False), (None, "Unattrib.", False), ("conversion", "Conversion", False),
    ("last", "Last suggested", False),
)


def product_headers(active: str, direction: str, qs: str) -> list[dict]:
    """Column headers with the sort link each click leads to (the page's other filters ride along)."""
    out = []
    for key, label, text in _PRODUCT_COLUMNS:
        if key is None:
            out.append({"label": label, "href": ""})
            continue
        descending = direction == "asc" if key == active else not text  # what the NEXT click asks for
        out.append({"label": label, "active": key == active, "dir": direction if key == active else "",
                    "href": f"?{qs}{'&' if qs else ''}sort={'-' if descending else ''}{key}"})
    return out


def sort_products(rows: list[dict], column: str, descending: bool) -> list[dict]:
    """Order the (already top-N) rows by ``column``; rows with no value go last whichever way it runs."""
    have = [r for r in rows if r.get(column) not in (None, "")]
    missing = [r for r in rows if r.get(column) in (None, "")]

    def key(r):
        v = r[column]
        return v.lower() if isinstance(v, str) else v

    return sorted(have, key=key, reverse=descending) + missing


def _buyer_rows(rows) -> list[dict]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        snap = r.get("snapshot") if isinstance(r.get("snapshot"), dict) else {}
        cust = r.get("customer") if isinstance(r.get("customer"), dict) else {}
        via, trusted = _IDENTITY.get(str(r.get("identity_via") or ""), (str(r.get("identity_via") or "") or "-", None))
        days = r.get("days_to_purchase")
        out.append({
            # ``voice_id`` is the dashboard's own customer id. The budtender's customer id is a different
            # key space, so it is never used to build a link (a wrong profile is worse than plain text).
            "name": str(cust.get("name") or "") or "(unnamed)", "voice_id": cust.get("voice_id"),
            "suggested": str(snap.get("name") or r.get("sku") or ""), "brand": str(snap.get("brand") or ""),
            "size_label": str(snap.get("size_label") or ""), "rank": snap.get("rank"),
            "channel": _CHANNEL_LABELS.get(str(r.get("channel") or ""), str(r.get("channel") or "")),
            "store": _STORE_LABELS.get(str(r.get("store") or ""), str(r.get("store") or "")),
            "match": "Exact" if r.get("status") == "bought_exact" else "Sibling",
            "match_kind": str(r.get("match_kind") or "").replace("sibling_", "").replace("_", " "),
            "bought": str(r.get("matched_name") or ""), "amount": _money(r.get("matched_amount")),
            "days": "-" if days is None else f"{days:g}",
            "when": str(r.get("matched_at") or "")[:10], "via": via, "via_trusted": trusted,
        })
    return out


def _unreachable(resp) -> str:
    reason = resp.get("reason") if isinstance(resp, dict) else ""
    return str(reason or "no answer")[:160]


def shape_suggestions(resp, request) -> dict:
    """The Suggestions section's context, or an unreachable marker (never zeros standing in for data)."""
    if not isinstance(resp, dict) or not resp.get("ok"):
        return {"state": "unreachable", "reason": _unreachable(resp)}
    totals = resp.get("totals")
    if not isinstance(totals, dict):
        return {"state": "unreachable", "reason": "unexpected analytics answer"}
    c = _counts(totals)
    bought = c["bought_exact"] + c["bought_sibling"]
    decided = bought + c["not_bought"]
    rank_rows, top3 = _rank_rows(resp.get("by_rank"))
    sort_col, sort_field, sort_dir = views._resolve_sort(request, _PRODUCT_SORTS, "-times_suggested")
    products = sort_products(_product_rows(resp.get("top_products")), sort_col.lstrip("-"), sort_dir == "desc")
    split = [{"label": label, "css": css, "n": c[key], "share": pct(c[key] / c["suggested"]) if c["suggested"] else "-",
              "width": round(c[key] / c["suggested"] * 100, 2) if c["suggested"] else 0}
             for key, label, css in STATUSES]
    return {
        "state": "ok" if c["suggested"] else "empty",
        "totals": {**c, "bought": bought, "decided": decided, "products": _int(totals.get("products")),
                   "customers_known": _int(totals.get("customers_known")),
                   "rate": totals.get("conversion_rate"),
                   "exact_share": round(c["bought_exact"] / bought, 4) if bought else None},
        "split": split,
        "by_channel": _group_rows(resp.get("by_channel"), _CHANNEL_LABELS),
        "by_store": _group_rows(resp.get("by_store"), _STORE_LABELS),
        "by_category": _group_rows(resp.get("by_category")),
        "by_kind": _group_rows(resp.get("by_kind"), _KIND_LABELS),
        "by_rank": rank_rows, "top3": top3,
        "trend": _suggestion_trend(resp.get("by_day"), _int(resp.get("window_days"))),
        "products": products,
        "product_headers": product_headers(sort_field, sort_dir, views._querystring(request, "sort")),
        "sort_label": next(label for key, label, _t in _PRODUCT_COLUMNS if key == sort_field),
        "never_bought": _product_rows(resp.get("never_bought")),
        "buyers": _buyer_rows(resp.get("recent_buyers")),
    }


# ── charts (inline SVG geometry; the templates only print it) ─────────────────
_W, _H = 720, 210
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 40, 8, 10, 26


def _nice_top(top: int) -> tuple[int, int]:
    """(axis max, step) with about 4 gridlines, on 1/2/5 x 10^k."""
    if top <= 0:
        return 4, 1
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000):
        if top <= 4 * step:
            return ceil(top / step) * step, step
    step = ceil(top / 4)
    return step * 4, step


def _bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """A bar with its data end (top) rounded and its baseline end square."""
    r = min(r, w / 2, h)
    if r <= 0:
        return f"M{x:.1f},{y + h:.1f}V{y:.1f}H{x + w:.1f}V{y + h:.1f}Z"
    return (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}"
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z")


def stacked_columns(buckets: list[dict], series: list[tuple[str, str, str]]) -> dict:
    """Columns for ``buckets`` = [{label, values: {key: n}}]; ``series`` = [(key, label, css)] bottom to top.
    4px rounded tops, square at the baseline, a 2px gap between stacked segments, a hit area wider than the bar."""
    n = len(buckets)
    plot_w, plot_h = _W - _PAD_L - _PAD_R, _H - _PAD_T - _PAD_B
    totals = [sum(b["values"].get(k, 0) for k, _l, _c in series) for b in buckets]
    top, step = _nice_top(max(totals, default=0))
    slot = plot_w / max(n, 1)
    bar = max(min(24, slot - 2), 1.5)
    baseline = _PAD_T + plot_h
    cols = []
    for i, b in enumerate(buckets):
        x = _PAD_L + i * slot + (slot - bar) / 2
        y_cursor, segs, shown = baseline, [], [(k, c) for k, _l, c in series if b["values"].get(k, 0) > 0]
        for j, (k, css) in enumerate(shown):
            h = b["values"][k] / top * plot_h
            gap = 2 if j else 0
            y_cursor -= gap
            seg_h = max(h - gap, 1)
            y_cursor -= seg_h
            segs.append({"d": _bar_path(x, y_cursor, bar, seg_h, 4 if j == len(shown) - 1 else 0), "css": css})
        title = f"{b['label']}: {totals[i]}" + "".join(
            f" | {lab} {b['values'][k]}" for k, lab, _c in series if b["values"].get(k, 0) and len(series) > 1)
        cols.append({"label": b["label"], "title": title, "segs": segs, "total": totals[i],
                     "values": [b["values"].get(k, 0) for k, _l, _c in series],
                     "hit_x": round(_PAD_L + i * slot, 1), "hit_w": round(slot, 1)})
    pick = sorted({0, n - 1, *(round(i * (n - 1) / 4) for i in range(5))}) if n else []
    return {
        "w": _W, "h": _H, "left": _PAD_L, "right": _W - _PAD_R, "baseline": baseline, "plot_top": _PAD_T,
        "plot_h": plot_h, "cols": cols, "series": [(label, css) for _k, label, css in series],
        "tick_x": _PAD_L - 6, "label_y": baseline + 16,
        "ticks": [{"y": round(baseline - v / top * plot_h, 1), "ty": round(baseline - v / top * plot_h + 3.5, 1), "label": v}
                  for v in range(0, top + 1, step)],
        "xlabels": [{"x": round(_PAD_L + (i + 0.5) * slot, 1), "text": buckets[i]["label"]} for i in pick],
        "total": sum(totals),
    }


def _day_label(d: date) -> str:
    return f"{d:%b} {d.day}"


def _bucket(days: list[tuple[date, dict]], weekly: bool) -> list[dict]:
    """Per-day buckets, or per Monday-start week when the window is long."""
    if not weekly:
        return [{"label": _day_label(d), "values": v} for d, v in days]
    weeks: dict[date, dict] = {}
    for d, v in days:
        acc = weeks.setdefault(d - timedelta(days=d.weekday()), {})
        for k, n in v.items():
            acc[k] = acc.get(k, 0) + n
    return [{"label": f"Week of {_day_label(w)}", "values": v} for w, v in sorted(weeks.items())]


def _window_days(days: int) -> tuple[date, date]:
    end = timezone.localdate()
    return end - timedelta(days=max(days, 1) - 1), end


def _suggestion_trend(by_day, window_days: int) -> dict | None:
    rows = {}
    for r in by_day if isinstance(by_day, list) else []:
        try:
            rows[date.fromisoformat(str(r.get("key"))[:10])] = _counts(r)
        except (ValueError, AttributeError):
            continue
    if not rows:
        return None
    start, end = _window_days(window_days or 30)
    start, end = min(start, min(rows)), max(end, max(rows))
    zero = {s[0]: 0 for s in STATUSES}
    days = [(start + timedelta(days=i), rows.get(start + timedelta(days=i), zero)) for i in range((end - start).days + 1)]
    chart = stacked_columns(_bucket(days, len(days) > WEEKLY_AFTER_DAYS), [(k, label, css) for k, label, css in STATUSES])
    chart["unit"] = "week" if len(days) > WEEKLY_AFTER_DAYS else "day"
    return chart


def call_volume_chart(by_day: dict[date, int], days: int) -> dict:
    start, end = _window_days(days)
    series = analytics_calls.daily_series(by_day, start, end)
    chart = stacked_columns(_bucket([(d, {"calls": n}) for d, n in series], len(series) > WEEKLY_AFTER_DAYS),
                            [("calls", "Calls", "c-calls")])
    chart["unit"] = "week" if len(series) > WEEKLY_AFTER_DAYS else "day"
    return chart


_HEAT_X0, _HEAT_CELL_W, _HEAT_CELL_H, _HEAT_Y0 = 36, 26, 22, 4


def _hour_label(h: int) -> str:
    return f"{h % 12 or 12}{'a' if h < 12 else 'p'}"


def heatmap(heat: list[list[int]]) -> dict:
    """Day-of-week x hour grid (store time). Five sequential levels; zero is its own quiet cell."""
    peak = max((n for row in heat for n in row), default=0)
    cells = []
    for d, row in enumerate(heat):
        for h, n in enumerate(row):
            level = ceil(5 * n / peak) if n and peak else 0
            cells.append({"x": _HEAT_X0 + h * _HEAT_CELL_W, "y": _HEAT_Y0 + d * _HEAT_CELL_H, "level": level,
                          "title": f"{analytics_calls.DAYS[d]} {_hour_label(h)}: {n} call{'' if n == 1 else 's'}"})
    return {
        "w": _HEAT_X0 + 24 * _HEAT_CELL_W, "h": _HEAT_Y0 + 7 * _HEAT_CELL_H + 20, "cw": _HEAT_CELL_W - 2,
        "ch": _HEAT_CELL_H - 2, "cells": cells, "peak": peak,
        "xlabels": [{"x": _HEAT_X0 + h * _HEAT_CELL_W + _HEAT_CELL_W / 2, "text": _hour_label(h)} for h in range(0, 24, 3)],
        "ylabels": [{"y": _HEAT_Y0 + d * _HEAT_CELL_H + _HEAT_CELL_H / 2 + 4, "text": name}
                    for d, name in enumerate(analytics_calls.DAYS)],
        "label_y": _HEAT_Y0 + 7 * _HEAT_CELL_H + 14,
        "table": [{"day": analytics_calls.DAYS[d], "hours_by3": [sum(row[h:h + 3]) for h in range(0, 24, 3)],
                   "total": sum(row)} for d, row in enumerate(heat)],
    }


# ── calls, chat funnel and demand sections ────────────────────────────────────
def shape_calls(metrics: dict, days: int) -> dict:
    d = metrics["duration"]
    out_total = metrics["total"] or 1
    return {
        **metrics, "empty": metrics["total"] == 0,
        "by_store": [{"store": _STORE_LABELS.get(s["store"], s["store"]), "n": s["n"]} for s in metrics["by_store"]],
        "outcomes": [{**o, "share": pct(o["n"] / out_total), "bar": round(o["n"] / out_total * 100, 1)}
                     for o in metrics["outcomes"]],
        "volume": call_volume_chart(metrics["by_day"], days),
        "heatmap": heatmap(metrics["heat"]),
        "median": analytics_calls.fmt_duration(d["median"]), "p90": analytics_calls.fmt_duration(d["p90"]),
        "transfer_rate": pct(metrics["transfers"]["rate"]),
        "peak_text": (f"{metrics['peak']['day']} {_hour_label(metrics['peak']['hour'])} "
                      f"({metrics['peak']['n']} calls)") if metrics["peak"] else "",
    }


def shape_funnel(resp) -> dict:
    if not isinstance(resp, dict) or not resp.get("ok"):
        return {"state": "unreachable", "reason": _unreachable(resp)}
    stages = [s for s in resp.get("funnel", []) if isinstance(s, dict)] if isinstance(resp.get("funnel"), list) else []
    first = max((_int(s.get("sessions")) for s in stages), default=0)
    bounces = resp.get("bounces") if isinstance(resp.get("bounces"), dict) else {}
    actions = resp.get("actions") if isinstance(resp.get("actions"), dict) else {}
    return {
        "state": "ok" if _int(resp.get("sessions")) else "empty",
        "sessions": _int(resp.get("sessions")), "visitors": _int(resp.get("unique_visitors")),
        "searches": _int(actions.get("searches")),
        "stages": [{"stage": str(s.get("stage", "")), "sessions": _int(s.get("sessions")),
                    "bar": round(_int(s.get("sessions")) / first * 100, 1) if first else 0} for s in stages],
        "bounces": _int(bounces.get("total")), "bounce_rate": bounces.get("rate"),
        "zero": [{"what": str(z.get("slots", "")), "n": _int(z.get("searches"))}
                 for z in resp.get("zero_result_searches", []) if isinstance(z, dict)]
        if isinstance(resp.get("zero_result_searches"), list) else [],
        "categories": [{"what": str(z.get("category", "")), "n": _int(z.get("searches"))}
                       for z in resp.get("top_categories", []) if isinstance(z, dict)]
        if isinstance(resp.get("top_categories"), list) else [],
    }


def _result(future) -> dict:
    """A fetch's answer, or an unreachable marker if the fetch itself blew up - the page never 500s."""
    try:
        return future.result()
    except Exception:  # noqa: BLE001 - any failure of a section must degrade to a note
        return {"ok": False, "reason": "analytics fetch failed"}


def build_kpis(f: Filters, sug: dict, calls: dict | None, chat: dict | None) -> list[dict]:
    def tile(label, value, sub, muted=False):
        return {"label": label, "value": value, "sub": sub, "muted": muted}

    down = "analytics service unreachable"
    if sug["state"] == "unreachable":
        sugg = [tile("Suggestions shown", "n/a", down, True), tile("Conversion rate", "n/a", down, True),
                tile("Exact vs sibling", "n/a", down, True)]
    else:
        t = sug["totals"]
        sugg = [
            tile("Suggestions shown", t["suggested"], f"{t['products']} products, {t['customers_known']} known customers"),
            tile("Conversion rate", pct(t["rate"]) if t["decided"] else "-",
                 f"{t['bought']} bought of {t['decided']} decided (pending and unattributable left out)"),
            tile("Exact vs sibling",
                 f"{t['exact_share'] * 100:.0f}% / {100 - t['exact_share'] * 100:.0f}%" if t["bought"] else "-",
                 f"exact / sibling, of {t['bought']} purchases"),
        ]
    if calls is None:
        call_tiles = [tile("Calls", "-", "phone only: not in this channel filter", True),
                      tile("Transfer success", "-", "phone only: not in this channel filter", True)]
    else:
        tr = calls["transfers"]
        call_tiles = [
            tile("Calls", calls["total"], "voice calls in the window"),
            tile("Transfer success", calls["transfer_rate"] if tr["attempts"] else "-",
                 f"{tr['connected']} of {tr['attempts']} transfers connected" if tr["attempts"] else "no transfers attempted"),
        ]
    if chat is None:
        chat_tile = tile("Chats", "-", "chat only: not in the phone filter", True)
    elif chat["state"] == "unreachable":
        chat_tile = tile("Chats", "n/a", down, True)
    else:
        chat_tile = tile("Chats", chat["sessions"], "website chat sessions" + (" (not narrowed by channel)" if f.channel else ""))
    return [*sugg, call_tiles[0], chat_tile, call_tiles[1]]


@staff_member_required
def analytics_dashboard(request):
    f = parse_filters(request.GET)
    show_calls = f.channel in ("", "phone")
    show_chat = f.channel != "phone"
    # Both budtender calls run at once (each can wait ~24s on a slow service); neither touches the database.
    with ThreadPoolExecutor(max_workers=2) as pool:
        sug_future = pool.submit(views._budtender_post, SUGGESTIONS_PATH, f.payload())
        chat_future = (pool.submit(views._budtender_post, FUNNEL_PATH,
                                   {k: v for k, v in f.payload(recent=0).items() if k != "channel"})
                       if show_chat else None)
    sug = shape_suggestions(_result(sug_future), request)
    chat = shape_funnel(_result(chat_future)) if chat_future else None
    calls = (shape_calls(analytics_calls.call_metrics(timezone.now() - timedelta(days=f.days), f.store), f.days)
             if show_calls else None)
    start, end = _window_days(f.days)
    return render(request, "dashboard/analytics.html", {
        "f": f, "stores": STORES, "channels": CHANNELS, "presets": PRESETS, "status_list": STATUSES,
        "since": start, "until": end,
        "kpis": build_kpis(f, sug, calls, chat), "sug": sug, "calls": calls, "chat": chat,
        "chat_link": f"?days={f.days}" + (f"&store={f.store}" if f.store else ""),
    })
