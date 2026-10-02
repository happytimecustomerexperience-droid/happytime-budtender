"""Service-token auth. Only the website's server-side proxy and the voice service may call this API."""
import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


def _matches(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided, expected)  # constant-time


class ServiceTokenPermission(BasePermission):
    """``HHT_BACKEND_TOKEN`` (the voice service + dashboard) opens every view. ``HHT_WEBSITE_TOKEN``
    opens only views marked ``website_ok = True`` — the public site's server routes (menu, search,
    its own chat) — so a leak of the website's env cannot read the customer roster, chat
    transcripts, or anyone's profile by phone."""

    message = "Invalid or missing service token."

    def has_permission(self, request, view) -> bool:
        # Health check is open (no token) so orchestrators can probe it.
        if getattr(view, "is_public", False):
            return True
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return False  # fail closed, also when no token is configured
        provided = header[len("Bearer "):].strip()
        request.website_token = False
        if _matches(provided, settings.HHT_BACKEND_TOKEN):
            return True
        if getattr(view, "website_ok", False) and _matches(
            provided, getattr(settings, "HHT_WEBSITE_TOKEN", "")
        ):
            # A phone in a website request was TYPED by the visitor: it is never an identity
            # (views read ``is_website(request)`` and stay anonymous). Only the voice service's
            # carrier caller-ID, sent with the backend token, may resolve a customer.
            request.website_token = True
            return True
        return False


def is_website(request) -> bool:
    return bool(getattr(request, "website_token", False))
