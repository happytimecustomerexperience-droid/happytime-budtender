"""Staff dashboard for the Happy Time voice stack (14-P4).

The owner manages the whole voice stack from one Django dashboard: edits every assistant
prompt/model/voice/tool/transfer-number, edits the KB + ranking weights, reviews every call /
vendor-callback / escalation, and clicks **Publish to Vapi** to push the local config to the live
Squad via ``PATCH /assistant/{id}`` + ``PATCH /squad/{id}``.

Ported from swedish-bot/dashboard/views.py (``_toast``/``_resolve_sort``/``_querystring``/``PER_PAGE``
verbatim; ``agent_config``/``agent_save``/``agent_detail``/``agent_prompt_assist``/``flow_canvas``/
``_clean_graph``/``_coord``/``flow_save``/``default_flow_graph`` adapted to the 5-member voice Squad).

Boundaries (binding, §1.3):
  * Every view is ``@staff_member_required``.
  * The flow canvas is config + docs ONLY — ``_clean_graph`` is fail-closed (role allowlist = the 5
    members, node-kind allowlist, MAX_NODES=80, coord clamp, char caps); a Publish re-asserts the
    required Squad transitions from CODE, so a guardrail/required-transition can NEVER be deleted
    from the UI.
  * Numbers-Guard + Leak-Guard hold: the weights tuner edits weights, never per-product cost/margin;
    no view renders a product cost/margin (the data is never present).
"""

from __future__ import annotations

import json

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

PER_PAGE = 25


# ── helpers (ported verbatim from swedish-bot) ────────────────────────────────
def _toast(type_: str, message: str) -> str:
    """Serialize an HX-Trigger 'toast' payload for the base.html toast stack."""
    return json.dumps({"toast": {"type": type_, "message": message}})


def _resolve_sort(request, allowed: dict[str, str], default: str) -> tuple[str, str, str]:
    """Validate ?sort= against an allowlist → (order_by, active_field, direction). A leading '-'
    means descending. Anything not in ``allowed`` falls back to ``default`` (no SQL-injection
    surface — only allowlisted column names ever reach ``.order_by()``)."""
    raw = (request.GET.get("sort") or "").strip()
    field, desc = raw.lstrip("-"), raw.startswith("-")
    if field not in allowed:
        raw = default
        field, desc = raw.lstrip("-"), raw.startswith("-")
    column = allowed[field]
    order_by = f"-{column}" if desc else column
    return order_by, field, ("desc" if desc else "asc")


def _querystring(request, *drop: str) -> str:
    """URL-encoded current GET params minus ``drop`` (preserves filters across pagination). Always
    drops 'page'."""
    params = request.GET.copy()
    for key in ("page", *drop):
        params.pop(key, None)
    return params.urlencode()


# ── Overview + Analytics ──────────────────────────────────────────────────────
@staff_member_required
def overview(request):
    """KPI pills + the at-risk banner over the durable call log."""
    from django.db.models import Count

    from crm.models import VendorCallback
    from voice.models import VoiceCall

    qs = VoiceCall.objects.all()
    ctx = {
        "total": qs.count(),
        "escalations": qs.filter(outcome="escalation").count(),
        "vendor_callbacks": VendorCallback.objects.filter(status="open").count(),
        "suggested": qs.filter(outcome="suggested").count(),
        "by_outcome": list(
            qs.exclude(outcome="").values("outcome").annotate(n=Count("id")).order_by("-n")
        ),
        "by_store": list(
            qs.exclude(store="").values("store").annotate(n=Count("id")).order_by("-n")
        ),
    }
    return render(request, "dashboard/overview.html", ctx)


@staff_member_required
def analytics_dashboard(request):
    """Call-volume + outcome funnel + escalation/vendor rates over ``days∈{7,30,90}``."""
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    from voice.models import VoiceCall

    try:
        days = int(request.GET.get("days") or 30)
    except ValueError:
        days = 30
    days = days if days in (7, 30, 90) else 30
    since = timezone.now() - timedelta(days=days)
    qs = VoiceCall.objects.filter(created_at__gte=since)
    total = qs.count()
    funnel = list(qs.exclude(outcome="").values("outcome").annotate(n=Count("id")).order_by("-n"))
    ctx = {
        "days": days,
        "total": total,
        "funnel": funnel,
        "by_store": list(
            qs.exclude(store="").values("store").annotate(n=Count("id")).order_by("-n")
        ),
        "top_asks": _top_product_asks(qs),
        "escalations": qs.filter(outcome="escalation").count(),
        "vendor_callbacks": qs.filter(outcome="vendor_callback").count(),
        "suggested": qs.filter(outcome="suggested").count(),
    }
    return render(request, "dashboard/analytics.html", ctx)


