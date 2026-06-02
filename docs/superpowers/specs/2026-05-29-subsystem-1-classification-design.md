# Subsystem 1 — Profit-Strategy Product Classification

> Date: 2026-05-29 · Status: approved · Parent: master-design.md
> Service: `happytime-budtender/` (Django). Foundational for margin-aware ranking.

## Purpose
Classify every in-stock product into **Core / Traffic-driver / Profit-driver**
using margin %, gross profit $, price and sales velocity, judged on a **bell
curve within its (category × subcategory) peer group**. This lets the ranking
steer customers — within taste + budget — toward items that make us the most
money, and lets the admin understand and override merchandising strategy.

## Data model (Product additions)
- `margin_pct` FloatField — `gross_profit / price` (0..1). Server-only.
- `gross_profit` FloatField — `price - cost` (== existing `margin`; expose as
  named field for clarity). Server-only.
- `velocity` FloatField — units sold per day over a trailing window (default 30d),
  computed from transactions. 0 when unknown.
- `subcategory` CharField — normalized size label: `28g/14g/7g/3.5g/1g/0.5g`
  (flower/cart/concentrate from `unit_weight`) or `5mg/10mg/20mg+` (edible/tincture
  from `potency_mg`); `""` when unknown.
- `bucket` CharField choices `core|traffic|profit`, default `core`.
- `bucket_source` CharField `auto|manual`, default `auto`.
- `margin_z` FloatField — z-score of `margin_pct` within peer group.
- `price_z` FloatField — z-score of `price` within peer group.
- `classified_at` DateTimeField null.

All margin/cost/bucket fields are server-only — excluded from `public_product`.

## Subcategory derivation
`size_label(p)`: from `unit_weight` → nearest of {0.5,1,2,3.5,7,14,28}g (tolerance
0.3g) else `""`; for edibles/tinctures from `potency_mg` → `5mg`/`10mg`/`20mg+`.
Reuses the size synonyms already in `ranking.py`.

## Classification algorithm (`tasks.classify_products`)
Per location:
1. Compute `velocity` for each product from `TransactionItem`/purchase history in
   the trailing 30d (units ÷ 30). (If transactions absent, velocity=0 — falls back
   to margin/price only.)
2. Group in-stock products by `(category, subcategory)`. If a group has < 8
   items, merge into a `(category, *)` group (category-only fallback).
3. For each group compute mean/std of `margin_pct` and `price`; set each product's
   `margin_z`, `price_z` (std=0 → z=0).
4. Bucket (auto, unless `bucket_source=manual`):
   - **profit** if `margin_z >= +0.5` OR `gross_profit >=` group 75th-pct GP.
   - **traffic** if `margin_z <= -0.5` AND `price_z <= -0.25` AND
     `velocity >=` group 60th-pct velocity.
   - **core** otherwise.
5. Save `bucket`, z-scores, `classified_at`. Never overwrite `bucket` when
   `bucket_source=manual`.
Thresholds live in a module constant block now; admin-tunable in subsystem 5.

## Scheduling
- Celery beat: `classify_products_all` nightly + after each `sync_inventory`.
- `sync_inventory` already runs ~10 min; classification reads only synced rows.

## Admin override (interim, full UI in subsystem 5)
- Django admin registration for Product with `bucket`, `bucket_source` editable;
  setting `bucket` via admin sets `bucket_source=manual`.
- Manual buckets are preserved across re-classification.

## How ranking consumes it (handoff to subsystem 3)
`bucket` + `margin_z` feed a `W_BUCKET` term: profit-drivers get a positive nudge,
traffic-drivers a small negative (unless the customer is price-sensitive), gated so
budget/taste always win. (Implemented in subsystem 3.)

## Migration / ops
- DB columns added via migration (or `ALTER TABLE` for the existing dev DB, which
  was built with `--run-syncdb`).
- Re-sync inventory, then run `classify_products_all`.

## Verification
- After classify on live Yakima data: every in-stock product has a `bucket`;
  distribution is sane (not all one bucket); profit-drivers have higher mean
  `margin_pct` than traffic-drivers within the same (category×subcategory).
- `public_product` output contains no `margin`, `cost`, `bucket`, or `*_z` keys
  (regression assertion).
- Manual override persists across a re-run of `classify_products`.
