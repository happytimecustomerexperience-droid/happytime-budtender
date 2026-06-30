"""The durable call log — the canonical record every later phase reads + enriches.

Frozen shapes (10-P0-CHASSIS-FAQ.md §4.6 — do NOT move these after P1/P2/P3 fork against
them). ``VoiceCall`` is one row per call, idempotent on the Vapi ``call_id``; ``VoiceTurn`` is
one row per turn, idempotent on ``(call, seq)`` so a Vapi re-delivery never double-writes.
``Outcome`` covers the P0 outcomes (faq_answered / abandoned / error) plus the slots P1–P3
fill (suggested / escalation / vendor_callback).

PII discipline (23-SPEC §3.5): ``VoiceCall`` stores only the peppered ``caller_phone_hash`` —
NO raw number column. The eocr handler hashes ``customer.number`` via ``crm.models.phone_hash``
and never persists the raw value.

Mirrors swedish-bot's Session/ServiceRequest durability + idempotency idioms.
"""

from __future__ import annotations

from django.db import models


class Outcome(models.TextChoices):
    FAQ_ANSWERED = "faq_answered", "FAQ answered"
    SUGGESTED = "suggested", "Suggested"  # set by P1
    ESCALATION = "escalation", "Escalation"  # set by P2
    VENDOR_CALLBACK = "vendor_callback", "Vendor callback"  # set by P3
    ABANDONED = "abandoned", "Abandoned"
    ERROR = "error", "Error"


class VoiceCall(models.Model):
    """One row per inbound call — the durable record (ADR-017). Keyed on the Vapi
    ``call_id`` (idempotency key) so an eocr re-delivery upserts, never duplicates."""

    call_id = models.CharField(max_length=64, unique=True, db_index=True)  # Vapi call.id
    store = models.CharField(max_length=32, blank=True)  # yakima|mount-vernon|pullman
    # Peppered; the raw number is NEVER stored (PII discipline, 23-SPEC §3.5).
    caller_phone_hash = models.CharField(max_length=64, blank=True, db_index=True)
    outcome = models.CharField(max_length=32, choices=Outcome.choices, blank=True)
    escalated = models.BooleanField(default=False)  # P2 sets — a transfer was attempted
    reason = models.CharField(max_length=64, blank=True)  # escalation_reason: defective_return|…
    human_requested_count = models.IntegerField(default=0)  # P2 — feeds the repeated_request gate
    # Transfer disposition (P2): connected | no_answer | not_attempted; the targeted store key.
    transfer_disposition = models.CharField(max_length=24, blank=True)
    transfer_number_key = models.CharField(max_length=16, blank=True)  # YAKIMA|MTVERNON|PULLMAN
    duration_s = models.IntegerField(null=True, blank=True)
    transcript = models.TextField(blank=True)
    ai_summary = models.TextField(blank=True)
    assistant_id = models.CharField(max_length=64, blank=True)
    suggested_skus = models.JSONField(default=list, blank=True)  # P1 appends
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"VoiceCall<{self.call_id}> {self.outcome or '—'}"


class VoiceTurn(models.Model):
    """One row per conversational turn. ``latency_ms`` is the server-side handler time the
    webhook stamps, so P5's p95 is computable straight from durable rows. Idempotent on
    ``(call, seq)`` — a re-delivered turn updates in place."""

    call = models.ForeignKey(VoiceCall, related_name="turns", on_delete=models.CASCADE)
    seq = models.IntegerField()
    role = models.CharField(max_length=16)  # user|assistant|tool
    text = models.TextField(blank=True)
    tool_name = models.CharField(max_length=64, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)  # server-side handler time
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("call", "seq")]  # idempotent re-delivery
        ordering = ["call", "seq"]

    def __str__(self) -> str:
        return f"VoiceTurn<{self.call_id}/{self.seq} {self.role}>"


class VoiceToolCall(models.Model):
    """One row per tool invocation in a call — the args the assistant SENT and the result it got
    back (P6 "tool calls must be logged"). Pre-P6 only the tool NAME survived (on ``VoiceTurn``);
    args+results were dropped on the floor in ``handle_tool_calls``. Keyed by the Vapi ``call_id``
    string (not a FK) because tool-calls arrive BEFORE the end-of-call-report creates the
    ``VoiceCall`` row — the dashboard joins on ``call_id``.

    Leak/PII discipline: ``result`` is already leak-scrubbed by ``tools.dispatch`` (no cost/margin);
    ``args`` is run through ``guardrails.scrub_leak`` + ``redact_pii`` before storing (a phone/number
    a caller spoke into an arg is masked — 23-SPEC §3.5)."""

    call_id = models.CharField(max_length=64, db_index=True)  # Vapi call.id (links to VoiceCall)
    tool_call_id = models.CharField(max_length=80, blank=True)  # Vapi toolCall id (idempotency)
    name = models.CharField(max_length=64)
    args = models.JSONField(default=dict, blank=True)  # redacted (PII-masked, leak-scrubbed)
    result = models.JSONField(default=dict, blank=True)  # leak-scrubbed by dispatch
    store = models.CharField(max_length=32, blank=True)
    source = models.CharField(max_length=16, default="webhook")  # webhook | vapi_fetch
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Idempotent re-delivery: a repeated tool-call webhook / re-fetch updates in place.
        # ponytail: when tool_call_id is "" (Vapi omitted it) the uniqueness degrades to
        # (call_id, name, "") — acceptable; synthesize an id upstream if collisions ever matter.
        unique_together = [("call_id", "tool_call_id", "name")]
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"VoiceToolCall<{self.call_id}/{self.name}>"


class VapiObject(models.Model):
    """The local id-map written back by the provisioner (10-P0 §3.6; 20-SPEC §4.6). One row per
    provisioned Vapi object (assistant/squad/tool/phone-number/file) keyed by ``(kind, name)`` so a
    re-run is GET-then-PATCH, never a blind POST. Assistant ids also land on
    ``kb.AgentPrompt.vapi_assistant_id``; this table is the full map the provisioner reconciles.

    ``last_provision_hash`` is the ZERO-DRIFT oracle (20-SPEC §4.6): when
    ``sha256(canonical_json(payload)) == last_provision_hash`` the reconcile is ``nodrift`` and NO
    Vapi write is issued — so a re-run with no local edits creates zero objects + issues zero
    PATCHes (the headline acceptance criterion A-IDEMP)."""

    KIND_CHOICES = [
        ("assistant", "Assistant"),
        ("squad", "Squad"),
        ("tool", "Tool"),
        ("phone_number", "Phone number"),
        ("file", "File"),
    ]
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    name = models.CharField(max_length=128)  # logical name (e.g. "entry_faq", "faq_lookup")
    vapi_id = models.CharField(max_length=64, blank=True)
    last_provision_hash = models.CharField(max_length=64, blank=True)  # zero-drift oracle
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("kind", "name")]
        ordering = ["kind", "name"]

    def __str__(self) -> str:
        return f"VapiObject<{self.kind}/{self.name}={self.vapi_id or '—'}>"
