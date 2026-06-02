# Subsystem 3 — Personalized + Profit-Aware Ranking

> Date: 2026-05-29 · Status: approved · Parent: master-design.md
> Depends on subsystem 1 (buckets) + subsystem 2 (profile, adjacency).

## Purpose
The brain. Produce a 5-product result set that (a) respects the customer's stated
category/budget/size, (b) feels personally chosen from their history, and (c)
leans toward profit-drivers — a deliberate, well-informed mix, never pushy.

## Scoring (extends current `rank_products`)
Hard gates first (unchanged): location, in-stock (qty ≥ 3), category, price range,
soft size filter, exclude already-shown.

Per-candidate score terms (weights tunable; logged-out zeroes the affinity terms):
- `W_MARGIN` · margin_z (normalized) — keep margin-first baseline.
- `W_BUCKET` · bucket nudge: profit `+1`, core `0`, traffic `−0.3` (but traffic
  `+` when the customer is price-sensitive / low quality_tier).
- `W_AFFINITY` · taste fit: brand + strain_type + terpene + subcategory match to
  the profile's affinity maps (price-/recency-weighted).
- `W_QUALITY` · quality-tier fit: closeness of the product's price_z to the
  customer's `quality_tier` band.
- `W_EFFECT` · requested effect match (existing).
- `W_BUDGETFIT` — now handled by the price-spread selection, not a center bias.

## Repeat vs explore (autodetect + ask + form)
- `novelty_score` from the profile decides the default blend:
  - habitual (low) → favor same brand/strain/quality (their usual).
  - explorer (high) → favor adjacent-new via the cheatsheet (same effect+quality,
    new brand/strain).
- Chat chips `My usual` / `Surprise me` and a form repeat↔explore toggle override
  the autodetected default for that search.

## The "educated mix" (selection)
Keep the distinct-price spread from the current build, but compose the 5 as:
- 1–2 **familiar** (brand/strain the customer has bought or close affinity),
- 1–2 **adjacent-new** (cheatsheet substitutes at their quality tier),
- ≥1 **profit-driver** nudge that still fits taste+budget,
all spread across distinct price points in the chosen range, ranked so the top
card is the best blended score. Anonymous users → margin-first price-spread only
(current behavior).

## Pairing/upsell (existing `pair_for`, enhanced)
Prefer a profit-driver complement; if the customer has history, prefer a
bought-before-not-recently or bought-2+-times item (co-purchase signal), else a
complementary high-margin item. Honors the manual pairings from the admin
(subsystem 5) when present.

## why_this (presentation)
Natural, taste-aware, never a receipt: "Same premium indica lane you usually go
for, a touch higher THC." / "Your go-to brand, new strain worth a try." Sanitize
all interpolated product text. No dates, no exact past SKUs.

## Verification
- Logged-in known phone: top picks visibly reflect their brands/quality; a
  habitual buyer gets familiar-leaning, an explorer gets adjacent-new.
- Profit-drivers are over-represented vs catalog base rate but never violate the
  budget/size gates; price spread preserved.
- Anonymous behavior unchanged (margin-first price-spread). No margin/cost/bucket
  in the response.
