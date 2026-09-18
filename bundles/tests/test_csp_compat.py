"""The storefront is served behind an edge CSP of ``script-src 'self'; style-src 'self'``
(docker-compose.yml, the ``budtender-security-headers`` Traefik middleware). Any inline
``<script>`` or ``style=`` attribute in these templates is silently blocked in production — the
first casualty was the 21+ gate, which would have sat closed for every shopper. Keep them out."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "bundles"
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)
_INLINE_STYLE = re.compile(r"<style\b|\sstyle=\"|\son\w+=\"|javascript:", re.I)


def test_no_inline_scripts_or_styles_in_storefront_templates():
    offenders = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if _INLINE_SCRIPT.search(text) or _INLINE_STYLE.search(text):
            offenders.append(path.name)
    assert not offenders, f"inline script/style would be blocked by the edge CSP: {offenders}"