def _top_product_asks(qs, limit: int = 10) -> list[dict]:
    """Top product asks = the most-suggested SKUs across the window's calls — a REAL count over the
    durable ``VoiceCall.suggested_skus`` lists (Numbers-Guard: a count of real rows, no LLM math, no
    fabrication). Leak-safe by construction (a SKU is an id, never cost/margin). Returns
    ``[{sku, n}…]`` ranked desc. The "top categories" rollup at this scale (no per-call category
    column on ``VoiceCall``) — extend with a category field if/when one is captured per call."""
    from collections import Counter

    counter: Counter = Counter()
    for skus in qs.values_list("suggested_skus", flat=True):
        for sku in skus or []:
            if sku:
                counter[str(sku)] += 1
    return [{"sku": sku, "n": n} for sku, n in counter.most_common(limit)]


# ── Agents editor (port agent_config/agent_save/agent_detail + voice fields) ───
def _agent_card_ctx(prompt, *, saved=False, error=""):
    return {
        "p": prompt,
        "saved": saved,
        "error": error,
        "transfer_keys": ["", "YAKIMA", "MTVERNON", "PULLMAN"],
    }


@staff_member_required
def agent_config(request):
    from kb.models import AgentPrompt

    prompts = AgentPrompt.objects.order_by("role")
    return render(
        request,
        "dashboard/agent_config.html",
        {"prompts": prompts, "transfer_keys": ["", "YAKIMA", "MTVERNON", "PULLMAN"]},
    )


@staff_member_required
@require_POST
def agent_save(request, pk: int):
    """Inline-save one assistant's editable config incl. the voice fields (``vapi_model``/
    ``voice_id``/``tool_names``/``transfer_number_key``). Fail-closed numeric validation; the
    hard-coded safety baseline stays in ``voice/guardrails.py`` (this only tunes prompt + knobs)."""
    from kb.models import AgentPrompt

    p = get_object_or_404(AgentPrompt, pk=pk)
    errors: list[str] = []

    for f in ("body", "vapi_model", "voice_id"):
        if f in request.POST:
            setattr(p, f, request.POST[f])

    # tool_names: comma- or whitespace-separated → list (the multiselect posts repeated values).
    if "tool_names" in request.POST:
        names = request.POST.getlist("tool_names")
        if len(names) == 1 and ("," in names[0] or " " in names[0]):
            names = [t.strip() for t in names[0].replace(",", " ").split() if t.strip()]
        p.tool_names = [n for n in names if n]

    key = (request.POST.get("transfer_number_key") or "").strip().upper()
    if key and key not in ("YAKIMA", "MTVERNON", "PULLMAN"):
        errors.append("transfer_number_key: unknown")
    else:
        p.transfer_number_key = key

    def _num(field, cast, *, lo=None, hi=None, default=None):
        raw = (request.POST.get(field) or "").strip()
        if raw == "":
            return default
        try:
            val = cast(raw)
        except ValueError:
            errors.append(f"{field}: not a number")
            return getattr(p, field)
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            errors.append(f"{field}: out of range")
            return getattr(p, field)
        return val

    p.temperature = _num("temperature", float, lo=0.0, hi=2.0, default=None)
    p.max_output_tokens = _num("max_output_tokens", int, lo=1, hi=65536, default=None)
    p.is_active = request.POST.get("is_active") == "on"

    if not errors:
        p.save()
    resp = render(
        request,
        "dashboard/_agent_card.html",
        _agent_card_ctx(p, saved=not errors, error="; ".join(errors)),
    )
    if errors:
        resp["HX-Trigger"] = _toast("error", "; ".join(errors))
    else:
        resp["HX-Trigger"] = _toast(
            "success", f"{p.role} updated — live server-side; click Publish to push to Vapi ✓"
        )
    return resp


