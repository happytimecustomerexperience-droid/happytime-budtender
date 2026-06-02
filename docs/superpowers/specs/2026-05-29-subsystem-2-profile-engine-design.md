# Subsystem 2 — Customer Profile Engine

> Date: 2026-05-29 · Status: approved · Parent: master-design.md
> Service: `happytime-budtender/`. Depends on subsystem 1 (buckets/subcategory).

## Purpose
Turn a customer's full Dutchie purchase history into a rich, queryable taste
profile so the ranking (subsystem 3) can make picks that feel hand-chosen:
right brands, strains, type, quality tier, and the right balance of "their usual"
vs "something new" — without ever reciting a receipt.

## Full transaction sync
Extend `sync_transactions` to capture **all** history (paginate
`register-transactions`, no 365d cap; incremental by `lastModifiedDateUTC`):
per line item store `{sku/productId, brand, category, subcategory, strain,
strain_type, terpene, price_paid, qty, bought_at}`. Join to customer via
`customerId → phone` (normalize to E.164). Fold into `CustomerProfile.purchase_history`.

SKU alignment: transaction `productId` must map to inventory `productId` (the key
we now aggregate Product on in subsystem 1). Where they differ, fall back to
fuzzy name match. This makes `velocity` (subsystem 1) and affinity real.

## CustomerProfile additions
- `quality_tier` (value/mid/top) — from the mean `price_z` of their purchases
  within each item's (category×subcategory) peer group (reuses subsystem-1 z-scores).
- `novelty_score` 0..1 — normalized entropy / distinct-ratio of their brand+strain
  distribution. High = explorer, low = creature-of-habit.
- `bucket_mix` — share of their buys that are core/traffic/profit.
- `avg_price_by_category`, `thc_min`/`thc_max` (already present), refined.
- Affinity maps (already present, now richer + price-weighted): brand, strain,
  strain_type, terpene, category, **subcategory**, flavor. Recency- and
  frequency-weighted (recent + repeated buys score higher).

## Adjacency cheatsheet (substitution reasoning)
A generated artifact (JSON in repo + cached in Redis) the ranking consults to say
"you liked X → try Y":
- **strain → similar strains**: shared lineage/terpene from `catalog.json` +
  education KB, reinforced by the co-purchase matrix.
- **brand → same-tier peers**: brands grouped by their products' median quality
  tier + category, so we substitute premium-for-premium.
- **terpene → effect** and **effect → terpene/strain_type**: from the terpenes /
  strain-types education content.
Built by a Celery task `build_adjacency` (nightly); pure data, no PII.

## recompute_affinity (rewrite)
On phone login + nightly: recompute all of the above from `purchase_history`
(now price- and subcategory-aware). Sets `quality_tier`, `novelty_score`,
`bucket_mix`, affinity maps, `computed_at`.

## Privacy
- `profile_summary` stays generic (has_history, top_categories, price_tier) — no
  raw history to the website.
- The LLM prompt receives only derived hints ("prefers premium indica flower;
  brands: A, B; explorer") — never product names + dates. Phone never logged.

## Verification
- For a known phone with history: profile shows non-empty affinity, a sane
  `quality_tier`, and a `novelty_score` that matches behavior (a repeat buyer
  scores low). Adjacency cheatsheet returns plausible substitutes for a sample
  strain/brand. No raw history in any website-facing response.
