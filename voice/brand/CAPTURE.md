# Brand capture runbook — DEFERRED (owner action required)

> **Status: DEFERRED (owner).** The real Happy Time brand assets (logo, hex palette, fonts) are
> **owner-gated**: the live site (`happytimeweed.com`) sits behind a **Vercel security checkpoint**
> that blocks automated fetch (`WebFetch` / server-side `fetch()` / scraping — see
> `_research-education-blogs.md` §Provenance). So this capture is a **manual browser step a human
> must do**; the code side is already wired and ships a neutral placeholder palette until the assets
> land.
>
> Until this runbook is completed, `brand/tokens.json` stays `"provisional": true` and the dashboard
> renders the neutral fallback + a **"brand provisional"** badge. **P5 is NOT blocked on this** —
> theming lands as a config drop (drop real values into `tokens.json`, set `provisional: false`).

---

## What the code already does (no further code needed)

- `dashboard/branding.py` reads `brand/tokens.json` once and renders `:root{ --brand-*: … }` CSS
  custom properties; a context processor injects `brand` + `brand_css_vars` into every dashboard
  template; `templates/dashboard/base.html` maps its design tokens onto the `--brand-*` vars and
  shows the logo + the provisional badge.
- Drop the captured values into `brand/tokens.json`, set `"provisional": false`, drop the logo file
  into `static/brand/`, set `colors.*`/`fonts.*`/`logo.svg_path` — the whole dashboard re-themes on
  the next page load. **Nothing else changes** (presentation only — ADR-014; the Vapi runtime is
  untouched except the persona/tone copy, which is a separate KB edit).

---

## The manual capture (a human, in a real logged-in browser)

Use a normal browser, or the **claude-in-chrome** / **computer-use** MCP if the operator has it
(those drive a real browser past the Vercel wall the way a human would).

Open, in order, and capture from each:

| Page | URL | Grab |
|---|---|---|
| Home | `https://happytimeweed.com` | header/nav color, button color, link color, heading + body fonts, the logo asset |
| Brands | `https://happytimeweed.com/brands` | secondary/accent colors, any brand-tile styling |
| A store menu | `https://happytimeweed.com/yakima-menu` (or the store's Dutchie menu) | the live retail palette (CTA color, price color) |

### 1. Logo
- Right-click the logo → **Save image as…** (or, if it's an inline SVG, copy the `<svg>` from
  DevTools → Elements). Save it to `static/brand/happytime-logo.svg` (or `.png`).
- If you can only screenshot it, screenshot the logo and trace it (or just use the PNG).
- Set `tokens.json` → `logo.svg_path = "brand/happytime-logo.svg"` and `logo.alt = "Happy Time Weed"`.

### 2. Hex palette
- DevTools → **Inspect** the header/primary button → **Computed** styles → read `background-color`
  / `color` (DevTools shows the hex). Or use an eyedropper on a screenshot.
- Fill `tokens.json` → `colors`:
  - `primary` — the main brand color (header / primary button).
  - `primary_fg` — text color on the primary (usually `#ffffff`).
  - `secondary` — a darker/hover shade of primary.
  - `accent` — the call-to-action / highlight color.
  - `bg` — page background. `fg` — body text. `muted` — secondary text. `border` — hairlines.
  - leave `danger` / `ok` as-is (they're semantic, not brand).

### 3. Fonts
- DevTools → **Computed** → `font-family` on an `<h1>` (→ `fonts.heading`) and on `<body>` (→
  `fonts.body`). Keep the full stack and append `system-ui, sans-serif` as a fallback.
- If the fonts are Google-hosted, also add the `<link>` to `base.html` `<head>` (or drop the webfont
  files into `static/brand/`).

### 4. Finalize
- Set `tokens.json` → `"provisional": false`, fill `"provenance"` with "manual browser capture
  YYYY-MM-DD (Vercel wall blocks auto-fetch)", and set `"capture_date"`.
- Reload the dashboard — the **"brand provisional"** badge disappears and the captured palette/logo
  render. Screenshot before/after for the acceptance record (AC-3).

---

## Assistant tone / persona (the voice side of branding)

The brand's **voice** is the "Koptza" persona — finalized as KB copy in `kb/seed.py`
(`KOPTZA_TONE`), NOT a visual asset, so it is **not blocked** by the Vercel wall. It's seeded on the
`entry_router` / `budtender` / `faq` / `vendor` / `escalation` `AgentPrompt` rows and reaches Vapi
via **Publish-to-Vapi** (`PATCH /assistant/{id}`, the P4 path — no new mechanism). The tone:
**warm, family/community, no-pressure, conservative on dosing**; spoken 21+ confirm (never "let me
peek at your ID"); out-the-door prices only; numbers/facts only from the KB (Numbers-Guard). Edit
`KOPTZA_TONE` and re-run `manage.py seed_kb`, then Publish.