@staff_member_required
def agent_detail(request, role: str):
    """Per-assistant full editor page (reached from the flow map)."""
    from kb.models import AgentPrompt

    from .flowgraph import VOICE_AGENT_FLOW

    p = get_object_or_404(AgentPrompt, role=role)
    ctx = _agent_card_ctx(p)
    ctx["info"] = VOICE_AGENT_FLOW.get(role, {})
    return render(request, "dashboard/agent_detail.html", ctx)


# ── agent_prompt_assist (Gemini additive editor, ported verbatim in behavior) ──
_ASSIST_SYSTEM = (
    "You are a careful prompt engineer editing the SYSTEM PROMPT of a production AI voice "
    "budtender for Happy Time Weed (a family-owned Washington cannabis retailer; persona "
    "'Koptza'). You receive the agent's CURRENT full system prompt and an ADMINISTRATOR "
    "INSTRUCTION describing an ADDITION to make.\n"
    "Rules:\n"
    "- Return the COMPLETE updated system prompt, ready to use as-is.\n"
    "- PRESERVE all existing content verbatim. Do NOT remove, weaken, reorder, or reword "
    "existing rules — especially safety guardrails, age-gate rules, leak rules, and escalation "
    "rules.\n"
    "- ADD only what the instruction asks, integrated into the most appropriate existing section "
    "or a clearly-labelled new section, matching the existing style, structure, and headings. "
    "Keep any {placeholder} tokens intact.\n"
    "- If the instruction would weaken or remove a safety rule, IGNORE that part and instead ADD a "
    "clarifying, stricter safe rule. Never reduce safety.\n"
    "- Output ONLY the new full prompt text — no preamble, no explanation, no markdown fences."
)


@staff_member_required
@require_POST
def agent_prompt_assist(request, pk: int):
    """AI-assisted prompt editing: Gemini reads the FULL current prompt + the admin's instruction
    and returns the complete prompt with only the addition. PROPOSED, never auto-saved (the admin
    reviews + clicks Save). Code guardrails remain the real boundary."""
    from core.constants import MODELS
    from core.services import gemini
    from kb.models import AgentPrompt

    p = get_object_or_404(AgentPrompt, pk=pk)
    instruction = (request.POST.get("instruction") or "").strip()[:2000]
    if not instruction:
        return JsonResponse({"ok": False, "error": "Describe what to add."}, status=400)
    contents = (
        f"CURRENT SYSTEM PROMPT:\n<<<\n{p.body}\n>>>\n\n"
        f"ADMINISTRATOR INSTRUCTION (additions to make):\n<<<\n{instruction}\n>>>\n\n"
        "Return the complete updated system prompt now."
    )
    try:
        resp = gemini.generate(
            contents,
            model=MODELS.get("flash") or p.vapi_model,
            system_instruction=_ASSIST_SYSTEM,
            temperature=0.2,
            max_output_tokens=8192,
        )
    except Exception:  # noqa: BLE001 — surface a clean error, never 500 the dashboard
        return JsonResponse(
            {"ok": False, "error": "AI service unavailable — try again."}, status=502
        )
    body = (resp.text or "").strip()
    if body.startswith("```"):  # strip accidental code fences
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if not body:
        return JsonResponse({"ok": False, "error": "No proposal returned."}, status=502)
    return JsonResponse({"ok": True, "body": body, "chars": len(body)})


# ── Flow canvas (config + docs only) ───────────────────────────────────────────
@staff_member_required
@ensure_csrf_cookie
def flow_canvas(request):
    from kb.models import AgentPrompt

    from .flowgraph import NODE_KINDS, get_flow

    cfg = get_flow()
    agents = {
        a.role: {
            "pk": a.pk,
            "display": a.get_role_display(),
            "vapi_model": a.vapi_model,
            "voice_id": a.voice_id,
            "tool_names": a.tool_names,
            "transfer_number_key": a.transfer_number_key,
            "temperature": a.temperature,
            "max_output_tokens": a.max_output_tokens,
            "is_active": a.is_active,
            "prompt_version": a.prompt_version,
            "body": a.body,
        }
        for a in AgentPrompt.objects.all()
    }
    return render(
        request,
        "dashboard/flow.html",
        {"graph": cfg.graph, "agents": agents, "kinds": NODE_KINDS},
    )


