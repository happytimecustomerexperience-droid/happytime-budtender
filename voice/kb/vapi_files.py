"""Mirror the curated KB to Vapi Files + a Query Tool (22-SPEC-kb-seed.md §6) — the
low-latency grounded FALLBACK (path 1). Canonical truth stays in ``kb/`` (read live by
``voice/tools/faq.py`` via ``kb.semantic.rank_faq`` — path 2); this is re-pushed on every
reindex and is NEVER the source of truth, NEVER read by ``faq_lookup``.

``mirror_all()`` renders the active KB into ≤300KB markdown files (one per source group),
uploads them via ``core/services/vapi.py`` (find-by-name-then-replace → idempotent, no
duplicate creates on a re-mirror), and attaches/updates a Vapi Query Tool on the entry_faq
assistant. Safe no-op when ``VAPI_PRIVATE_KEY`` is unset → ``{"skipped": "not configured"}``
(the canonical ``faq_lookup`` path still answers from ``kb/``).

The exact Files-API endpoint shapes live in 20-SPEC-vapi-deploy.md; this module states only
what it calls (``/file`` CRUD via the vapi verb primitives, ``/tool`` for the Query Tool).
"""

from __future__ import annotations

import logging

from core.services import vapi

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 300 * 1024  # Vapi Files per-file cap (22-SPEC §6 + E1)
QUERY_TOOL_NAME = "kb_query"  # the Vapi Query Tool attached to entry_faq


# ── Render: one markdown body per source group ────────────────────────────────


def _h(title: str) -> str:
    return f"# {title}\n\n"


def _render_faq() -> str:
    from kb.models import FAQEntry

    out = [_h("Happy Time — FAQ")]
    rows = FAQEntry.objects.filter(is_active=True).order_by("store", "-weight", "key")
    by_store: dict[str, list] = {}
    for r in rows:
        by_store.setdefault(r.store or "all stores", []).append(r)
    for store, items in by_store.items():
        out.append(f"## {store}\n\n")
        for r in items:
            out.append(r.chunk_text() + "\n\n")
    return "".join(out)


def _render_return_policy() -> str:
    from kb.models import PolicyDocument

    out = [_h("Return policy")]
    for p in PolicyDocument.objects.filter(is_active=True, kind="return_policy"):
        out.append(p.chunk_text() + "\n\n")
    return "".join(out)


def _render_store_facts() -> str:
    from kb.models import StoreFact

    out = [_h("Store facts")]
    rows = StoreFact.objects.filter(is_active=True).exclude(kind="limit").order_by("store", "kind")
    by_store: dict[str, list] = {}
    for r in rows:
        by_store.setdefault(r.store or "all stores", []).append(r)
    for store, items in by_store.items():
        out.append(f"## {store}\n\n")
        for r in items:
            out.append(r.chunk_text() + "\n\n")
    return "".join(out)


def _render_wa_law() -> str:
    from kb.models import StoreFact, WeightTypeTaxonomy

    out = [_h("Washington law — purchase limits + age")]
    for r in StoreFact.objects.filter(is_active=True, kind__in=["limit", "age"]).order_by("label"):
        out.append(r.chunk_text() + "\n\n")
    out.append("## Limits (reference)\n\n")
    for r in WeightTypeTaxonomy.objects.filter(is_active=True, axis="limit").order_by("id"):
        out.append(r.chunk_text() + "\n\n")
    return "".join(out)


def _render_weights_types() -> str:
    from kb.models import WeightTypeTaxonomy

    out = [_h("Weights + types reference")]
    axes = [a for a, _ in WeightTypeTaxonomy.AXES if a != "limit"]
    for axis in axes:
        rows = WeightTypeTaxonomy.objects.filter(is_active=True, axis=axis).order_by("id")
        if not rows:
            continue
        out.append(f"## {axis}\n\n")
        for r in rows:
            out.append(r.chunk_text() + "\n\n")
    return "".join(out)


