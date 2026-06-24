"""Everything-as-code Vapi provisioning + the reconcile engine (20-SPEC-vapi-deploy.md §6;
10-P0-CHASSIS-FAQ.md §6).

Declares the Vapi stack as code and reconciles it create-or-PATCH-by-id-then-by-name with a
zero-drift short-circuit (``VapiObject.last_provision_hash``). The three ``build_*_payload``
builders are the single source of truth for the Vapi JSON shapes — shared with P4's
``dashboard/publish.py`` (one shape, two callers).

P0 scope (what this run stands up): the ONE merged ``entry_faq`` assistant (model gpt-4.1-mini,
Cartesia sonic-3 Koptza voice, Deepgram nova-3 + the 33-term keyterm list, system prompt from the
seeded ``AgentPrompt(role="faq")``, ``server.url`` + serverMessages, the ``faq_lookup`` tool, a
transferPlan placeholder), the ``faq_lookup`` tool, the ONE Squad (single-member container so the
shape is proven), the KB Files + Query Tool mirror, and the phone-number attach. Re-running is a
proven no-op (zero new objects, zero PATCH) — ADR-003.

The builders are written for ALL 5 members (entry_router/budtender/faq/vendor/escalation) so the
later phases only ADD ``AgentPrompt`` rows; ``provision_all`` reconciles whatever members + tools
the CURRENT phase has defined. ``--dry-run`` routes every write through the client's recorder and
issues zero real calls (auto-engaged when ``VAPI_PRIVATE_KEY`` is unset).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from django.conf import settings

from core.services import vapi
from voice import constants as C

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/api/voice/vapi"


# ── Report shapes (§4.11) ──────────────────────────────────────────────────────
@dataclass
class ReconcileResult:
    kind: str
    name: str
    vapi_id: str = ""
    action: str = "nodrift"  # created|patched|nodrift|skipped|error
    changed_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def line(self) -> str:
        tag = self.vapi_id or "-"
        warn = f"  (warn: {'; '.join(self.warnings)})" if self.warnings else ""
        err = f"  ERROR: {self.error}" if self.error else ""
        return f"  {self.kind:<13} {self.name:<26} {self.action:<8} {tag}{warn}{err}"


@dataclass
class ProvisionReport:
    ok: bool = True
    dry_run: bool = False
    results: list[ReconcileResult] = field(default_factory=list)
    error: str | None = None  # a fatal pre-flight error (e.g. auth not configured)

    @property
    def created(self) -> int:
        return sum(r.action == "created" for r in self.results)

    @property
    def patched(self) -> int:
        return sum(r.action == "patched" for r in self.results)

    @property
    def nodrift(self) -> int:
        return sum(r.action == "nodrift" for r in self.results)

    @property
    def skipped(self) -> int:
        return sum(r.action == "skipped" for r in self.results)

    @property
    def errors(self) -> int:
        return sum(r.action == "error" for r in self.results)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "error": self.error,
            "created": self.created,
            "patched": self.patched,
            "nodrift": self.nodrift,
            "skipped": self.skipped,
            "errors": self.errors,
            "results": [
                {
                    "kind": r.kind,
                    "name": r.name,
                    "id": r.vapi_id,
                    "action": r.action,
                    "changed_fields": r.changed_fields,
                    "warnings": r.warnings,
                    "error": r.error,
                }
                for r in self.results
            ],
        }


# ── payload builders (the single source of truth — shared with P4) ──────────────
def _server_block() -> dict:
    """The ``server`` block written into every assistant + tool — ``url`` from PUBLIC_BASE_URL,
    ``secret`` from VAPI_WEBHOOK_SECRET (the secret the webhook gate verifies; 23-SPEC §5)."""
    base = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    return {"url": f"{base}{WEBHOOK_PATH}", "secret": getattr(settings, "VAPI_WEBHOOK_SECRET", "")}


def _voice_block() -> dict:
    """Cartesia sonic-3 Koptza — voiceId overridable via settings.VAPI_VOICE_ID (ADR-011)."""
    voice = dict(C.CARTESIA_VOICE)
    voice["voiceId"] = getattr(settings, "VAPI_VOICE_ID", "") or C.CARTESIA_VOICE["voiceId"]
    return voice


def build_tool_payload(name: str) -> dict:
    """A Vapi ``function`` tool whose ``server.url`` is our webhook; the webhook routes by
    ``function.name`` via TOOL_REGISTRY (ADR-020). Parameters from ``C.TOOL_SPECS`` (§4.5)."""
    spec = C.TOOL_SPECS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
        "server": _server_block(),
        "async": spec.get("async", False),
    }


def _transfer_tool(role: str, warnings: list[str]) -> dict:
    """The built-in ``transferCall`` tool (warm + summaryPlan) inline on vendor/escalation
    ``model.tools`` (§4.8). An unset HHT_TRANSFER_NUMBER_<key> (O-4) → a documented placeholder
    + a warning (never blocks the run)."""
    key = C.MEMBER_TRANSFER_KEY.get(role, "YAKIMA")
    number = getattr(settings, f"HHT_TRANSFER_NUMBER_{key}", "") or ""
    if not number:
        number = C.TRANSFER_NUMBER_PLACEHOLDER
        warnings.append(f"transfer number not configured for {key} (using placeholder)")
    return {
        "type": "transferCall",
        "destinations": [
            {
                "type": "number",
                "number": number,
                "message": "Connecting you to the team now — one moment.",
                "transferPlan": {
                    # Warm transfer where the AI reads the operator a call summary, then connects
                    # the caller. Verified valid against the LIVE Vapi schema (2026-06-23): the old
                    # "warm-transfer-wait-for-operator" mode AND a fallbackPlan{mode:...} were both
                    # rejected with 400. The vendor "no-answer → return control to the AI member"
                    # leg (ADR-015) needs a different Vapi mechanism and is deferred to Phase 3.
                    "mode": "warm-transfer-say-summary",
                    "summaryPlan": {
                        "enabled": True,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Summarize this call for the store operator before "
                                    "connecting. Include: store, what the caller wants, any "
                                    "defective-product/return details (WAC 314-55-079), and the "
                                    "disposition. Transcript: {{transcript}}"
                                ),
                            }
                        ],
                    },
                },
            }
        ],
    }


def _resolve_tool_ids(role: str, warnings: list[str]) -> tuple[list[str], bool]:
    """Resolve a member's custom-tool ids from ``MEMBER_TOOLS`` via the VapiObject map. Returns
    ``(tool_ids, ok)``; ``ok=False`` (with a warning) means a tool is not provisioned → the caller
    must NOT PATCH a dangling toolId (§4.2 / C3)."""
    from voice.models import VapiObject

    ids: list[str] = []
    ok = True
    for tool_name in C.MEMBER_TOOLS.get(role, []):
        rec = VapiObject.objects.filter(kind="tool", name=tool_name).first()
        if rec and rec.vapi_id:
            ids.append(rec.vapi_id)
        else:
            warnings.append(f"tool not provisioned: {tool_name}")
            ok = False
    # The faq member also carries the KB Query Tool id (attached by ensure_files).
    if role == "faq":
        qt = VapiObject.objects.filter(kind="tool", name="kb_query").first()
        if qt and qt.vapi_id and qt.vapi_id not in ids:
            ids.append(qt.vapi_id)
    return ids, ok


def build_assistant_payload(role: str, *, name: str | None = None) -> tuple[dict, list[str]]:
    """The full ``POST/PATCH /assistant`` body for a member (§4.3). Voice/transcriber/model/server
    are emitted ONCE each (ADR-011). The system prompt comes from ``AgentPrompt(role=…).body``;
    the model id is pinned to gpt-4.1-mini (ADR-010). Returns ``(payload, warnings)``."""
    from kb.models import AgentPrompt

    warnings: list[str] = []
    prompt = AgentPrompt.objects.filter(role=role, is_active=True).first()
    body_text = prompt.body if prompt else ""
    if not prompt:
        warnings.append(f"no AgentPrompt(role={role}) — system prompt is empty")

    tool_ids, _tools_ok = _resolve_tool_ids(role, warnings)
    max_tokens = 200 if role == "entry_router" else C.ASSISTANT_MAX_TOKENS

    model: dict = {
        "provider": C.ASSISTANT_PROVIDER,
        "model": C.ASSISTANT_MODEL,
        "temperature": C.ASSISTANT_TEMPERATURE,
        "maxTokens": max_tokens,
        "messages": [{"role": "system", "content": body_text}],
        "toolIds": tool_ids,
    }
    # vendor/escalation carry the built-in transferCall inline (warm + summaryPlan, §4.8).
    if role in ("vendor", "escalation"):
        model["tools"] = [_transfer_tool(role, warnings)]

    payload = {
        "name": name or role,
        "model": model,
        "voice": _voice_block(),
        "transcriber": dict(C.DEEPGRAM_TRANSCRIBER),
        "server": _server_block(),
        "serverMessages": list(C.SERVER_MESSAGES),
    }
    # The entry member OPENS the call with the fixed Happy Time greeting (speaks first, deterministic).
    # Every OTHER member RECEIVES a mid-call handoff, so it must CONTINUE the conversation with a
    # model-generated line off the shared transcript — NOT "assistant-speaks-first" with no
    # firstMessage, which Vapi renders as dead SILENCE on transfer (the bug the owner heard).
    if role == "entry_router":
        payload["firstMessageMode"] = "assistant-speaks-first"
        payload["firstMessage"] = C.ENTRY_FIRST_MESSAGE
    else:
        payload["firstMessageMode"] = "assistant-speaks-first-with-model-generated-message"
    return payload, warnings


def _assistant_name_for_role(role: str) -> str:
    """The provisioned assistant NAME for a member role. P0 ships role ``faq`` under the merged
    name ``entry_faq`` (10-P0 §6.4); every P1 member is provisioned under its own role name. A
    squad destination references a member by its assistant ``name``, so the edge must use this
    name, not the bare role — otherwise the destination dangles (the faq edge)."""
    if role == C.P0_ASSISTANT_ROLE:
        return C.P0_ASSISTANT_NAME
    return role


def build_squad_payload(member_names: dict[str, str]) -> dict:
    """The ``POST/PATCH /squad`` body (§4.7). ``member_names`` maps role → its provisioned
    assistant id (P0 has only ``faq``→entry_faq; P1 adds entry_router/budtender).
    ``assistantDestinations`` come from the code-defined ``SQUAD_SHAPE`` — but only edges whose
    BOTH endpoints are provisioned are emitted, so the squad is a valid container that grows as
    members land. Destinations reference each member by its provisioned assistant NAME."""
    members = []
    for role, dest_list in C.SQUAD_SHAPE.items():
        if role not in member_names:
            continue  # member not provisioned yet (P0 has only faq)
        destinations = []
        for dest_role, description in dest_list:
            if dest_role not in member_names:
                continue  # destination member not provisioned yet — skip the edge
            destinations.append(
                {
                    "type": "assistant",
                    "assistantName": _assistant_name_for_role(dest_role),
                    "message": "",
                    "description": description,
                }
            )
        members.append({"assistantId": member_names[role], "assistantDestinations": destinations})
    return {"name": C.SQUAD_NAME, "members": members}


# ── the generic reconcile (create-or-PATCH, by id then by name, zero-drift) ─────
def _payload_hash(payload: dict) -> str:
    """Stable sha256 of the canonical JSON — the zero-drift oracle. Volatile/secret fields are
    masked so the hash is deterministic across runs (and never embeds a raw secret)."""
    canonical = json.dumps(vapi.redact_payload(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _reconcile(
    kind: str,
    name: str,
    payload: dict,
    *,
    find_by_name: Callable[[str], dict | None],
    get_by_id: Callable[[str], dict],
    create: Callable[[dict], dict],
    patch: Callable[[str, dict], dict],
    warnings: list[str] | None = None,
) -> ReconcileResult:
    """Create-or-PATCH by stored id, falling back to find-by-name — safely re-runnable, zero drift
    (§6.1). A single object's error is isolated (returned as ``action="error"``), never aborts the
    rest."""
    from voice.models import VapiObject

    warnings = warnings or []
    h = _payload_hash(payload)
    try:
        rec = VapiObject.objects.filter(kind=kind, name=name).first()

        # 1) resolve the existing object (stored id first, then find-by-name).
        obj = None
        if rec and rec.vapi_id:
            try:
                obj = get_by_id(rec.vapi_id)
            except vapi.VapiError as exc:
                if exc.status == 404:
                    obj = None  # stale id → fall through to find-by-name
                else:
                    raise
        if obj is None:
            obj = find_by_name(name)

        # 2) zero-drift short-circuit (no Vapi write issued).
        if obj and rec and rec.last_provision_hash == h:
            return ReconcileResult(
                kind, name, obj.get("id", ""), action="nodrift", warnings=warnings
            )

        # 3) create or patch.
        if obj is None:
            obj = create(payload)
            action = "created"
        else:
            obj = patch(obj["id"], payload)
            action = "patched"

        # 4) write back the id + hash (idempotency memory).
        VapiObject.objects.update_or_create(
            kind=kind,
            name=name,
            defaults={"vapi_id": obj.get("id", ""), "last_provision_hash": h},
        )
        return ReconcileResult(kind, name, obj.get("id", ""), action=action, warnings=warnings)
    except vapi.VapiError as exc:
        return ReconcileResult(kind, name, action="error", error=str(exc), warnings=warnings)


# ── the deploy steps ────────────────────────────────────────────────────────────
def ensure_tool(name: str) -> ReconcileResult:
    payload = build_tool_payload(name)
    return _reconcile(
        "tool",
        name,
        payload,
        find_by_name=vapi.find_tool_by_name,
        get_by_id=vapi.get_tool,
        create=vapi.create_tool,
        patch=vapi.patch_tool,
    )


def ensure_assistant(role: str, *, name: str | None = None) -> ReconcileResult:
    """Reconcile one assistant. If a custom tool it needs is not provisioned, the member is
    ``skipped`` with a warning and NO PATCH is sent (never a dangling toolId — C3)."""
    asst_name = name or role
    payload, warnings = build_assistant_payload(role, name=asst_name)
    if any(w.startswith("tool not provisioned") for w in warnings):
        return ReconcileResult("assistant", asst_name, action="skipped", warnings=warnings)

    result = _reconcile(
        "assistant",
        asst_name,
        payload,
        find_by_name=vapi.find_assistant_by_name,
        get_by_id=vapi.get_assistant,
        create=vapi.create_assistant,
        patch=vapi.patch_assistant,
        warnings=warnings,
    )
    # Write the assistant id back onto the AgentPrompt (P4's publish reads it).
    if result.vapi_id and result.action in ("created", "patched"):
        from kb.models import AgentPrompt

        AgentPrompt.objects.filter(role=role).update(vapi_assistant_id=result.vapi_id)
    return result


def ensure_files() -> ReconcileResult:
    """Mirror the KB → Vapi Files + a Query Tool (delegates to ``kb/vapi_files.mirror_all``).
    Records each file as a ``VapiObject(kind="file")`` and the Query Tool id (so the faq assistant
    picks it up). Degrades cleanly when Vapi is unconfigured."""
    from kb import vapi_files
    from voice.models import VapiObject

    try:
        out = vapi_files.mirror_all()
    except vapi.VapiError as exc:
        # The Vapi Files mirror is a REDUNDANT fallback (kb/vapi_files docstring): faq_lookup answers
        # FAQ live from kb/ regardless. A /file API hiccup (e.g. the multipart-vs-JSON upload shape)
        # must NOT fail the whole provision — degrade to a warning skip so the assistants + squad
        # still deploy clean and the call line keeps working.
        return ReconcileResult(
            "file",
            "kb-mirror",
            action="skipped",
            warnings=[f"Vapi file mirror skipped (redundant — faq_lookup covers FAQ): {exc}"],
        )
    if out.get("skipped"):
        return ReconcileResult(
            "file",
            "kb-mirror",
            action="skipped",
            warnings=[f"Vapi mirror skipped ({out['skipped']})"],
        )
    for f in out.get("files", []):
        if f.get("id"):
            VapiObject.objects.update_or_create(
                kind="file", name=f["name"], defaults={"vapi_id": f["id"]}
            )
    tool_id = out.get("tool_id", "")
    if tool_id:
        VapiObject.objects.update_or_create(
            kind="tool", name=vapi_files.QUERY_TOOL_NAME, defaults={"vapi_id": tool_id}
        )
    n = len(out.get("files", []))
    return ReconcileResult(
        "file",
        "kb-mirror",
        vapi_id=tool_id,
        action="patched",
        changed_fields=[f"{n} files + query tool"],
    )


def ensure_squad(member_names: dict[str, str]) -> ReconcileResult:
    payload = build_squad_payload(member_names)
    if not payload["members"]:
        return ReconcileResult(
            "squad", C.SQUAD_NAME, action="skipped", warnings=["no provisioned members yet"]
        )
    result = _reconcile(
        "squad",
        C.SQUAD_NAME,
        payload,
        find_by_name=vapi.find_squad_by_name,
        get_by_id=vapi.get_squad,
        create=vapi.create_squad,
        patch=vapi.patch_squad,
    )
    return result


def ensure_phone_number() -> ReconcileResult:
    """Attach the Squad to the inbound number (``PATCH /phone-number/{id}`` → ``squadId``).
    ``VAPI_PHONE_NUMBER_ID`` unset (O-4) → ``skipped`` (the Squad + assistant still provision)."""
    from voice.models import VapiObject

    number_id = getattr(settings, "VAPI_PHONE_NUMBER_ID", "") or ""
    if not number_id:
        return ReconcileResult(
            "phone_number",
            "Happy Time inbound",
            action="skipped",
            warnings=["VAPI_PHONE_NUMBER_ID not configured"],
        )
    squad = VapiObject.objects.filter(kind="squad", name=C.SQUAD_NAME).first()
    if not (squad and squad.vapi_id):
        return ReconcileResult(
            "phone_number",
            "Happy Time inbound",
            action="skipped",
            warnings=["squad not provisioned yet"],
        )
    payload = {
        "squadId": squad.vapi_id,
        "assistantId": None,
        "name": "Happy Time inbound",
        "server": _server_block(),
    }
    return _reconcile(
        "phone_number",
        number_id,
        payload,
        find_by_name=lambda _n: vapi.find_phone_number(number_id),
        get_by_id=vapi.get_phone_number,
        create=lambda _b: (_ for _ in ()).throw(  # phone numbers are NEVER created — owner-owned
            vapi.VapiError("phone numbers are owner-provisioned; cannot create")
        ),
        patch=vapi.patch_phone_number,
    )


def _p0_members() -> dict[str, str]:
    """The members provisioned in P0 = the seeded AgentPrompt rows that map to an assistant id.
    P0 ships ONE merged member: ``entry_faq`` (AgentPrompt.role="faq")."""
    from voice.models import VapiObject

    out: dict[str, str] = {}
    rec = VapiObject.objects.filter(kind="assistant", name=C.P0_ASSISTANT_NAME).first()
    if rec and rec.vapi_id:
        out[C.P0_ASSISTANT_ROLE] = rec.vapi_id
    return out


# Roles P1 adds as their OWN Squad members (the retail brain + its router). Each is the
# AgentPrompt.role, provisioned under an assistant named the same as the role so the code-defined
# SQUAD_SHAPE edges (entry_router →(retail)→ budtender) resolve by assistantName (11-P1 §3.5/§5).
P1_MEMBER_ROLES = ("entry_router", "budtender")

# Roles P2 adds as their OWN Squad members. ``escalation`` is the human-handoff member — it fixes
# the export's orphan (it gains REAL inbound edges from entry_router/budtender/faq via SQUAD_SHAPE)
# and is terminal (it exits via the warm built-in transferCall, ADR-016). Provisioned under an
# assistant named for its role so the SQUAD_SHAPE …→escalation edges resolve by assistantName.
P2_MEMBER_ROLES = ("escalation",)

# Roles P3 adds as their OWN Squad members. ``vendor`` is the B2B handler (the export-#6 fix) — it
# carries the notify_vendor_callback custom tool + the warm built-in transferCall, gains the
# entry_router →(vendor)→ vendor inbound edge + the vendor →(dispute)→ escalation edge via
# SQUAD_SHAPE, and never enters retail. Provisioned under an assistant named for its role.
P3_MEMBER_ROLES = ("vendor",)

# Every phase's own-member roles, in a stable order — what the seeded-role + provisioned-member
# scans iterate (each member is provisioned only when its AgentPrompt row is seeded + active).
EXTRA_MEMBER_ROLES = P1_MEMBER_ROLES + P2_MEMBER_ROLES + P3_MEMBER_ROLES


def _provisioned_members() -> dict[str, str]:
    """role → provisioned assistant id, for EVERY member that currently has an assistant in the
    VapiObject map. Includes P0's entry_faq (role=faq) + any P1 members (entry_router/budtender).
    The squad reconcile emits only edges whose BOTH endpoints are provisioned, so the topology
    grows as members land (zero-drift safe)."""
    from voice.models import VapiObject

    out = _p0_members()
    for role in EXTRA_MEMBER_ROLES:
        rec = VapiObject.objects.filter(kind="assistant", name=role).first()
        if rec and rec.vapi_id:
            out[role] = rec.vapi_id
    return out


def provision_all(
    *, dry_run: bool = False, only: str | None = None, members: list[str] | None = None
) -> ProvisionReport:
    """Stand up the P0 Vapi stack from env; a re-run is a proven no-op (ADR-003).

    Order is mandatory (§6.2): tools → files → assistants → squad → phone. ``--dry-run`` (auto when
    VAPI_PRIVATE_KEY is unset) records writes without issuing them."""
    # Auto-engage dry-run when no key (the command also forces it on --dry-run).
    if not vapi.configured():
        dry_run = True
    vapi.set_dry_run(dry_run)

    report = ProvisionReport(dry_run=dry_run)

    auth = vapi.auth_ok()
    if not dry_run and not auth["ok"]:
        report.ok = False
        report.error = auth["error"] or "VAPI_PRIVATE_KEY not configured"
        return report

    results = report.results

    # The extra member roles to provision = those with a seeded AgentPrompt row (so a fresh P0-only
    # tree provisions nothing new). budtender carries the 3 suggestion tools (11-P1 §5); escalation
    # carries only the built-in transferCall (P2).
    extra_roles = _seeded_extra_roles()
    extra_tool_names = _tools_for_roles(extra_roles)

    # (1) TOOLS first — the assistant references the tool ids. P0 provisions faq_lookup; P1 adds
    #     suggest_products/check_inventory/pair_upsell (only when the budtender member is seeded).
    if only in (None, "tool"):
        results.append(ensure_tool("faq_lookup"))
        for tool_name in extra_tool_names:
            results.append(ensure_tool(tool_name))

    # (2) KB FILES + Query Tool (faq grounding fallback).
    if only in (None, "file"):
        results.append(ensure_files())

    # (3) ASSISTANTS — P0's ONE merged entry_faq (role="faq"), toolIds from step 1 (+ Query Tool);
    #     then each seeded P1 member (entry_router/budtender) under an assistant named for its role.
    if only in (None, "assistant"):
        results.append(ensure_assistant(C.P0_ASSISTANT_ROLE, name=C.P0_ASSISTANT_NAME))
        for role in extra_roles:
            results.append(ensure_assistant(role, name=role))

    # (4) SQUAD — every provisioned member; the code-defined edges (entry_router →(retail)→
    #     budtender, budtender →(human)→ escalation) are emitted only when BOTH endpoints exist.
    if only in (None, "squad"):
        results.append(ensure_squad(_provisioned_members()))

    # (5) PHONE NUMBER — attach squadId (graceful skip if O-4 unset).
    if only in (None, "phone"):
        results.append(ensure_phone_number())

    report.ok = report.errors == 0
    return report


def _seeded_extra_roles() -> list[str]:
    """The non-P0 member roles (P1 entry_router/budtender + P2 escalation) that have a seeded,
    active AgentPrompt row — so provisioning is driven by what's seeded, never hardcoded to fire on
    a bare P0 tree. A fresh P0-only tree (no entry_router/budtender/escalation prompt) provisions
    nothing new."""
    from kb.models import AgentPrompt

    seeded = set(
        AgentPrompt.objects.filter(role__in=EXTRA_MEMBER_ROLES, is_active=True).values_list(
            "role", flat=True
        )
    )
    return [r for r in EXTRA_MEMBER_ROLES if r in seeded]


def _tools_for_roles(roles: list[str]) -> list[str]:
    """The distinct custom-tool names the given member roles need (from MEMBER_TOOLS), in a stable
    order, excluding faq_lookup (already provisioned). Used to provision the P1 suggestion tools
    only when their member is seeded."""
    seen: list[str] = []
    for role in roles:
        for name in C.MEMBER_TOOLS.get(role, []):
            if name != "faq_lookup" and name not in seen:
                seen.append(name)
    return seen