@staff_member_required
@require_POST
def flow_save(request):
    from kb.models import FlowConfig

    from .flowgraph import clean_graph, get_flow

    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "bad JSON"}, status=400)
    graph, err = clean_graph(data)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=400)
    cfg, _ = FlowConfig.objects.get_or_create(pk=get_flow().pk)
    cfg.graph = graph
    cfg.save(update_fields=["graph", "updated_at"])
    return JsonResponse({"ok": True, "nodes": len(graph["nodes"]), "edges": len(graph["edges"])})


# ── KB manager + KB-source manager + reindex ──────────────────────────────────
@staff_member_required
def kb_manager(request):
    """KB landing: one card per KB source kind with a live count."""
    from .forms import KB_KINDS

    groups = [
        {"kind": slug, "label": label, "count": model.objects.count()}
        for slug, (model, label) in KB_KINDS.items()
    ]
    return render(request, "dashboard/kb_manager.html", {"groups": groups})


@staff_member_required
def kb_source_list(request, kind: str):
    from .forms import KB_KINDS

    if kind not in KB_KINDS:
        return redirect("dash-kb")
    model, label = KB_KINDS[kind]
    qs = model.objects.all()
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q

        text_fields = [
            f.name
            for f in model._meta.fields
            if f.get_internal_type() in ("TextField", "CharField", "SlugField")
        ]
        cond = Q()
        for f in text_fields:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    page_obj = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    return render(
        request,
        "dashboard/kb_source.html",
        {
            "kind": kind,
            "label": label,
            "rows": page_obj,
            "page_obj": page_obj,
            "q": q,
            "querystring": _querystring(request),
        },
    )


@staff_member_required
def kb_row_new(request, kind: str):
    from .forms import KB_FORMS, KB_KINDS

    if kind not in KB_FORMS:
        return redirect("dash-kb")
    form_cls = KB_FORMS[kind]
    _model, label = KB_KINDS[kind]
    if request.method == "POST":
        form = form_cls(request.POST)
        if form.is_valid():
            form.save()
            resp = redirect("dash-kb-source", kind=kind)
            resp["HX-Trigger"] = _toast("success", f"{label} row added — live on the next call.")
            return resp
    else:
        form = form_cls()
    return render(
        request,
        "dashboard/kb_form.html",
        {"form": form, "kind": kind, "label": label, "is_new": True},
    )


