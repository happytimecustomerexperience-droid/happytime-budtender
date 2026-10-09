"""HTTP layer for the bulk data tools (``dashboard/bulk.py`` holds the logic).

Routes (all ``/dashboard/data/<key>/...``, staff only, CSRF like every other dashboard page):

* ``template.csv`` / ``export.csv`` -- the CSV round trip downloads.
* ``upload`` -- GET the form; POST a file = step 1 (validate + preview, writes NOTHING); POST the
  signed token back = step 2 (re-validate from scratch, then commit in one transaction).
* ``row/new``, ``row/<pk>/``, ``row/<pk>/edit``, ``row/<pk>/delete`` -- in-place editing. With an
  ``HX-Request`` they answer with a ``<tr>`` partial (status 200, never a redirect); without one
  they redirect back to the originating list page (``safe_next``).
* ``edit-all`` -- every visible row as inputs, one "Save all".
* ``bulk-action`` -- activate / deactivate / delete / set one field over the ticked rows.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.core.paginator import Paginator
from django.db.models import ProtectedError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import bulk
from .views import PER_PAGE, _querystring, _toast

logger = logging.getLogger(__name__)

TOKEN_SALT = "dashboard.bulk.upload.v1"
TOKEN_MAX_AGE = 3600  # a preview is good for an hour


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────
def _dataset(key: str, *, need_csv: bool = False) -> bulk.Dataset:
    ds = bulk.get_dataset(key)
    if ds is None or (need_csv and not ds.csv):
        raise Http404("unknown dataset")
    return ds


def list_url(ds: bulk.Dataset) -> str:
    """The list page that shows ``ds`` (the fallback for every redirect)."""
    if ds.list_url == "dash-kb-source":
        return reverse("dash-kb-source", kwargs={"kind": ds.kb_kind})
    return reverse(ds.list_url)


def same_site_path(request, candidate: str | None) -> str:
    """``candidate`` reduced to ``/dashboard/...?query`` when it points at this site's dashboard,
    else ''. Absolute URLs are accepted only for this request's own host; ``//host`` and other
    schemes never pass (``url_has_allowed_host_and_scheme``)."""
    if not candidate or len(candidate) > 2000:
        return ""
    if not url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return ""
    parts = urlsplit(candidate)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return path if path.startswith("/dashboard/") else ""


def safe_next(request, ds: bulk.Dataset) -> str:
    """Where to send the user back to: an explicit ``next`` (form field or query), else the page
    htmx says it was on (``HX-Current-URL``), else the dataset's list page. Never off-site."""
    for cand in (request.POST.get("next"), request.GET.get("next"), request.headers.get("HX-Current-URL")):
        path = same_site_path(request, cand)
        if path:
            return path
    return list_url(ds)


def settable_fields(ds: bulk.Dataset) -> list[tuple[str, str]]:
    return [(c.name, c.label) for c in ds.columns() if c.name not in ds.key_fields and c.type != "longtext"]


def list_context(request, ds: bulk.Dataset) -> dict:
    """The context the toolbar / action bar / row partials need on a list page."""
    return {
        "ds": ds,
        "next_url": request.get_full_path(),
        "tool_qs": _querystring(request),
        "edit_qs": request.GET.urlencode(),
        "settable": settable_fields(ds),
    }


def filtered_qs(ds: bulk.Dataset, params):
    qs = bulk.search_queryset(ds.model, ds.queryset(), params.get("q", ""))
    kind = params.get("kind", "")
    if kind and "kind" in ds.form_fields() and kind in {k for k, _l in ds.model.KINDS}:
        qs = qs.filter(kind=kind)
    return qs


def _visible(ds: bulk.Dataset, params) -> list:
    qs = filtered_qs(ds, params)
    if ds.paginate:
        return list(Paginator(qs, PER_PAGE).get_page(params.get("page")))
    return list(qs)


def _csv_response(body: bytes, name: str) -> HttpResponse:
    resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "no-store"
    return resp


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _ctx(request, ds, **extra) -> dict:
    return {"ds": ds, "next_url": safe_next(request, ds), **extra}


def _toasted(resp: HttpResponse, level: str, message: str) -> HttpResponse:
    resp["HX-Trigger"] = _toast(level, message)
    return resp


