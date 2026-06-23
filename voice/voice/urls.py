"""voice app URLs — ``/api/voice/`` prefix (01-ARCHITECTURE.md §2).

ONE Vapi webhook route → ``/api/voice/vapi``. Every custom tool routes through this single
webhook by name via ``TOOL_REGISTRY`` (P1+ add NO routes here — they add tool modules instead).
"""

from django.urls import path

from voice import webhooks

urlpatterns = [
    path("vapi", webhooks.vapi_webhook, name="vapi_webhook"),
]