def _render_education() -> str:
    from kb.models import BlogDoc, EducationDoc

    out = [_h("Education + blog")]
    out.append("## Education\n\n")
    for r in EducationDoc.objects.filter(is_active=True).order_by("topic", "slug"):
        out.append(r.chunk_text() + "\n\n")
    out.append("## Blog\n\n")
    for r in BlogDoc.objects.filter(is_active=True).order_by("slug"):
        out.append(r.chunk_text() + "\n\n")
    return "".join(out)


# name (without extension) -> renderer
_RENDERERS = {
    "faq": _render_faq,
    "return-policy": _render_return_policy,
    "store-facts": _render_store_facts,
    "wa-law": _render_wa_law,
    "weights-types": _render_weights_types,
    "education": _render_education,
}


def _render_file(kind: str) -> str:
    """Render one markdown body. Asserts the ≤300KB cap (E1) — if a body ever overflows it is
    split by the caller; today every group is far under the cap."""
    body = _RENDERERS[kind]()
    if len(body.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(
            f"KB file {kind!r} exceeds the {MAX_FILE_BYTES} byte Vapi cap "
            f"({len(body.encode('utf-8'))} bytes) — split it (e.g. {kind}-2.md)."
        )
    return body


# ── Upload (find-by-name-then-replace) + Query Tool ───────────────────────────


def _find_file_by_name(name: str) -> dict | None:
    files = vapi.get("/file") or []
    for f in files:
        if (f or {}).get("name") == name:
            return f
    return None


def _upload_file(name: str, body: str) -> dict:
    """Idempotent upload: delete the prior file of that name, then create the new one (so a
    re-mirror never duplicates)."""
    prior = _find_file_by_name(name)
    if prior and prior.get("id"):
        try:
            vapi.delete(f"/file/{prior['id']}")
        except vapi.VapiError:
            logger.warning("vapi_files: could not delete prior file %s", name, exc_info=True)
    created = vapi.post("/file", {"name": name, "content": body, "mimeType": "text/markdown"})
    return created or {}


def ensure_query_tool(file_ids: list[str]) -> str:
    """Find-or-create the Vapi Query Tool referencing the uploaded file ids; return its id.
    GET-then-PATCH (idempotent)."""
    existing = vapi.find_tool_by_name(QUERY_TOOL_NAME)
    body = {
        "type": "query",
        "function": {"name": QUERY_TOOL_NAME},
        "knowledgeBases": [{"provider": "google", "name": "happy-time-kb", "fileIds": file_ids}],
    }
    if existing and existing.get("id"):
        vapi.patch(f"/tool/{existing['id']}", body)
        return existing["id"]
    created = vapi.create_tool(body)
    return (created or {}).get("id", "")


def _attach_tool_to_entry_faq(tool_id: str) -> None:
    """Attach the Query Tool's id to the entry_faq assistant's toolIds (alongside faq_lookup).
    Best-effort: a missing assistant (not yet provisioned) is a clean skip, not a crash."""
    if not tool_id:
        return
    assistant = vapi.find_assistant_by_name("entry_faq")
    if not assistant or not assistant.get("id"):
        logger.info("vapi_files: entry_faq assistant not provisioned yet; tool not attached")
        return
    model = assistant.get("model") or {}
    tool_ids = list(model.get("toolIds") or [])
    if tool_id not in tool_ids:
        tool_ids.append(tool_id)
        model["toolIds"] = tool_ids
        vapi.patch_assistant(assistant["id"], {"model": model})


def mirror_all() -> dict:
    """Render → upload → attach. Returns ``{files:[{name,id}…], tool_id}`` or, with
    ``VAPI_PRIVATE_KEY`` unset, ``{"skipped": "not configured"}`` — the canonical
    ``faq_lookup`` path still answers from ``kb/``."""
    if not vapi.configured():
        return {"skipped": "not configured"}
    files: list[dict] = []
    for kind in _RENDERERS:
        name = f"{kind}.md"
        body = _render_file(kind)
        created = _upload_file(name, body)
        files.append({"name": name, "id": created.get("id", "")})
    file_ids = [f["id"] for f in files if f["id"]]
    tool_id = ensure_query_tool(file_ids)
    _attach_tool_to_entry_faq(tool_id)
    return {"files": files, "tool_id": tool_id}
