"""Customers page: a sortable, filterable, exportable table over ``crm.CustomerProfile``.

The imported analytics snapshot is the roster. Sorting, filtering and the CSV export all read the
denormalised columns on ``CustomerProfile`` (``items``, ``top_category``, ``brands_text``,
``first_order_date``, ``last_order_date`` — see ``crm/models.py``), never the JSON. With no snapshot
loaded the page falls back to budtender's live roster (name search + paging only; it has no spend).

Boundaries: staff-only; no phone number is ever read, rendered or exported (the model has none).
Spend over a date range is NOT possible with the current export (no per-order history), so the date
filters apply to first/last order dates only and the page says so.
"""

from __future__ import annotations

import csv
import math
from datetime import date, datetime, timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import F, Value
from django.db.models.functions import Lower, NullIf
from django.http import StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers

from .views import _bounded_int, _resolve_sort, _row_from_bt

PAGE_SIZES = (25, 50, 100)
EXPORT_CAP = 50_000
DEFAULT_SORT = "-spend"
DASH = "—"

# Frequency bands over ``cadence_days`` (avg days between orders): (key, label, lo, hi), inclusive,
# ``None`` = open end. A null cadence (fewer than two orders) is the separate "unknown" band.
BANDS = (
    ("weekly", "weekly", None, 9),
    ("biweekly", "2 weeks", 10, 19),
    ("monthly", "monthly", 20, 45),
    ("occasional", "occasional", 46, 120),
    ("rare", "rare", 121, None),
)
UNKNOWN_BAND = "unknown"

# (sort key, header, first-click direction). "freq" sorts on days between orders, so asc = most
# frequent first.
COLUMNS = (
    ("name", "Customer", "asc"),
    ("orders", "Orders", "desc"),
    ("spend", "Lifetime spend", "desc"),
    ("aov", "Avg cart", "desc"),
    ("items", "# items", "desc"),
    ("freq", "Frequency", "asc"),
    ("last", "Last order", "desc"),
    ("since", "Customer since", "asc"),
    ("brand", "Favorite brand", "asc"),
    ("category", "Favorite category", "asc"),
)
_NUMERIC_COLUMNS = {"orders", "spend", "aov", "items"}
# Blank text sorts as "no value" (NULLIF) so it lands last in both directions, like a null.
_SORT_SOURCE = {
    "name": Lower("name"),
    "orders": F("orders"),
    "spend": F("total_spend"),
    "aov": F("aov"),
    "items": F("items"),
    "freq": F("cadence_days"),
    "last": F("last_order_date"),
    "since": F("first_order_date"),
    "brand": NullIf(Lower("top_brand"), Value("")),
    "category": NullIf(Lower("top_category"), Value("")),
}

# (param prefix, label, model field); params are <prefix>_min / <prefix>_max.
NUMBER_FILTERS = (
    ("aov", "Avg cart", "aov"),
    ("items", "# items", "items"),
    ("orders", "Orders", "orders"),
    ("spend", "Lifetime spend", "total_spend"),
)
# (param prefix, label, model field); params are <prefix>_from / <prefix>_to.
DATE_FILTERS = (
    ("last", "Last order", "last_order_date"),
    ("first", "First order", "first_order_date"),
)
_LIST_FIELDS = (
    "id", "name", "customer_key", "segment", "orders", "total_spend", "aov", "items",
    "cadence_days", "first_order_date", "last_order_date", "top_brand", "top_category",
)


def band_for(cadence_days: int | None) -> str:
    """The frequency band label for a cadence; the single place a cadence becomes a band."""
    if cadence_days is None:
        return UNKNOWN_BAND
    for _key, label, lo, hi in BANDS:
        if (lo is None or cadence_days >= lo) and (hi is None or cadence_days <= hi):
            return label
    return UNKNOWN_BAND  # unreachable: BANDS cover every integer


def _band_option_label(label: str, lo, hi) -> str:
    if lo is None:
        span = f"≤ {hi} days"
    elif hi is None:
        span = f"> {lo - 1} days"
    else:
        span = f"{lo}–{hi} days"
    return f"{label.capitalize()} ({span})"


def _band_options() -> list[tuple[str, str]]:
    return [(k, _band_option_label(lbl, lo, hi)) for k, lbl, lo, hi in BANDS] + [
        (UNKNOWN_BAND, "Unknown (fewer than 2 orders)")
    ]


def _today() -> date:
    return timezone.localdate()


