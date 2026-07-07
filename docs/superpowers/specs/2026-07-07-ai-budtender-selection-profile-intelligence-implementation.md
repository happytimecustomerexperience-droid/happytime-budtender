# AI Budtender Product Selection, Customer Profiles, and Product Intelligence

> Date: 2026-07-07
> Status: implementation spec
> Owner: Vladi
> Repo: `happytime-budtender`
> Primary apps: `budtender`, `pos`, `customers`

## Problem Statement

Budtenders and the AI assistant need to recommend products that feel personally
chosen, not generic. The current system already shares the POS menu, customer
purchase profiles, and the budtender ranking engine, but the profile view and
product classification need to expose richer category, subcategory, strain,
terpene, bucket, and retail context so a budtender can understand why a customer
likes something and quickly find available matching products.

The goal is not to invent a second recommender. The goal is to enrich the
existing `CustomerProfile`, `Product`, `purchase_history`, `budtender.engine`,
and POS profile surfaces so recommendations, profile browsing, and product
relationships all use one shared source of truth.

## Current Architecture To Preserve

- `budtender.models.Product` is the synced product intelligence row.
  It already has `category`, `subcategory`, `strain`, `strain_type`,
  `dominant_terpene`, `effects`, `flavors`, `price`, `price_was`, `cost`,
  `margin`, `margin_pct`, `velocity`, `price_z`, `margin_z`, `bucket`,
  `bucket_source`, `unit_weight`, and `potency_mg`.
- `budtender.models.CustomerProfile` is the phone-keyed customer profile.
  It already has affinity maps, `purchase_history`, `price_tier`,
  `novelty_score`, `bucket_mix`, THC band, and total order fields.
- `budtender.engine` is the one scoring and pairing brain for both website/voice
  API and POS menu. Keep it shared.
- `pos.ranking` and `pos.pairing` are thin re-exports from `budtender.engine`.
  Do not fork ranking logic into POS.
- POS product add and checkout are the live Dutchie register write path.
  Do not move cart writes into budtender APIs.
- Public product serializers must never expose cost, raw margin, internal bucket
  strategy, or z-score internals.

## Goals

1. Classify every in-stock product into a retail bucket inside its true peer
   group: category -> subcategory -> traffic/core/profit using a 15/70/15 price
   curve.
2. Enrich each customer's profile with category, subcategory, strain, strain
   type, terpene, flavor, potency, bucket, price tier, and product relationship
   signals.
3. Make the AI budtender suggestions explainable: why this product, why this
   bucket, why this similar product, and whether it is familiar, adjacent-new, or
   a profit step-up.
4. Add a POS customer profile drilldown where category pills open subcategories,
   and subcategories open product tables with bucket, retail data, availability,
   and menu links.
5. Track suggestion exposure, clicks, adds, checkout conversion, product views,
   and transaction-derived affinities so recommendations can improve from real
   behavior and real sales.

## Non-Goals

- No new external recommender service. Use Django, ORM, Redis/cache, and the
  existing shared engine.
- No new dependency unless a standard library or existing dependency cannot do
  the job.
- No LLM-only product decisions. The model can phrase a recommendation, but
  product choice must come from deterministic product/profile data.
- No public exposure of server-only commercial data. Cost, raw margin, and
  bucket internals are staff-only.
- No wholesale rewrite of POS. Add profile and ranking intelligence through the
  existing views/templates first.

## Definitions

### Category

The major product lane. Examples:

- `flower`
- `pre-rolls`
- `vapes`
- `concentrate`
- `edibles`
- `tinctures`
- `topicals`
- `beverages`
- `other`

### Subcategory

The peer group inside a category. This is what makes price buckets fair.
Examples:

- Flower: `1g`, `3.5g`, `7g`, `14g`, `28g`
- Pre-rolls: `single`, `infused`, `pack`, or gram-based when available
- Vapes: `0.5g`, `1g`, `2g`, `disposable`, `cart`
- Concentrates: `0.5g`, `1g`, `2g`
- Edibles: `5mg`, `10mg`, `20mg+`, `100mg pack`
- Tinctures/topicals: potency or package-size based when available

### Bucket

Use the existing `Product.bucket` choices:

- `traffic`: the cheapest 15 percent of products in the same category and
  subcategory peer group.
- `core`: the middle 70 percent.
- `profit`: the highest-priced 15 percent.

Important: this spec uses bucket as a retail positioning bucket based on price
within peer group. Keep `margin_pct`, `margin`, and `velocity` as additional
ranking signals, but the default bucket assignment should follow the requested
15/70/15 retail bell curve.