# ── CSV downloads ───────────────────────────────────────────────────────────────────────────────
@staff_member_required
@require_GET
def data_template(request, key: str):
    ds = _dataset(key, need_csv=True)
    return _csv_response(ds.template_csv(), f"happytime-{ds.key}-template.csv")


@staff_member_required
@require_GET
def data_export(request, key: str):
    """Everything the list currently shows (same ``q`` / ``kind`` filters), all pages."""
    ds = _dataset(key, need_csv=True)
    logger.info("bulk export dataset=%s user=%s", ds.key, request.user.get_username())
    return _csv_response(ds.export_csv(filtered_qs(ds, request.GET)), f"happytime-{ds.key}-export.csv")


# ── CSV upload (two steps) ──────────────────────────────────────────────────────────────────────
def _upload_page(request, ds, **extra):
    return render(request, "dashboard/data_upload.html", {
        "ds": ds, "columns": ds.columns(), "max_rows": bulk.MAX_ROWS, "list_page": list_url(ds),
        "stores": bulk.STORES, **extra,
    })


def _preview(request, ds, parsed, plan, *, warning: str = ""):
    token = signing.dumps(
        {"ds": ds.key, "uid": request.user.pk, "text": parsed.text,
         "del": plan.allow_deletes, "digest": plan.digest},
        salt=TOKEN_SALT, compress=True,
    )
    counts = plan.counts
    logger.info(
        "bulk preview dataset=%s user=%s create=%d update=%d unchanged=%d delete=%d errors=%d",
        ds.key, request.user.get_username(), counts["create"], counts["update"], counts["unchanged"],
        counts["delete"], counts["error"],
    )
    return _upload_page(request, ds, plan=plan, counts=counts, token=token, warning=warning,
                        can_apply=not plan.file_errors and (counts["create"] + counts["update"] + counts["delete"]) > 0)


@staff_member_required
@require_http_methods(["GET", "POST"])
def data_upload(request, key: str):
    ds = _dataset(key, need_csv=True)
    if request.method == "GET":
        return _upload_page(request, ds)
    if request.POST.get("token"):
        return _upload_apply(request, ds)
    upload = request.FILES.get("file")
    if upload is None:
        return _upload_page(request, ds, error="Choose a CSV file first.")
    if upload.size > bulk.MAX_BYTES:
        return _upload_page(request, ds, error="The file is larger than 1 MB.")
    try:
        parsed = bulk.parse_upload(ds, upload.read(bulk.MAX_BYTES + 1))
    except bulk.BulkError as exc:
        return _upload_page(request, ds, error=str(exc))
    plan = bulk.plan_file(ds, parsed, allow_deletes=request.POST.get("allow_deletes") == "on")
    return _preview(request, ds, parsed, plan)


