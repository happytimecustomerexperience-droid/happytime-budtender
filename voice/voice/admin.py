"""Admin registration for the durable call log (the P0 read surface — 10-P0 §3.7).

Read-mostly: the call log is written by the webhook, not by hand. PII discipline holds — the
list shows only the peppered ``caller_phone_hash``, never a raw number (there is no raw column).
"""

from __future__ import annotations

from django.contrib import admin

from voice.models import VapiObject, VoiceCall, VoiceTurn


class VoiceTurnInline(admin.TabularInline):
    model = VoiceTurn
    extra = 0
    fields = ("seq", "role", "tool_name", "latency_ms", "text")
    readonly_fields = fields


@admin.register(VoiceCall)
class VoiceCallAdmin(admin.ModelAdmin):
    list_display = ("call_id", "store", "outcome", "duration_s", "escalated", "created_at")
    list_filter = ("store", "outcome", "escalated")
    search_fields = ("call_id", "caller_phone_hash")
    readonly_fields = ("created_at", "updated_at")
    inlines = [VoiceTurnInline]


@admin.register(VapiObject)
class VapiObjectAdmin(admin.ModelAdmin):
    list_display = ("kind", "name", "vapi_id", "updated_at")
    list_filter = ("kind",)
    search_fields = ("name", "vapi_id")