Manual bucket overrides in Django admin still win.

## Product Classification Requirements

### P0 - Retail Bucket Classifier

Update `budtender.tasks.classify_products` so auto buckets are assigned by
retail price percentile inside `(location_slug, category, subcategory)`.

Acceptance criteria:

- Given at least 8 in-stock `flower + 7g` products in Yakima, the cheapest 15
  percent are `traffic`, the highest-priced 15 percent are `profit`, and the
  rest are `core`.
- Given fewer than 8 products in a `(category, subcategory)` group, fallback to
  `(category, *)`.
- Given a product with `bucket_source="manual"`, classification updates
  `price_z`, `margin_z`, `subcategory`, and `classified_at`, but does not change
  `bucket`.
- `price_z` remains the peer-relative z-score used for customer price tier and
  quality fit.
- `margin_pct`, `margin_z`, and `velocity` remain populated for ranking and
  analytics.

Implementation notes:

- Reuse existing `Product.bucket`, `bucket_source`, `price_z`, `margin_z`,
  `velocity`, `margin_pct`, and `classified_at`.
- Use existing `budtender.ranking.size_label` for subcategory derivation.
- Use a tiny percentile helper already present in `budtender.tasks`.
- This is a behavior change, not a schema change.

Suggested logic:

```python
traffic_cutoff = percentile(prices, 15)
profit_cutoff = percentile(prices, 85)

if price <= traffic_cutoff:
    bucket = "traffic"
elif price >= profit_cutoff:
    bucket = "profit"
else:
    bucket = "core"
```

Tie handling:

- If many products share the cutoff price, allow distribution to be slightly
  wider than 15/70/15. Deterministic price grouping is better than splitting
  identical retail prices arbitrarily.

### P1 - Classification Diagnostics

Add a small internal helper that returns a bucket summary per
`location/category/subcategory`:

- count
- min price
- max price
- traffic count
- core count
- profit count
- average margin percent by bucket
- average velocity by bucket

Do not build a full UI for this in P0. A management command printout or staff
dashboard helper is enough.

## Customer Profile Enrichment Requirements

### P0 - Purchase History Row Contract

Every `CustomerProfile.purchase_history` row should carry enough data to render
the profile and rank future suggestions without re-querying Dutchie transaction
lines.

Required fields per aggregate row:

- `product_id`
- `sku`
- `product_name`
- `brand`
- `category`
- `subcategory`
- `strain`
- `strain_type`
- `dominant_terpene`
- `effects`
- `flavors`
- `bucket`
- `price_z`
- `last_price`
- `times_bought`
- `qty`
- `last_bought_at`
- `first_bought_at`

Nice-to-have when source data exists:

- `thc_percent`
- `potency_mg`
- `unit_weight`
- `total_terpenes`
- `cbd`
- `vendor`

Implementation notes:

- Extend `_fold_history` in `budtender.tasks`.
- Extend the line dict produced by `sync_transactions`.
- Do not store cost or raw margin in `purchase_history`.
- Keep the aggregate JSON shape. Do not add a separate transaction fact table in
  P0 unless the current JSON cannot support the UI.

### P0 - Affinity Maps

`recompute_affinity` should calculate and save:

- `brand_affinity`
- `category_affinity`
- `subcategory_affinity`
- `strain_type_affinity`
- `terpene_affinity`
- `flavor_affinity`
- `bucket_mix`
- `price_tier`
- `novelty_score`
- `thc_min`
- `thc_max`
- `last_purchase_at`
- `total_orders`

Acceptance criteria:

- A customer who repeatedly buys `flower + 7g` has that category and subcategory
  ranked above weaker categories/subcategories.
- A customer who buys mostly expensive peer-group products gets `price_tier="top"`.
- A customer who repeats the same few products has a low `novelty_score`.
- A customer who buys a wide variety has a higher `novelty_score`.
- Terpene and flavor affinity populate when source product data exists.

Weighting:

- Start with frequency weighting because it is already available.
- Add recency weighting only if it stays small: for example, multiply purchases
  from the last 90 days by `1.25`.
- Add price weighting only through `price_tier` and `price_z`, not separate
  handcrafted weights scattered through views.

### P1 - Category Preference Tree

Build a pure helper that converts a profile into a tree:

```python
[
  {
    "category": "flower",
    "weight": 0.52,
    "times_bought": 31,
    "subcategories": [
      {
        "subcategory": "7g",
        "weight": 0.41,
        "times_bought": 14,
        "buckets": {"traffic": 2, "core": 8, "profit": 4},
        "products": [...]
      }
    ]
  }
]
```

