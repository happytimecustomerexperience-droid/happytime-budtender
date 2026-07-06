"""Content-Security-Policy header (audit hardening).

Everything is self-hosted (htmx + app.js + CSS in static, served by WhiteNoise),
so we can keep a tight policy. Google Fonts is the only external origin. Inline
styles are still used in a few templates, so style-src allows 'unsafe-inline';
scripts are 'self' only (no inline <script>), which blocks injected JS execution.
"""

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resp = self.get_response(request)
        resp.setdefault("Content-Security-Policy", CSP)
        return resp
