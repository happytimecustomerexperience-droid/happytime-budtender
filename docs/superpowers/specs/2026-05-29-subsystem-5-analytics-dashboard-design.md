# Subsystem 5 — Analytics + Secure Admin Dashboard

> Date: 2026-05-29 · Status: approved · Parent: master-design.md
> Service: `happytime-budtender/` (events) + a gated dashboard app.

## Purpose
Capture every interaction across chat + menu, store it securely server-side, and
surface it (plus merchandising controls) in an admin dashboard that is
**impenetrable** to the public — Cloudflare Access only.

## Event capture
- Endpoint `POST /api/v1/track` (Bearer, via the website proxy `/api/track`).
- Model `AnalyticsEvent`: `session_token`, `phone` (nullable, hashed at rest),
  `location_slug`, `event_type`, `props` JSON, `ts`, `channel` (chat|menu|questionnaire).
- Event types: session_start, route_pick, slot_set, search, recommend_view,
  card_click, shop_now, pairing_view, pairing_accept, checkout, menu_view,
  menu_dwell, plus Dutchie funnel (product_view, add_to_cart, checkout) from
  `menu-analytics.js`.
- Conversions: link `shop_now`/`checkout` back to the `SuggestedProduct` that was
  shown → suggestion→purchase attribution (the `accepted` flag already exists).

## Dashboard app
A separate Django app (or sub-site) served only through **Cloudflare Access**
(SSO; no public ports — same tunnel pattern, separate hostname + Access policy).
Pages:
- **Usage & funnels** — sessions, route mix, drop-off at each stage, dwell.
- **Product performance** — by bucket (core/traffic/profit): impressions, clicks,
  shop-now, attributed conversions, revenue/margin contribution.
- **Suggestion quality** — accept rate per reason_code, per bucket, per category.
- **Customer cohorts** — habitual vs explorer, quality-tier mix (aggregate, not
  per-person PII beyond what staff already see in Dutchie).
- **Merchandising controls** — manage manual pairings (add/edit/remove),
  classify/override product buckets, tune classification thresholds + ranking
  weights (writes to a `Settings` row the jobs read).

## Security (hard requirements)
- Cloudflare Access in front of the entire dashboard hostname; deny-by-default.
- Dashboard has its own Django auth on top (defense in depth) + per-action audit
  log (`AdminAudit`: who, what, before/after, ts).
- All queries parameterized, read-only against synced tables; writes only to
  budtender-owned tables (pairings, buckets, settings).
- Margin/cost/bucket internals are visible ONLY here, never via the public chat
  API. Phone stored hashed; raw phone never rendered in analytics lists.
- Non-root container; secrets gitignored; rate-limit the track endpoint.

## Verification
- Events land for every interaction type; funnel counts reconcile with a scripted
  session. Conversion attribution links a shop_now to its SuggestedProduct.
- Dashboard unreachable without Cloudflare Access; admin writes appear in the
  audit log; threshold/weight changes take effect on next classify/rank run.
- No-leak regression still passes (public API has no margin/cost/bucket).
