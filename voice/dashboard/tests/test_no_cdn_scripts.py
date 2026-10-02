"""Dashboard pages run only scripts we serve ourselves. A CDN script on these pages could read the
Credentials page and every transcript, so htmx and Alpine are vendored under voice/static/vendor."""

import re
from pathlib import Path

from django.conf import settings

TEMPLATES = Path(settings.BASE_DIR) / "templates" / "dashboard"
# The staff console's Vapi web SDK is the one remote module left; it is version-pinned.
_PINNED_REMOTE = {"https://esm.sh/@vapi-ai/web@2.7.1"}


def test_no_dashboard_template_loads_a_script_from_another_host():
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for url in re.findall(r"<script[^>]+src=\"(https?://[^\"]+)\"", text):
            offenders.append(f"{path.name}: {url}")
        for url in re.findall(r"from \"(https?://[^\"]+)\"", text):
            if url not in _PINNED_REMOTE:
                offenders.append(f"{path.name}: {url}")
    assert not offenders, offenders


def test_the_vendored_files_exist():
    vendor = Path(settings.BASE_DIR) / "static" / "vendor"
    for name in ("htmx-1.9.12.min.js", "alpinejs-3.13.3.cdn.min.js"):
        assert (vendor / name).stat().st_size > 10_000, name