# ── parsing ───────────────────────────────────────────────────────────────────
def _num(raw: str) -> float | None:
    """'$1,234.5' -> 1234.5; junk, nan and inf -> None."""
    try:
        n = float(raw.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _day(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _filtered(get):
    """Apply every filter in the query string -> (queryset, notes, active). Bad input is ignored
    with a note naming it (never a 500, never a silently different result)."""
    from crm.models import CustomerProfile

    qs = CustomerProfile.objects.all()
    notes: list[str] = []
    active = False

    q = (get.get("q") or "").strip()
    if q:
        qs, active = qs.filter(name__icontains=q), True

    brand = (get.get("brand") or "").strip().lower().replace("|", " ")
    if brand:
        # A name that is exactly one known brand matches that brand only; otherwise it is a substring.
        exact = f"|{brand}|"
        needle = exact if qs.filter(brands_text__icontains=exact).exists() else brand
        qs, active = qs.filter(brands_text__icontains=needle), True

    for param, field in (("category", "top_category"), ("segment", "segment")):
        value = (get.get(param) or "").strip()
        if value:
            qs, active = qs.filter(**{field: value}), True

    freq = (get.get("freq") or "").strip()
    if freq == UNKNOWN_BAND:
        qs, active = qs.filter(cadence_days__isnull=True), True
    elif freq:
        band = next((b for b in BANDS if b[0] == freq), None)
        if band is None:
            notes.append(f"Ignored unknown frequency '{freq}'.")
        else:
            _key, _label, lo, hi = band
            if lo is not None:
                qs = qs.filter(cadence_days__gte=lo)
            if hi is not None:
                qs = qs.filter(cadence_days__lte=hi)
            qs = qs.filter(cadence_days__isnull=False)
            active = True

    for prefix, label, field in NUMBER_FILTERS:
        for suffix, lookup, word in (("min", "gte", "min"), ("max", "lte", "max")):
            raw = (get.get(f"{prefix}_{suffix}") or "").strip()
            if not raw:
                continue
            n = _num(raw)
            if n is None:
                notes.append(f"Ignored '{raw}' for {label} {word}: not a number.")
                continue
            qs, active = qs.filter(**{f"{field}__{lookup}": n}), True

    for prefix, label, field in DATE_FILTERS:
        bounds = {}
        for suffix, lookup, word in (("from", "gte", "from"), ("to", "lte", "to")):
            raw = (get.get(f"{prefix}_{suffix}") or "").strip()
            if not raw:
                continue
            d = _day(raw)
            if d is None:
                notes.append(f"Ignored '{raw}' for {label} {word}: use YYYY-MM-DD.")
                continue
            bounds[lookup] = d
            qs, active = qs.filter(**{f"{field}__{lookup}": d}), True
        if len(bounds) == 2 and bounds["gte"] > bounds["lte"]:
            notes.append(f"{label}: 'from' is after 'to', so nothing can match.")
    return qs, notes, active


def _ordered(qs, field: str, direction: str):
    """Primary key with nulls last in either direction, then name, then pk (stable)."""
    src = _SORT_SOURCE[field]
    primary = src.desc(nulls_last=True) if direction == "desc" else src.asc(nulls_last=True)
    return qs.order_by(primary, Lower("name").asc(), "pk")


def _sort(request) -> tuple[str, str]:
    _order_by, field, direction = _resolve_sort(request, {k: k for k in _SORT_SOURCE}, DEFAULT_SORT)
    return field, direction


# ── presentation ──────────────────────────────────────────────────────────────
def _money(x: float | None) -> str:
    return DASH if x is None else f"${x:,.2f}"


def _row(p) -> dict:
    """One table/CSV row. Raw values for the CSV, display strings for the HTML — both from here."""
    per_order = round(p.items / p.orders, 1) if p.items is not None and p.orders else None
    cadence = p.cadence_days
    return {
        "id": p.pk,
        "name": p.name or p.customer_key,
        "segment": p.segment,
        "orders": p.orders,
        "spend": p.total_spend,
        "aov": p.aov,
        "items": p.items,
        "items_per_order": per_order,
        "cadence": cadence,
        "band": band_for(cadence),
        "last": p.last_order_date,
        "since": p.first_order_date,
        "brand": p.top_brand,
        "category": p.top_category,
        "spend_txt": _money(p.total_spend),
        "aov_txt": _money(p.aov),
        "items_txt": DASH if p.items is None else f"{p.items:,}",
        "per_order_txt": "" if per_order is None else f"{per_order:g}/order",
        "freq_txt": DASH if cadence is None
        else f"every ~{cadence} day{'' if cadence == 1 else 's'}",
        "last_txt": p.last_order_date.isoformat() if p.last_order_date else DASH,
        "since_txt": p.first_order_date.isoformat() if p.first_order_date else DASH,
        "brand_txt": p.top_brand or DASH,
        "category_txt": p.top_category or DASH,
    }


def _href(request, *, path: str | None = None, **set_: object) -> str:
    """The current query string with ``set_`` params replaced ('' removes one), on ``path`` (default:
    this page); 'page' always resets."""
    params = request.GET.copy()
    params.pop("page", None)
    for key, value in set_.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = str(value)
    qs = params.urlencode()
    path = path or request.path
    return f"{path}?{qs}" if qs else path


def _columns(request, field: str, direction: str) -> list[dict]:
    out = []
    for key, label, first in COLUMNS:
        nxt = ("asc" if direction == "desc" else "desc") if key == field else first
        out.append({
            "label": label,
            "num": key in _NUMERIC_COLUMNS,
            "href": _href(request, sort=("-" if nxt == "desc" else "") + key),
            "arrow": ("▼" if direction == "desc" else "▲") if key == field else "",
            "aria": ("descending" if direction == "desc" else "ascending") if key == field else "none",
        })
    return out


def _presets(request, today: date) -> dict[str, list[dict]]:
    out = {}
    for prefix, _label, _field in DATE_FILTERS:
        options = (
            ("30 days", today - timedelta(days=30)),
            ("90 days", today - timedelta(days=90)),
            ("This year", date(today.year, 1, 1)),
            ("All time", None),
        )
        out[prefix] = [
            {
                "label": label,
                "from": start.isoformat() if start else "",
                "href": _href(request, **{f"{prefix}_from": start.isoformat() if start else "",
                                          f"{prefix}_to": ""}),
            }
            for label, start in options
        ]
    return out


def _page_size(request) -> int:
    n = _bounded_int(request.GET.get("per"), default=PAGE_SIZES[0], lo=1, hi=1000)
    return n if n in PAGE_SIZES else PAGE_SIZES[0]


def _is_htmx_partial(request) -> bool:
    """htmx in-place refresh. A history restore (Back button) must get the full page."""
    return bool(request.headers.get("HX-Request")) and not request.headers.get(
        "HX-History-Restore-Request"
    )


# ── views ─────────────────────────────────────────────────────────────────────
@staff_member_required
def customers(request):
    """The Customers table. htmx requests get just the table partial; a plain GET the full page."""
    from crm.models import CustomerProfile

    per = _page_size(request)
    page_no = _bounded_int(request.GET.get("page"), default=1, lo=1, hi=10_000_000)
    partial = _is_htmx_partial(request)
    total_all = CustomerProfile.objects.count()

    if total_all:
        ctx = _analytics_context(request, per, page_no, total_all, partial)
    else:
        ctx = _live_context(request, per, page_no)
    ctx.update({"per": per, "page_sizes": PAGE_SIZES, "page_no": page_no})

    response = render(
        request,
        "dashboard/_customers_table.html" if partial else "dashboard/customers.html",
        ctx,
    )
    patch_vary_headers(response, ("HX-Request",))  # same URL, two bodies: caches must tell them apart
    return response


def _analytics_context(request, per: int, page_no: int, total_all: int, partial: bool) -> dict:
    qs, notes, active = _filtered(request.GET)
    field, direction = _sort(request)
    page = Paginator(_ordered(qs, field, direction).only(*_LIST_FIELDS), per).get_page(page_no)
    shown = page.paginator.count
    ctx = {
        "source": "analytics",
        "rows": [_row(p) for p in page.object_list],
        "notes": notes,
        "summary": f"{shown:,} of {total_all:,} customers",
        "columns": _columns(request, field, direction),
        "page_no": page.number,
        "num_pages": page.paginator.num_pages,
        "prev_href": _href(request, page=page.number - 1) if page.has_previous() else "",
        "next_href": _href(request, page=page.number + 1) if page.has_next() else "",
        "export_href": _href(request, path=reverse("dash-customers-export")),
        "export_capped": shown > EXPORT_CAP,
        "export_cap": f"{EXPORT_CAP:,}",
        "filters_active": active,
        "clear_href": _clear_href(request),
    }
    if not partial:  # the form (and its option lists) is not part of the htmx swap
        presets = _presets(request, _today())
        vals = {k: (request.GET.get(k) or "").strip() for k in _filter_params()}
        ctx.update({
            "f": vals,
            "sort_param": request.GET.get("sort", ""),
            "brand_options": _distinct("top_brand")[:3000],
            "category_options": _distinct("top_category"),
            "segment_options": _distinct("segment"),
            "band_options": _band_options(),
            "number_filters": [
                {"label": label, "lo": f"{p}_min", "lo_val": vals[f"{p}_min"],
                 "hi": f"{p}_max", "hi_val": vals[f"{p}_max"]}
                for p, label, _f in NUMBER_FILTERS
            ],
            "date_filters": [
                {"label": label, "prefix": p, "lo": f"{p}_from", "lo_val": vals[f"{p}_from"],
                 "hi": f"{p}_to", "hi_val": vals[f"{p}_to"], "presets": presets[p]}
                for p, label, _f in DATE_FILTERS
            ],
        })
    return ctx


def _distinct(field: str):
    """Sorted distinct non-blank values of a CustomerProfile column (for the filter controls)."""
    from crm.models import CustomerProfile

    return list(
        CustomerProfile.objects.exclude(**{field: ""}).order_by(field)
        .values_list(field, flat=True).distinct()
    )


def _filter_params() -> list[str]:
    params = ["q", "brand", "category", "segment", "freq"]
    params += [f"{p}_{s}" for p, _l, _f in NUMBER_FILTERS for s in ("min", "max")]
    params += [f"{p}_{s}" for p, _l, _f in DATE_FILTERS for s in ("from", "to")]
    return params


def _clear_href(request) -> str:
    return _href(request, **dict.fromkeys(_filter_params(), ""))


def _live_context(request, per: int, page_no: int) -> dict:
    """No snapshot loaded: budtender's live roster (name search + paging only)."""
    from voice.budtender_client import budtender

    q = (request.GET.get("q") or "").strip()
    bt = budtender().list_customers(q=q, limit=per, offset=(page_no - 1) * per)
    rows = [_row_from_bt(c) for c in bt.get("customers", [])]
    total = bt.get("total", len(rows))
    num_pages = max(1, (total + per - 1) // per)
    page_no = min(page_no, num_pages)
    return {
        "source": "live" if bt.get("ok") else "empty",
        "rows": rows,
        "notes": [],
        "summary": f"{total:,} customer{'' if total == 1 else 's'}",
        "f": {"q": q},
        "sort_param": "",
        "num_pages": num_pages,
        "prev_href": _href(request, page=page_no - 1) if page_no > 1 else "",
        "next_href": _href(request, page=page_no + 1) if page_no < num_pages else "",
        "filters_active": bool(q),
        "clear_href": _clear_href(request),
    }


# ── CSV export ────────────────────────────────────────────────────────────────
BOM = chr(0xFEFF)  # so Excel opens the UTF-8 CSV correctly
CSV_HEADER = (
    "Customer", "Segment", "Orders", "Lifetime spend", "Avg cart", "Items", "Items per order",
    "Days between orders", "Frequency", "Last order", "Customer since", "Favorite brand",
    "Favorite category",
)
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value):
    """Neutralise spreadsheet formulas in TEXT cells; numbers stay numbers (a refund total of -12.5
    must not turn into the text "'-12.5")."""
    if isinstance(value, str) and value.startswith(_FORMULA_LEADS):
        return "'" + value
    return value


def _blank(v):
    return "" if v is None else v


def _csv_cells(r: dict) -> list:
    return [
        r["name"], r["segment"], r["orders"], round(r["spend"], 2), round(r["aov"], 2),
        _blank(r["items"]), _blank(r["items_per_order"]), _blank(r["cadence"]), r["band"],
        r["last"].isoformat() if r["last"] else "", r["since"].isoformat() if r["since"] else "",
        r["brand"], r["category"],
    ]


class _Echo:
    def write(self, value):
        return value


def _csv_stream(qs):
    writer = csv.writer(_Echo())
    yield BOM + writer.writerow(CSV_HEADER)  # BOM so Excel reads UTF-8
    for p in qs.iterator(chunk_size=2000):
        yield writer.writerow([_safe_cell(c) for c in _csv_cells(_row(p))])


@staff_member_required
def customers_export(request):
    """CSV of the CURRENT filtered + sorted result (first 50,000 rows), streamed."""
    qs, _notes, _active = _filtered(request.GET)
    field, direction = _sort(request)
    qs = _ordered(qs, field, direction).only(*_LIST_FIELDS)[:EXPORT_CAP]
    response = StreamingHttpResponse(
        _csv_stream(qs), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="customers-{_today().isoformat()}.csv"'
    return response

