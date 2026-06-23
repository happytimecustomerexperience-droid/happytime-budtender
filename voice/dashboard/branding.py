"""Brand tokens loader + CSS-variable renderer (P5; 15-P5 §3.1; ADR-014).

Reads the version-controlled ``brand/tokens.json`` (logo / hex palette / fonts) ONCE (module cache)
and renders the dashboard's ``:root{ --brand-*: … }`` CSS-variable block. Thin, pure, unit-testable.
Presentation only — nothing here touches the Vapi runtime or any guardrail (ADR-014 — the dashboard
is config + docs; safety lives in code, not the theme).

DEFERRED (owner): the real Happy Time hex/fonts/logo come from a manual browser capture past the
Vercel security wall (brand/CAPTURE.md). Until then ``tokens.json`` ships ``provisional:true`` with a
neutral palette identical to the current dashboard, so theming is a no-op visual change (never a
blocker, never a regression) — and the templates show a "brand provisional" badge.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# brand/tokens.json lives at the repo root alongside config/ (BASE_DIR is config's parent).
_TOKENS_PATH = Path(settings.BASE_DIR) / "brand" / "tokens.json"

# The neutral fallback — IDENTICAL to dashboard/base.html's current :root, so a missing/unparseable
# tokens file degrades to exactly today's look (never a blank/broken theme).
_FALLBACK_TOKENS: dict = {
    "provenance": "built-in neutral fallback (no brand/tokens.json found)",
    "provisional": True,
    "logo": {"svg_path": "", "alt": "Happy Time Voice"},
    "colors": {
        "primary": "#1a74bf",
        "primary_fg": "#ffffff",
        "secondary": "#155c9a",
        "accent": "#c0392b",
        "bg": "#f6f7f9",
        "fg": "#1e293b",
        "muted": "#64748b",
        "border": "#e2e8f0",
        "danger": "#dc2626",
        "ok": "#16a34a",
    },
    "fonts": {
        "heading": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
        "body": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    },
}

_HEX_KEYS = (
    "primary",
    "primary_fg",
    "secondary",
    "accent",
    "bg",
    "fg",
    "muted",
    "border",
    "danger",
    "ok",
)

_cache: dict | None = None


def load_brand_tokens() -> dict:
    """Read + cache ``brand/tokens.json`` (read once at module level). On a missing file or any parse
    error, returns the neutral fallback (never raises — a bad theme must not break the dashboard).
    The returned dict always has ``colors`` (all ``_HEX_KEYS``), ``fonts``, ``logo``, and
    ``provisional``; missing color keys are backfilled from the fallback."""
    global _cache
    if _cache is not None:
        return _cache
    _cache = _read_tokens()
    return _cache


def _read_tokens() -> dict:
    try:
        raw = json.loads(_TOKENS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(_FALLBACK_TOKENS)
    except (ValueError, OSError):
        logger.warning("brand/tokens.json unreadable; using neutral fallback", exc_info=True)
        return dict(_FALLBACK_TOKENS)
    return _merge_with_fallback(raw)


def _merge_with_fallback(raw: dict) -> dict:
    """Backfill any missing color/font/logo keys from the fallback so the renderer never KeyErrors on
    a partially-filled (e.g. mid-capture) tokens file."""
    out: dict = dict(_FALLBACK_TOKENS)
    if not isinstance(raw, dict):
        return out
    out["provisional"] = bool(raw.get("provisional", True))
    out["provenance"] = raw.get("provenance", out["provenance"])
    colors = dict(_FALLBACK_TOKENS["colors"])
    for k, v in (raw.get("colors") or {}).items():
        if k in _HEX_KEYS and isinstance(v, str) and v.strip():
            colors[k] = v.strip()
    out["colors"] = colors
    fonts = dict(_FALLBACK_TOKENS["fonts"])
    for k in ("heading", "body"):
        v = (raw.get("fonts") or {}).get(k)
        if isinstance(v, str) and v.strip():
            fonts[k] = v.strip()
    out["fonts"] = fonts
    logo = dict(_FALLBACK_TOKENS["logo"])
    for k in ("svg_path", "alt"):
        v = (raw.get("logo") or {}).get(k)
        if isinstance(v, str):
            logo[k] = v
    out["logo"] = logo
    return out


def brand_css_vars(tokens: dict | None = None) -> str:
    """Render the ``:root{ --brand-*: …; }`` CSS-variable block from the tokens. Pure string build —
    safe to inject into ``<head>`` (values are hex/font-stack strings from a version-controlled file,
    never user input). Emits ``--brand-<color>`` for every color + ``--brand-font-heading/body``."""
    t = tokens if tokens is not None else load_brand_tokens()
    colors = t.get("colors", {})
    fonts = t.get("fonts", {})
    lines = [
        f"  --brand-{key.replace('_', '-')}: {colors[key]};" for key in _HEX_KEYS if key in colors
    ]
    lines.append(
        f"  --brand-font-heading: {fonts.get('heading', _FALLBACK_TOKENS['fonts']['heading'])};"
    )
    lines.append(f"  --brand-font-body: {fonts.get('body', _FALLBACK_TOKENS['fonts']['body'])};")
    return ":root{\n" + "\n".join(lines) + "\n}"


def brand_context() -> dict:
    """The dashboard template context: ``{brand: tokens, brand_css_vars: <css>}``. Injected by every
    dashboard view (or a context processor). Read-only — presentation only (ADR-014)."""
    tokens = load_brand_tokens()
    return {"brand": tokens, "brand_css_vars": brand_css_vars(tokens)}


def context_processor(request) -> dict:
    """Django template context processor — makes ``brand`` + ``brand_css_vars`` available to every
    template (so ``base.html`` can emit the CSS-variable block + the logo without each view passing
    them). Read-only; never raises (a theme error must not 500 the dashboard)."""
    try:
        return brand_context()
    except Exception:  # noqa: BLE001 — a theme read must never break a page render
        logger.warning("brand context_processor failed; rendering unthemed", exc_info=True)
        return {"brand": dict(_FALLBACK_TOKENS), "brand_css_vars": ""}


def reset_cache() -> None:
    """Test seam: drop the module cache so a fixture can re-read a patched tokens file."""
    global _cache
    _cache = None
