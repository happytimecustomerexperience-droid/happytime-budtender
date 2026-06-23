"""Minimal CORS for the dashboard/API surface (01-ARCHITECTURE.md §5). No
third-party dependency. Ported from swedish-bot/core/middleware.py.

NOTE: the Vapi webhook HMAC verification is NOT here — it lives inside
voice/webhooks.py so a bad signature returns a Vapi-shaped 401 (a middleware 401
confuses Vapi's retry). See 10-P0-CHASSIS-FAQ.md §3.2.
"""

import logging

from django.conf import settings
from django.http import HttpResponse

logger = logging.getLogger(__name__)


class WidgetCorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin", "")
        allowed = origin in settings.WIDGET_ALLOWED_ORIGINS

        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            resp = HttpResponse(status=204)
        else:
            resp = self.get_response(request)

        if request.path.startswith("/api/") and allowed:
            resp["Access-Control-Allow-Origin"] = origin
            resp["Vary"] = "Origin"
            resp["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            resp["Access-Control-Allow-Headers"] = "Content-Type"
            resp["Access-Control-Max-Age"] = "86400"
        return resp