Recommended location:

- `budtender/profile_tree.py` for pure helpers, or `customers/intelligence.py`
  if the helper is only consumed by POS.

Keep it pure and assert-testable.

## Product Relationship Requirements

### P0 - Similarity Signal

Add one pure similarity function that scores how similar two products are.

Recommended location:

- `budtender/product_similarity.py`

Inputs:

- Canonical product dict from `budtender.engine.from_product`, or POS live dict.

Signals:

- same category: strong
- same subcategory: strong
- same strain: strong
- same strain type: medium
- same dominant terpene: medium
- overlapping effects: medium
- overlapping flavors: small
- same bucket: small
- nearby price_z: medium
- nearby THC/potency/unit weight: small to medium
- same brand: context-dependent; useful for "usual", but do not let brand alone
  make products similar if the format/category is different

Output:

```python
{
  "score": 0.0_to_1.0,
  "reasons": ["same 7g flower", "hybrid", "limonene-forward", "same price lane"]
}
```

Acceptance criteria:

- Two `flower + 7g + hybrid` products with similar terpene/price score higher
  than a `flower + 28g` product.
- A `vape` and an `edible` from the same brand do not score as highly similar
  just because brand matches.
- Function works without database access.

### P1 - Similar Products In Profile Tables

In each subcategory product table, include:

- products the customer bought
- in-stock exact matches
- in-stock similar replacements when exact item is unavailable
- reason tags from the similarity function

Do not store a permanent all-to-all similarity table in P1. Compute on demand
over the live store inventory for the selected profile/category/subcategory.
If it is slow later, cache by `store:category:subcategory` in Redis.

### P1 - Co-Purchase Relationship

The current `build_copurchase` already builds:

- `pair:{location}:{sku}`
- `pairattr:{location}:{category|subcategory}`

Keep it. Expand profile UI and suggestion reasons to use these relationship
signals where useful:

- "customers who buy this flower often add this edible"
- "this is a common add-on for 7g flower buyers"

Do not use co-purchase as "similarity"; it means relationship, not similarity.

## Product Selection And Ranking Requirements

### P0 - Product Choice Contract

AI budtender suggestions must pass these gates before scoring:

- correct store
- in stock
- quantity at or above `MIN_STOCK`
- matches requested category when category is explicit
- matches requested subcategory/size when explicit, unless fallback is needed
- respects requested price range or budget
- excludes already-shown SKUs when the caller passes `exclude_skus`
- never returns products not sellable in the active menu/register path

### P0 - Known Customer Ranking

Known customers should rank taste-first with profit awareness.

Ranking terms to preserve or strengthen in `budtender.engine.score_one`:

- affinity fit: brand, category, subcategory, strain type, terpene, flavor
- quality fit: product `price_z` compared to customer `price_tier`
- bucket nudge: `profit` > `core` > `traffic`, except value customers can prefer
  traffic/core
- margin signal: keep as a secondary business signal
- effect fit: requested effect
- THC band fit when known
- recency boost for recent categories/brands
- novelty behavior: habitual customers get familiar picks; explorers get
  adjacent-new picks

Important: if two products are both good taste matches, prefer the stronger
business product. If a high-profit product is a weak taste match, do not force it
above a strong customer match.

### P0 - Suggestion Mix

For a known customer, the top suggestion set should intentionally mix:

- familiar: something they already like or a close exact-lane match
- adjacent-new: same category/subcategory/effect/quality lane, but a new strain,
  terpene, or brand
- profit step-up: a `profit` bucket item that still fits their taste and budget
- fallback core: reliable in-stock item when the above are sparse

Do not guarantee exact counts in every sparse category. Instead, guarantee the
ranker attempts these lanes in order and falls back honestly.

Suggested output annotation per product:

```python
recommendation_type = "usual" | "similar" | "adjacent_new" | "profit_step_up" | "popular" | "fallback"
why_this = "Same 7g flower lane they buy most, with a higher-price profit pick."
```

Staff-only surfaces can show `recommendation_type`, bucket, and retail strategy.
Public AI/API surfaces should keep reasons customer-safe and avoid internal
bucket language.

### P0 - Anonymous Ranking

Anonymous shoppers should stay margin/profit aware, but still respect:

- store
- category
- subcategory/size
- budget
- effect
- availability
- price spread

Do not personalize anonymous customers beyond in-session taste signals already
captured by `customers.tracking.accrue_taste`.

### P1 - "My Usual" And "Surprise Me"

Add a lightweight mode parameter to ranking:

