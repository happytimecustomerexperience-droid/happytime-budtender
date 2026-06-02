# Happy Time Budtender — Personalization, Merchandising & Analytics (Master Design)

> Date: 2026-05-29 · Status: approved (brainstorm) · Owner: Vladi
> Scope: the AI budtender's "frighteningly good" personalization + margin-aware
> merchandising + full analytics, served from the self-contained budtender VPS
> (`happytime-budtender/`) and the website (`happytimeweed/`).

## Goal
When a customer enters their phone, the budtender should feel like it *knows
them* — surfacing brands, strains, types and a quality level they'll actually
want — while steering them, within their taste and budget, toward the items that
make Happy Time the most money. Everything is observed and fed to a locked-down
admin dashboard.

## Locked decisions (from brainstorm)
1. **No OTP.** Use purchase history to the fullest to drive suggestions. Phrase
   naturally ("you lean toward premium indica flower") — do **not** recite exact
   receipts/dates. Margin/cost never leave the server to the public chat API.
2. **Product buckets** = Core / Traffic-driver / Profit-driver, from margin %,
   gross profit $, price and velocity (velocity-aware retail model).
3. **Bell curve per (main category × subcategory)** — e.g. flower×28g — so a
   product is judged against its true peers; sparse groups fall back to
   category-only.
4. **Balanced profit nudge** — never break stated budget/taste; within that,
   weight profit-drivers up and use the pairing slot for one higher-margin nudge.
5. **Repeat-vs-explore = autodetect + ask in chat + offer in the form** → a
   well-informed educated mix.
6. **No-reload menu widget** — standalone vanilla-JS widget sharing the same
   localStorage session + `/api/chat/*`.
7. **Analytics + admin dashboard inside the budtender VPS, behind Cloudflare
   Access** (SSO, no public ports, own auth + audit log).

## Subsystems & build order
Each subsystem gets its own spec → plan → implementation cycle.

1. **Profit-strategy classification** (foundational) — `margin_pct`,
   `gross_profit`, `velocity`, `subcategory`, `bucket`, `bucket_source`, z-scores;
   a Celery `classify_products` job; admin override. Spec:
   `2026-05-29-subsystem-1-classification-design.md`.
2. **Customer profile engine** — full transaction sync (incl. price → quality
   tier), affinity maps, `novelty_score`, `quality_tier`, `bucket_mix`, and the
   strain/brand/effect **adjacency cheatsheet** (catalog.json + education KB +
   co-purchase matrix).
3. **Personalized + profit-aware ranking** — fuse taste-fit + bucket strategy +
   margin + effect + budget-spread + size, hard-gated by budget/taste; habitual
   vs explorer handling; deliberate familiar/adjacent-new/profit mix; "my usual"
   / "surprise me" chips + form toggle; natural `why_this` reasoning.
4. **No-reload menu chat widget** — `budtender-widget.js` on the static menu
   pages; in-place open, shared `chatbot-state-v2` + same-origin APIs.
5. **Analytics + secure admin dashboard** — `/api/v1/track` → `AnalyticsEvent`;
   Cloudflare-Access dashboard: usage/funnels, product performance by bucket,
   suggestion→conversion, manage manual pairings, classify/override buckets, tune
   thresholds.

## Cross-cutting security
- Cloudflare Tunnel (no inbound ports) + Bearer token for the website→VPS link;
  Cloudflare Access (SSO) for the admin dashboard.
- Public chat/search/pairing responses NEVER include cost/margin/bucket internals
  (allowlist serializers; regression test asserts no `margin`/`cost` substring).
- All admin writes audited; parameterized read-only queries; non-root containers;
  secrets gitignored; phone treated as PII (never echoed to the LLM or logs).

## Data flow
```
Browser ──same-origin──► Next /api/* ──Bearer/TLS──► budtender VPS
  (chat + menu widget)        proxy                   ├─ ranking (taste+profit)
                                                       ├─ classification job
                                                       ├─ profile engine
                                                       ├─ analytics store
                                                       └─ Dutchie POS sync
Admin ──Cloudflare Access SSO──► dashboard app (margin/analytics, manage pairings)
```

## Verification (per subsystem)
Defined in each subsystem spec; master-level: no margin/cost leak to public API;
classification + profile recompute run on live data; dashboard reachable only via
Cloudflare Access; widget opens with no reload and resumes the session.
