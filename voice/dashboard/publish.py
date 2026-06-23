"""Publish-to-Vapi — the control-plane action (14-P4 §5).

Maps each edited ``AgentPrompt`` → ``PATCH /assistant/{id}`` and the code-defined Squad shape →
``PATCH /squad/{id}`` via ``core/services/vapi.py``, reusing ``voice/provision.py``'s payload
builders (one shape, two callers — §9 risk-2 mitigation).

Binding invariants (§5):
  * GET-then-PATCH, never a blind POST in a request handler. A missing id is created via the
    idempotent provisioner upsert (``provision.ensure_assistant``), never a raw POST loop.
  * Squad destinations come from CODE (``provision.build_squad_payload`` reads ``SQUAD_SHAPE``), so
    the canvas cannot delete a required transition — "guardrails cannot be deleted from the UI".
  * Zero-drift idempotency: ``sha256(canonical_json(payload)) == AgentPrompt.last_publish_hash`` →
    ``action="nodrift"``, NO Vapi write issued. A re-publish with no edits issues zero PATCH calls.
  * Fail-loud per object: a Vapi 4xx/5xx on assistant N is captured in that ``PublishResult.error``
    and never aborts the others; the whole action never 500s the dashboard.
  * Tool ids resolved or skipped: a ``tool_name`` with no provisioned ``vapi_tool_id`` → that
    assistant is ``skipped`` with a warning; NO dangling-tool PATCH is sent.
  * Secrets never logged: payloads are hashed/diffed through ``vapi.redact_payload``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from core.services import vapi
from voice import constants as C
from voice import provision

# The 5 Squad members, in dependency order (assistants before squad).
MEMBER_ROLES = ["entry_router", "budtender", "faq", "vendor", "escalation"]


@dataclass
class PublishResult:
    object: str  # "assistant" | "squad"
    role: str
    id: str = ""
    action: str = "nodrift"  # patched | created | skipped | error | nodrift
    drift: bool = False
    changed_fields: list[str] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "object": self.object,
            "role": self.role,
            "id": self.id,
            "action": self.action,
            "drift": self.drift,
            "changed_fields": self.changed_fields,
            "error": self.error,
            "warnings": self.warnings,
        }


def _payload_hash(payload: dict) -> str:
    """Stable sha256 over the redacted canonical JSON — the zero-drift oracle (never a raw secret)."""
    canonical = json.dumps(vapi.redact_payload(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _diff_fields(current: dict | None, payload: dict) -> list[str]:
    """Top-level changed-field names between the live object and the new payload (for the UI)."""
    cur = current or {}
    changed = []
    for key, val in payload.items():
        if cur.get(key) != val:
            changed.append(key)
    return changed


def build_assistant_payload(prompt) -> tuple[dict, list[str]]:
    """The ``PATCH /assistant/{id}`` body for one member — delegates to the shared provision builder
    so the dashboard and the provisioner emit the identical shape (§4.3)."""
    return provision.build_assistant_payload(prompt.role, name=prompt.role)


def build_squad_payload() -> dict:
    """The ``PATCH /squad/{id}`` body from the CODE-defined topology (§4.4). Destinations are
    re-asserted from ``provision.SQUAD_SHAPE`` over the currently-provisioned members — a canvas
    edge that removed a required transition is ignored (the required set is rebuilt from code)."""
    members = provision._provisioned_members()
    return provision.build_squad_payload(members)


def publish_assistant(prompt) -> PublishResult:
    """Publish one member: ensure the id (provisioner upsert), zero-drift short-circuit, then
    GET-then-PATCH. Fail-loud per object; never raises into the view."""
    from kb.models import AgentPrompt

    result = PublishResult(object="assistant", role=prompt.role)
    try:
        payload, warnings = build_assistant_payload(prompt)
        result.warnings = list(warnings)

        # Never PATCH a dangling toolId — a tool not yet provisioned → skip this assistant (G4).
        if any(w.startswith("tool not provisioned") for w in warnings):
            result.action = "skipped"
            return result

        # Ensure the assistant exists (idempotent provisioner upsert; never a blind POST here).
        if not prompt.vapi_assistant_id:
            rec = provision.ensure_assistant(prompt.role, name=prompt.role)
            if rec.action == "error":
                result.action = "error"
                result.error = rec.error
                result.warnings = rec.warnings
                return result
            if rec.action == "skipped":
                result.action = "skipped"
                result.warnings = rec.warnings
                return result
            # ensure_assistant wrote vapi_assistant_id onto the row + did the create/patch already.
            prompt = AgentPrompt.objects.get(pk=prompt.pk)
            result.id = prompt.vapi_assistant_id
            result.action = rec.action
            result.changed_fields = ["model", "voice", "transcriber"]
            result.drift = True
            prompt.last_publish_hash = _payload_hash(payload)
            prompt.last_published_at = timezone.now()
            prompt.save(update_fields=["last_publish_hash", "last_published_at", "updated_at"])
            return result

        result.id = prompt.vapi_assistant_id
        h = _payload_hash(payload)

        # Zero-drift: identical to the last published payload → no Vapi write (G2).
        if h == prompt.last_publish_hash:
            result.action = "nodrift"
            return result

        current = vapi.get_assistant(prompt.vapi_assistant_id)  # GET-then-PATCH
        result.changed_fields = _diff_fields(current, payload)
        vapi.patch_assistant(prompt.vapi_assistant_id, payload)  # PATCH only
        prompt.last_publish_hash = h
        prompt.last_published_at = timezone.now()
        prompt.save(update_fields=["last_publish_hash", "last_published_at", "updated_at"])
        result.action = "patched"
        result.drift = True
        return result
    except vapi.VapiError as exc:  # fail-loud per object — never abort the others (G3)
        result.action = "error"
        result.error = str(exc)
        return result


def publish_squad() -> PublishResult:
    """Publish the Squad shape (members + destinations) from CODE — always safe to re-run."""
    result = PublishResult(object="squad", role="squad")
    try:
        payload = build_squad_payload()
        if not payload.get("members"):
            result.action = "skipped"
            result.warnings = ["no provisioned members yet — run provision first"]
            return result
        squad_id = getattr(settings, "VAPI_SQUAD_ID", "") or ""
        from voice.models import VapiObject

        if not squad_id:
            rec = VapiObject.objects.filter(kind="squad", name=C.SQUAD_NAME).first()
            squad_id = rec.vapi_id if rec else ""
        if not squad_id:
            result.action = "skipped"
            result.warnings = ["VAPI_SQUAD_ID not configured / squad not provisioned"]
            return result
        result.id = squad_id
        current = vapi.get_squad(squad_id)  # GET-then-PATCH
        result.changed_fields = _diff_fields(current, payload)
        vapi.patch_squad(squad_id, payload)
        result.action = "patched"
        result.drift = bool(result.changed_fields)
        return result
    except vapi.VapiError as exc:
        result.action = "error"
        result.error = str(exc)
        return result


def publish_all() -> list[PublishResult]:
    """Publish every active member (assistants first), then the squad. Each result is isolated —
    one object's error never aborts the rest (G3)."""
    from kb.models import AgentPrompt

    results: list[PublishResult] = []
    prompts = {p.role: p for p in AgentPrompt.objects.filter(is_active=True)}
    for role in MEMBER_ROLES:
        prompt = prompts.get(role)
        if prompt is None:
            continue
        results.append(publish_assistant(prompt))
    results.append(publish_squad())
    return results