def _upload_apply(request, ds):
    try:
        data = signing.loads(request.POST["token"], salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        return _upload_page(request, ds, error="That preview expired or was altered. Upload the file again.")
    if data.get("ds") != ds.key or data.get("uid") != request.user.pk:
        return _upload_page(request, ds, error="That preview belongs to someone else. Upload the file again.")
    parsed = bulk.parse_text(ds, data["text"])
    plan = bulk.plan_file(ds, parsed, allow_deletes=bool(data.get("del")))
    if plan.digest != data["digest"]:
        return _preview(request, ds, parsed, plan, warning=(
            "The data changed since you previewed this file, so nothing was saved. "
            "Review the new preview and apply again."))
    summary = bulk.apply_plan(
        ds, plan, user=request.user, action="upload",
        stop_on_error=request.POST.get("stop_on_error") == "on",
    )
    return _upload_page(request, ds, result=summary, plan=plan)


# ── in-place row editing ────────────────────────────────────────────────────────────────────────
def _row(request, ds, obj, **extra):
    return render(request, ds.row_template, _ctx(request, ds, r=obj, **extra))


def _form_partial(request, ds, form, obj=None):
    return render(request, "dashboard/_data_row_form.html", _ctx(request, ds, form=form, obj=obj))


def _fallback(request, ds, level: str, message: str):
    (messages.success if level == "success" else messages.error)(request, message)
    return redirect(safe_next(request, ds))


def _get_obj(ds, pk: int):
    try:
        return ds.queryset().get(pk=pk)
    except ds.model.DoesNotExist as exc:
        raise Http404("no such row") from exc


def _form_error_text(form) -> str:
    return "; ".join(bulk.flat_errors(form))[:300] or "Fix the highlighted fields."


@staff_member_required
@require_http_methods(["GET"])
def data_row(request, key: str, pk: int):
    """The read-only row (what Cancel restores)."""
    ds = _dataset(key)
    obj = _get_obj(ds, pk)
    if not _is_htmx(request):
        return redirect(safe_next(request, ds))
    return _row(request, ds, obj)


@staff_member_required
@require_http_methods(["GET", "POST"])
def data_row_new(request, key: str):
    ds = _dataset(key)
    if request.method == "GET":
        if not _is_htmx(request):
            return redirect(reverse("dash-kb-row-new", args=[ds.kb_kind]) if ds.kb_kind else safe_next(request, ds))
        return _form_partial(request, ds, ds.form_cls(initial=ds.new_initial))
    form = ds.form_cls(request.POST)
    if not form.is_valid():
        if not _is_htmx(request):
            return _fallback(request, ds, "error", _form_error_text(form))
        return _toasted(_form_partial(request, ds, form), "error", _form_error_text(form))
    obj = form.save()
    if not _is_htmx(request):
        return _fallback(request, ds, "success", "Saved")
    return _toasted(_row(request, ds, obj), "success", "Saved")


@staff_member_required
@require_http_methods(["GET", "POST"])
def data_row_edit(request, key: str, pk: int):
    ds = _dataset(key)
    obj = _get_obj(ds, pk)
    if request.method == "GET":
        if not _is_htmx(request):
            return redirect(safe_next(request, ds))
        return _form_partial(request, ds, ds.form_cls(instance=obj), obj)
    form = ds.form_cls(request.POST, instance=obj)
    if not form.is_valid():
        if not _is_htmx(request):
            return _fallback(request, ds, "error", _form_error_text(form))
        return _toasted(_form_partial(request, ds, form, obj), "error", _form_error_text(form))
    obj = form.save()
    if not _is_htmx(request):
        return _fallback(request, ds, "success", "Saved")
    return _toasted(_row(request, ds, obj), "success", "Saved")


@staff_member_required
@require_POST
def data_row_delete(request, key: str, pk: int):
    ds = _dataset(key)
    obj = _get_obj(ds, pk)
    blocked = ds.can_delete(obj) if ds.can_delete else ""
    if not blocked:
        try:
            obj.delete()
        except ProtectedError:
            blocked = "other rows still depend on it"
    if blocked:
        text = f"Not deleted: {blocked}."
        if not _is_htmx(request):
            return _fallback(request, ds, "error", text)
        return _toasted(_row(request, ds, obj), "error", text)
    if not _is_htmx(request):
        return _fallback(request, ds, "success", "Deleted")
    return _toasted(HttpResponse(""), "info", "Deleted")


# ── Edit all ────────────────────────────────────────────────────────────────────────────────────
def _grid(request, ds, rows, forms_by_pk=None, *, error: str = ""):
    items = [(o, (forms_by_pk or {}).get(o.pk) or ds.form_cls(instance=o, prefix=f"r{o.pk}")) for o in rows]
    ctx = _ctx(request, ds, items=items, error=error)
    ctx["edit_qs"] = request.GET.urlencode()
    tpl = "dashboard/_data_edit_all.html" if _is_htmx(request) else "dashboard/data_edit_all.html"
    return render(request, tpl, ctx)


@staff_member_required
@require_http_methods(["GET", "POST"])
def data_edit_all(request, key: str):
    """Every visible row as inputs with one "Save all".

    Transaction semantics: ALL-OR-NOTHING. Every row is validated first with its own ModelForm; if
    any row fails, nothing at all is saved, the failing rows are highlighted with their errors and
    every other row keeps what was typed. Only when all rows are valid are the changed ones written,
    in a single transaction, with the side-effect hook run once (``bulk.apply_plan``)."""
    ds = _dataset(key)
    if request.method == "GET":
        return _grid(request, ds, _visible(ds, request.GET))
    ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
    rows = list(ds.queryset().filter(pk__in=ids))
    rows.sort(key=lambda o: ids.index(o.pk))
    forms_by_pk = {o.pk: ds.form_cls(request.POST, instance=o, prefix=f"r{o.pk}") for o in rows}
    plan = bulk.Plan()
    for i, o in enumerate(rows, start=1):
        plan.rows.append(bulk.judge(ds, i, o, forms_by_pk[o.pk], (str(o),)))
    if plan.counts["error"]:
        n = plan.counts["error"]
        text = f"{n} row{'s need' if n != 1 else ' needs'} fixing. Nothing was saved."
        if not _is_htmx(request):
            return _grid(request, ds, rows, forms_by_pk, error=text)
        return _toasted(_grid(request, ds, rows, forms_by_pk, error=text), "error", text)
    summary = bulk.apply_plan(ds, plan, user=request.user, action="edit-all", stop_on_error=True)
    if summary["failed"]:
        return _toasted(_grid(request, ds, rows, forms_by_pk, error=summary["failed"]), "error", summary["failed"])
    changed = summary["created"] + summary["updated"]
    text = f"Saved {changed} row{'s' if changed != 1 else ''}" + (
        f" ({summary['unchanged']} unchanged)." if summary["unchanged"] else ".")
    messages.success(request, text)
    if _is_htmx(request):
        resp = HttpResponse("")
        resp["HX-Refresh"] = "true"
        return resp
    return redirect(safe_next(request, ds))


# ── Row actions over a selection ────────────────────────────────────────────────────────────────
@staff_member_required
@require_POST
def data_bulk_action(request, key: str):
    """activate / deactivate / delete / set one field, over the ticked rows. All-or-nothing, like
    Edit all: one invalid row (or a delete a category refuses) stops the whole action."""
    ds = _dataset(key)
    ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
    objs = list(ds.queryset().filter(pk__in=ids))
    action = request.POST.get("action", "")
    problem = ""
    plan = bulk.Plan()
    if not objs:
        problem = "Tick at least one row first."
    elif action in ("activate", "deactivate") and ds.active_field:
        plan = bulk.plan_edits(ds, [(o, {ds.active_field: "yes" if action == "activate" else "no"}) for o in objs])
    elif action == "set":
        field = request.POST.get("field", "")
        if field not in {n for n, _l in settable_fields(ds)}:
            problem = "Choose which field to set."
        else:
            plan = bulk.plan_edits(ds, [(o, {field: request.POST.get("value", "").strip()}) for o in objs])
    elif action == "delete":
        for i, o in enumerate(objs, start=1):
            blocked = ds.can_delete(o) if ds.can_delete else ""
            plan.rows.append(
                bulk.RowPlan(i, "error", (str(o),), errors=[f"cannot delete: {blocked}"]) if blocked
                else bulk.RowPlan(i, "delete", (str(o),), obj=o)
            )
    else:
        problem = "Unknown action."
    if not problem and plan.counts["error"]:
        first = next(r for r in plan.rows if r.action == "error")
        problem = f"{first.key_display}: {'; '.join(first.errors)} Nothing was changed."
    if problem:
        if not _is_htmx(request):
            return _fallback(request, ds, "error", problem)
        resp = HttpResponse("")
        resp["HX-Reswap"] = "none"
        return _toasted(resp, "error", problem)
    summary = bulk.apply_plan(ds, plan, user=request.user, action=f"bulk-{action}", stop_on_error=True)
    if summary["failed"]:
        return _fallback(request, ds, "error", summary["failed"]) if not _is_htmx(request) else _toasted(
            HttpResponse(""), "error", summary["failed"])
    n = summary["updated"] + summary["deleted"]
    verb = "Deleted" if action == "delete" else "Updated"
    messages.success(request, f"{verb} {n} row{'s' if n != 1 else ''}"
                     + (f" ({summary['unchanged']} already up to date)." if summary["unchanged"] else "."))
    if _is_htmx(request):
        resp = HttpResponse("")
        resp["HX-Refresh"] = "true"
        return resp
    return redirect(safe_next(request, ds))
