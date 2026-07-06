"""Tiny inline-SVG chart helpers — no JS, no dependency, CSP-safe.

Each returns an SVG string to drop into a template with ``|safe``. Colours are literal
hex from the app.css palette (SVG ``stroke`` can't read CSS vars reliably across browsers),
kept in sync with :root in app.css. Bars/rankings use CSS in the templates (the existing
affinity-bar pattern); these cover the two things CSS can't: trend lines and donuts.
"""

from __future__ import annotations

import html
import math

# app.css palette (amber, teal, sage, ember, coral, violet, blue, cream)
PALETTE = ["#FFB74D", "#5EE0D0", "#9AB0C6", "#FF8A00", "#FF7A5C", "#B79CED", "#7FB2FF", "#E8EEF5"]


def sparkline(values, width=140, height=36, stroke="#FFB74D") -> str:
    """A single trend line with a dot on the last point. '' when there's nothing to plot."""
    pts = [float(v or 0) for v in (values or [])]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    n = len(pts) - 1
    coords = [(round(i / n * (width - 4) + 2, 1),
               round(height - 2 - (v - lo) / span * (height - 6), 1)) for i, v in enumerate(pts)]
    path = " ".join(f"{x},{y}" for x, y in coords)
    lx, ly = coords[-1]
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
            f'role="img" aria-hidden="true">'
            f'<polyline points="{path}" fill="none" stroke="{stroke}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{lx}" cy="{ly}" r="2.6" fill="{stroke}"/></svg>')


def donut(segments, size=132, thickness=16) -> str:
    """``segments`` = [(label, value)] → SVG donut coloured from PALETTE (legend is separate)."""
    data = [(str(lbl), float(v or 0)) for lbl, v in (segments or []) if (v or 0) > 0]
    total = sum(v for _, v in data)
    if total <= 0:
        return ""
    r = (size - thickness) / 2
    c = size / 2
    circ = 2 * math.pi * r
    arcs, off = [], 0.0
    for i, (label, v) in enumerate(data):
        frac = v / total
        dash = frac * circ
        arcs.append(
            f'<circle cx="{c}" cy="{c}" r="{r:.1f}" fill="none" stroke="{PALETTE[i % len(PALETTE)]}" '
            f'stroke-width="{thickness}" stroke-dasharray="{dash:.2f} {circ - dash:.2f}" '
            f'stroke-dashoffset="{-off:.2f}" transform="rotate(-90 {c} {c})">'
            f'<title>{html.escape(label)}: {round(frac * 100)}%</title></circle>')
        off += dash
    return (f'<svg class="donut" viewBox="0 0 {size} {size}" role="img">' + "".join(arcs)
            + f'<text x="{c}" y="{c}" text-anchor="middle" dominant-baseline="central" '
            f'fill="var(--cream)" font-size="20" font-weight="700">{round(total)}</text></svg>')


def legend(segments) -> list[dict]:
    """[(label, value)] → [{label, value, pct, color}] to render beside a donut."""
    data = [(str(lbl), float(v or 0)) for lbl, v in (segments or []) if (v or 0) > 0]
    total = sum(v for _, v in data) or 1
    return [{"label": lbl, "value": round(v), "pct": round(v / total * 100),
             "color": PALETTE[i % len(PALETTE)]} for i, (lbl, v) in enumerate(data)]
