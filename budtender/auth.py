"""Service-token auth. Only the website's server-side proxy may call this API."""
import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


class ServiceTokenPermission(BasePermission):
    message = "Invalid or missing service token."

    def has_permission(self, request, view) -> bool:
        # Health check is open (no token) so orchestrators can probe it.
        if getattr(view, "is_public", False):
            return True
        expected = settings.HHT_BACKEND_TOKEN
        if not expected:
            return False  # fail closed if not configured
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return False
        provided = header[len("Bearer "):].strip()
        # constant-time compare
        return hmac.compare_digest(provided, expected)