- `usual`: favor exact/familiar products and close substitutions
- `surprise`: favor adjacent-new products with high similarity and matching
  quality tier
- default: choose based on `novelty_score`

The POS can expose this later as a segmented control. The voice/web assistant can
pass it when the customer says "my usual" or "surprise me".

## POS Customer Profile UI Requirements

### P0 - Profile Overview

Update `pos/customer_full.html` and related view context so the budtender sees:

- ranked category preference pills, wide and tappable
- each pill shows category name, share/weight, and units or purchase count
- top brands
- top strain types
- top terpenes
- bucket mix
- price tier
- novelty label: `habitual`, `balanced`, or `explorer`
- suggested products

Pill behavior:

- Click category pill -> show subcategory rows for that category.
- Click subcategory -> show product table for that category/subcategory.

This can be implemented server-rendered first. Use normal links or HTMX partials,
whichever is smallest in the existing template.

### P0 - Subcategory Product Table

For a selected category/subcategory, render products the customer bought with:

- product name
- brand
- strain
- strain type
- dominant terpene
- effects/flavors when present
- times bought
- last bought
- last price
- current retail price if in stock
- bucket
- price tier/bucket label
- availability status
- menu link

Menu link rules:

- If exact `product_id` is in current POS inventory, link to
  `reverse("product", args=[ProductId])`.
- If exact product is unavailable, link to menu search with category/subcategory
  filters and query prefilled enough to find similar/current replacements.
- Link label should be operational: `Open item`, `Find similar`, or
  `Search menu`.

### P1 - In-Stock Replacement Rows

When exact products are unavailable, show up to 5 in-stock replacements from the
same category/subcategory ordered by similarity score and current ranking score.

Columns:

- replacement product
- current price
- bucket
- similarity reasons
- add/menu link

### P1 - Staff-Only Internal Labels

Inside POS staff profile pages, it is okay to show:

- bucket
- price lane
- recommendation type
- "profit step-up" labels

Still do not show raw cost or raw margin unless the route is explicitly
manager/staff-only and already has staff gating.

## Tracking Requirements

### P0 - Keep Current Visit Tracking

Preserve existing `ShopVisit` and `ShopEvent` tracking:

- visit start
- customer selected
- profile view
- full profile view
- menu browse
- search
- category browse
- product view
- suggestions shown
- item add
- checkout
- checkout failed

### P0 - Add Missing Metadata Where Cheap

For `suggestions_shown`, include:

- product ids
- recommendation type when available
- category
- subcategory
- bucket on staff/POS event metadata only

For `item_add`, include:

- `from_suggestion`
- source recommendation type when known
- product category/subcategory/bucket when available

Do not let tracking failure break the sale path. Keep `customers.tracking`
degrade-safe.

### P1 - Conversion Attribution

Strengthen accepted suggestion attribution:

- suggestion shown -> product added to POS cart in same visit
- suggestion shown -> checkout completed with that product
- suggestion shown -> later transaction sync folds that SKU into purchase history

Use existing `SuggestedProduct.accepted` for budtender API suggestions.
For POS-only suggestions, `ShopEvent` can be enough in P1. Do not add a second
analytics store unless queries are too slow.

## Category Data Completeness

Codex should enrich product parsing and profile display per category using fields
that already exist in Dutchie/product rows when available.

### Flower

Important fields:

- strain
- strain type
- subcategory/weight: `1g`, `3.5g`, `7g`, `14g`, `28g`
- THC percent
- dominant terpene
- terpene percent if available
- effects
- flavors
- brand
- bucket
- price_z
- current price

### Pre-Rolls

Important fields:

- strain
- strain type
- single vs pack
- infused vs non-infused when detectable
- unit grams
- THC percent
- dominant terpene
- effects
- flavors
- brand
- bucket
- price_z

### Vapes

Important fields:

- cart vs disposable
- size: `0.5g`, `1g`, `2g`
- strain
- strain type
- oil/extract type when available
- THC percent
- dominant terpene
- brand
- bucket
- price_z

### Concentrates

Important fields:

- concentrate type when available: wax, resin, rosin, badder, sugar, shatter,
  sauce, hash
- size
- strain
- strain type
- THC percent
- terpene data
- brand
- bucket
- price_z

### Edibles And Beverages

Important fields:

- potency per serving
- total package potency
- serving count when available
- flavor
- effect intent when available
- brand
- bucket
- price_z

### Tinctures And Topicals

Important fields:

- potency
- ratio when available
- package size
- application type when available
- brand
- bucket
- price_z

## Data And File Targets