@staff_member_required
def kb_row_edit(request, pk: int):
    """Edit a KB row. The kind is resolved from ?kind= (set by the source list links)."""
    from .forms import KB_FORMS, KB_KINDS

    kind = (request.GET.get("kind") or request.POST.get("kind") or "").strip()
    if kind not in KB_FORMS:
        return redirect("dash-kb")
    model, label = KB_KINDS[kind]
    obj = get_object_or_404(model, pk=pk)
    form_cls = KB_FORMS[kind]
    if request.method == "POST":
        form = form_cls(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            resp = redirect("dash-kb-source", kind=kind)
            resp["HX-Trigger"] = _toast("success", f"{label} row updated — live on the next call.")
            return resp
    else:
        form = form_cls(instance=obj)
    return render(
        request,
        "dashboard/kb_form.html",
        {"form": form, "kind": kind, "label": label, "is_new": False, "obj": obj},
    )


@staff_member_required
@require_POST
def kb_row_delete(request, pk: int):
    from .forms import KB_KINDS

    kind = (request.GET.get("kind") or request.POST.get("kind") or "").strip()
    if kind not in KB_KINDS:
        return redirect("dash-kb")
    model, label = KB_KINDS[kind]
    get_object_or_404(model, pk=pk).delete()
    resp = redirect("dash-kb-source", kind=kind)
    resp["HX-Trigger"] = _toast("info", f"{label} row deleted.")
    return resp


@staff_member_required
@require_POST
def kb_reindex(request):
    """Rebuild the kb/semantic.py cosine cache AND re-mirror to Vapi Files. Bounded (the KB is
    small at this scale); degrades cleanly when the Vapi file API is unconfigured."""
    from kb import semantic, vapi_files

    chunks = 0
    try:
        chunks = semantic.reindex()
    except Exception:  # noqa: BLE001 — never 500 the dashboard
        chunks = 0
    mirror = vapi_files.mirror_all()
    if mirror.get("skipped"):
        msg = f"{chunks} chunks reindexed; Vapi mirror skipped ({mirror['skipped']})."
    else:
        files = len(mirror.get("files", []))
        msg = f"{chunks} chunks reindexed, {files} files mirrored."
    resp = redirect("dash-kb")
    resp["HX-Trigger"] = _toast("success", msg)
    return resp


# ── Ranking-weights tuner ──────────────────────────────────────────────────────
@staff_member_required
def weights_tuner(request):
    from . import weights as W
    from .forms import RankingWeightsForm

    current = W.get_weights()
    sync = None
    if request.method == "POST":
        form = RankingWeightsForm(request.POST)
        if form.is_valid():
            saved = W.save_weights(
                w_anon=form.cleaned_data["w_anon"],
                w_known=form.cleaned_data["w_known"],
                margin_emphasis=form.cleaned_data["margin_emphasis"],
            )
            sync = W.push_to_budtender(saved)
            current = saved
        # rebuild the textareas with the just-posted values
    else:
        form = RankingWeightsForm(
            initial={
                "w_anon": json.dumps(current.w_anon, indent=2),
                "w_known": json.dumps(current.w_known, indent=2),
                "margin_emphasis": current.margin_emphasis,
            }
        )
    return render(
        request,
        "dashboard/weights.html",
        {
            "form": form,
            "current": current,
            "preview_anon": W.normalize(current.w_anon),
            "preview_known": W.normalize(current.w_known),
            "warnings": getattr(form, "warnings", []),
            "sync": sync,
        },
    )


# ── Call monitor / log / detail / transcript ───────────────────────────────────
@staff_member_required
def call_monitor(request):
    """Live (in-flight) + recent calls. The live strip polls via HTMX ``every 5s``."""
    from . import monitor

    if request.headers.get("HX-Request") and request.GET.get("strip") == "live":
        return render(
            request,
            "dashboard/_calls_live.html",
            {"live": monitor.live_calls(), "badge": monitor.call_outcome_badge},
        )
    return render(
        request,
        "dashboard/calls.html",
        {
            "live": monitor.live_calls(),
            "recent": monitor.recent_calls(),
            "badge": monitor.call_outcome_badge,
        },
    )


_CALL_SORTS = {
    "id": "pk",
    "store": "store",
    "outcome": "outcome",
    "duration": "duration_s",
    "created_at": "created_at",
}


@staff_member_required
def call_log(request, _outcomes: list[str] | None = None):
    """Paginated/searchable/sortable VoiceCall log. ``_outcomes`` pre-filters (escalation review)."""
    from django.db.models import Q

    from voice.models import VoiceCall

    from . import monitor

    qs = VoiceCall.objects.all()
    if _outcomes:
        qs = qs.filter(outcome__in=_outcomes)
    outcome = request.GET.get("outcome", "")
    store = request.GET.get("store", "")
    q = (request.GET.get("q") or "").strip()
    if outcome:
        qs = qs.filter(outcome=outcome)
    if store:
        qs = qs.filter(store=store)
    if q:
        qs = qs.filter(
            Q(outcome__icontains=q)
            | Q(store__icontains=q)
            | Q(caller_phone_hash__istartswith=q)
            | Q(reason__icontains=q)
            | Q(ai_summary__icontains=q)
        )
    order_by, sort_field, sort_dir = _resolve_sort(request, _CALL_SORTS, "-created_at")
    qs = qs.order_by(order_by)
    page_obj = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    return render(
        request,
        "dashboard/call_log.html",
        {
            "calls": page_obj,
            "page_obj": page_obj,
            "querystring": _querystring(request),
            "base_qs": _querystring(request, "sort"),
            "q": q,
            "outcome": outcome,
            "store": store,
            "sort_field": sort_field,
            "sort_dir": sort_dir,
            "badge": monitor.call_outcome_badge,
        },
    )


@staff_member_required
def call_detail(request, pk: int):
    from voice.models import VoiceCall

    from . import monitor

    call = get_object_or_404(VoiceCall, pk=pk)
    return render(
        request,
        "dashboard/call_detail.html",
        {
            "call": call,
            "turns": call.turns.order_by("seq"),
            "badge": monitor.call_outcome_badge,
        },
    )


@staff_member_required
def call_transcript(request, pk: int):
    """Read-only VoiceTurn bubble replay (popup/modal partial)."""
    from voice.models import VoiceCall

    call = get_object_or_404(VoiceCall, pk=pk)
    return render(
        request,
        "dashboard/_call_transcript.html",
        {"call": call, "turns": call.turns.order_by("seq")},
    )


@staff_member_required
def escalation_review(request):
    """The call log pre-filtered to escalation outcomes (a thin wrapper over ``call_log``)."""
    return call_log(request, _outcomes=["escalation"])


# ── Specials / hours editor (14-P4 item 5) ─────────────────────────────────────
# The dedicated surface over the StoreFact KB the `faq` assistant speaks: weekly specials +
# per-store hours. CRUD reuses the generic kb-row views (StoreFactForm, kind="store-fact") so
# there is one editor; this view just FILTERS + surfaces the O-8 Mt Vernon "confirmed" gate.
SPECIALS_HOURS_KINDS = ("special", "hours")


@staff_member_required
def specials_hours(request):
    """List the weekly-special + per-store-hours ``StoreFact`` rows the ``faq`` assistant speaks.

    Edits route through the existing kb-row CRUD (``?kind=store-fact``) — one editor, no dup. The
    O-8 Mt Vernon hours gate is surfaced: an unconfirmed row shows "call to confirm" and never gets
    spoken as a fact (``StoreFact.chunk_text`` already enforces this; this view just flags it)."""
    from kb.models import StoreFact

    kind = request.GET.get("kind", "")
    qs = StoreFact.objects.filter(kind__in=SPECIALS_HOURS_KINDS)
    if kind in SPECIALS_HOURS_KINDS:
        qs = qs.filter(kind=kind)
    rows = qs.order_by("kind", "store", "label")
    unconfirmed = qs.filter(confirmed=False).count()
    return render(
        request,
        "dashboard/specials_hours.html",
        {
            "rows": rows,
            "kind": kind,
            "kinds": SPECIALS_HOURS_KINDS,
            "unconfirmed": unconfirmed,
        },
    )


# ── Vendor-callback queue ──────────────────────────────────────────────────────
@staff_member_required
def vendor_queue(request):
    from crm.models import VendorCallback, VendorCallbackStatus

    qs = VendorCallback.objects.all()
    status = request.GET.get("status", "")
    if status:
        qs = qs.filter(status=status)
    page_obj = Paginator(qs.order_by("-created_at"), PER_PAGE).get_page(request.GET.get("page"))
    return render(
        request,
        "dashboard/vendor_queue.html",
        {
            "callbacks": page_obj,
            "page_obj": page_obj,
            "querystring": _querystring(request),
            "status": status,
            "statuses": [s[0] for s in VendorCallbackStatus.choices],
        },
    )


@staff_member_required
@require_POST
def vendor_callback_update(request, pk: int):
    """Mark a vendor callback contacted/closed; optionally re-fire the staff alert."""
    from crm.models import VendorCallback
    from crm.sinks import dispatch

    cb = get_object_or_404(VendorCallback, pk=pk)
    action = request.POST.get("action", "")
    if action == "contacted":
        cb.mark_contacted()
        msg = "Marked contacted."
    elif action == "closed":
        cb.mark_closed()
        msg = "Marked closed."
    elif action == "realert" and cb.voice_call_id:
        dispatch(cb.voice_call)
        msg = "Staff alert re-sent."
    else:
        msg = "No change."
    resp = redirect("dash-vendor-queue")
    resp["HX-Trigger"] = _toast("success", msg)
    return resp


# ── Publish to Vapi ────────────────────────────────────────────────────────────
@staff_member_required
@ensure_csrf_cookie
def publish_page(request):
    """The publish landing — per-object state + the Publish-all / Publish-this buttons."""
    from kb.models import AgentPrompt

    prompts = AgentPrompt.objects.order_by("role")
    return render(request, "dashboard/publish.html", {"prompts": prompts})


@staff_member_required
@require_POST
def publish_vapi(request):
    """Publish ALL members + the squad. Per-object results; the whole action never 500s."""
    from . import publish

    results = [r.to_dict() for r in publish.publish_all()]
    return render(request, "dashboard/_publish_result.html", {"results": results})


@staff_member_required
@require_POST
def publish_assistant_one(request, pk: int):
    """Publish a single assistant (the per-card "Publish this" button) + reconcile the squad."""
    from kb.models import AgentPrompt

    from . import publish

    p = get_object_or_404(AgentPrompt, pk=pk)
    results = [publish.publish_assistant(p).to_dict(), publish.publish_squad().to_dict()]
    return render(request, "dashboard/_publish_result.html", {"results": results})