Likely implementation files:

- `budtender/tasks.py`
  - retail 15/70/15 classifier
  - richer transaction line folding
  - affinity recompute improvements
- `budtender/engine.py`
  - recommendation type annotations
  - stronger known-customer mix logic
  - optional mode: default/usual/surprise
- `budtender/product_similarity.py`
  - pure similarity scoring helper
- `budtender/tests/test_product_similarity.py`
  - assert-based similarity coverage
- `budtender/tests/test_customer_history_sync.py`
  - update expected purchase history fields
- `budtender/tests/test_engine.py`
  - known customer suggestion mix and bucket behavior
- `pos/views.py`
  - profile tree context and selected category/subcategory drilldown
- `pos/templates/pos/customer_full.html`
  - wide category pills and product tables
- `pos/static/pos/app.css`
  - only minimal layout styles needed for profile pills/table
- `pos/tests/test_personalization.py`
  - profile tree or ranking behavior tests
- `pos/tests/test_views.py`
  - profile drilldown renders links/status

## Implementation Phases

### Phase 1 - Classification And Profile Data Contract

Deliver:

- retail 15/70/15 bucket classifier
- richer `purchase_history` row fields
- recompute affinity updates
- focused tests

Verification:

```powershell
python manage.py check
pytest budtender/tests/test_customer_history_sync.py -q
pytest pos/tests/test_personalization.py -q
```

### Phase 2 - Product Similarity And Suggestion Mix

Deliver:

- pure product similarity helper
- recommendation type annotations
- known-customer mix improvements
- no public cost/margin leak regression

Verification:

```powershell
pytest budtender/tests/test_product_similarity.py -q
pytest budtender/tests/test_engine.py -q
pytest budtender/tests/test_no_leak.py -q
pytest pos/tests/test_pairing.py -q
```

### Phase 3 - POS Profile Drilldown

Deliver:

- category preference pills
- subcategory drilldown
- product table with bucket, retail data, availability, and menu links
- similar replacements when exact product is unavailable if cheap enough

Verification:

```powershell
pytest pos/tests/test_views.py -q
pytest pos/tests/test_tracking.py -q
python manage.py check
```

### Phase 4 - Tracking And Analytics Polish

Deliver:

- richer suggestion/add metadata
- suggestion conversion query helpers
- dashboard/profile summaries if needed

Verification:

```powershell
pytest pos/tests/test_tracking.py -q
pytest pos/tests/test_analytics.py -q
pytest budtender/tests/test_no_leak.py -q
```

## Acceptance Criteria

- A known customer's POS profile shows ranked category pills.
- Clicking a category reveals subcategories they actually buy.
- Clicking a subcategory shows a table of products they bought in that lane.
- Each table row shows bucket, retail price context, purchase count, last bought,
  and whether the product is currently available.
- Available exact products link directly to the POS product page.
- Unavailable products offer a menu/search path or similar in-stock replacements.
- `flower -> 7g` products classify into traffic/core/profit using the requested
  15/70/15 retail price curve.
- Known-customer AI suggestions visibly respect category/subcategory, brand,
  strain type, terpene/flavor, price tier, and novelty behavior.
- Profit products are nudged only when they still fit the customer's taste and
  constraints.
- Anonymous suggestions still work and do not require a profile.
- Public API product responses still exclude cost, margin, bucket internals, and
  z-scores.
- Tracking captures suggestion exposure, add, and checkout outcomes without
  breaking sale flow on tracking errors.

## Open Questions

- Should staff POS pages show raw margin dollars to managers only, or keep all
  margin hidden and show only bucket/price lane? Default: hide raw margin.
- Should bucket use price-only retail positioning forever, or should a manager
  mode later support margin-aware bucket overrides? Default: price bucket for
  auto, manual override for exceptions.
- How much terpene data does Dutchie actually provide per store/category?
  Default: consume it when present, degrade cleanly when missing.
- Should exact historical transaction lines be stored long term? Default: no for
  P0; aggregate `purchase_history` is enough. Add a fact table only when the UI
  or analytics require per-receipt reconstruction.

## Guardrails For Codex

- Keep the shared engine shared. Do not create a separate POS recommender.
- Reuse existing fields first. Add schema only when the existing JSON/model
  cannot support the requested behavior.
- Add one small runnable test for each non-trivial logic change.
- Keep public serializers allowlisted.
- Keep POS write paths secure: cart price/batch/serial still resolve server-side.
- Do not let analytics/tracking failures block checkout.
- Mark any intentional shortcut with a `ponytail:` comment and name the ceiling.
