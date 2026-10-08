# Search v2 contract (budtender API <-> website chat/questionnaire)

Owner of the backend half: whoever edits `budtender/ranking.py`, `facets.py`, search views. Website agents code against
THIS file; the backend may only EXTEND it (add fields), never rename. Every response field is additive.

## Rules that never bend
- Only in-stock products: `availability=True` and `quantity_on_hand >= MIN_STOCK` (ranking.MIN_STOCK). Nothing that is not
  on the online menu is ever returned, counted or offered as a button.
- **A button exists only if it leads to >= 1 product** under ALL slots chosen so far. Every options endpoint returns only
  options with `count >= 1`. If a whole step would offer 0 or 1 meaningful choices, the response says `skip: true` and the
  UI does not ask that question.
- No cost / margin in any response (tests/test_no_leak.py).

## Categories — synced with Dutchie MASTER categories
Master list (fixed display order): Flower, Pre-roll, Infused Pre-roll, Concentrate, Vape Cartridge, Disposable Vape,
Solid Edible, Liquid Edible, Topical.
`GET /api/v1/products/categories?store=<slug>` ->
`{"categories":[{"master":"Vape Cartridge","value":"<catalog slug used by search/slots>","label":"Vapes","count":42}], "skip":false}`
Only categories with count >= 1 are listed. The backend owns the master->slug map (document it in this file under
"Mapping" once decided). Tinctures: keep only if Dutchie sync still yields them; otherwise they vanish with count 0.

## Search
`POST /api/v1/products/search/` body: `{slots, limit (1..20, default 5), offset (0..15, default 0), exclude_skus, session_token, location}`
- Hard cap: `offset + limit <= 20`. "Show 5 more" = same slots, `offset += 5` (the website sends offset, NOT exclude_skus,
  so ranking is stable and the same criteria apply). Beyond 20 -> empty `results`, `has_more:false`.
- Response: `{results:[...], source, total_matching:int, has_more:bool}` (`total_matching` capped at 20).
- New optional `slots` keys (all optional; unknown keys ignored):
  `thc_min`, `thc_max` (percent, lab THC preferred, else product thc_percent) ·
  `cbd_min`, `cbg_min`, `cbn_min`, `cbc_min`, `thcv_min` (percent, from lab) ·
  `terpenes` (string[] <=5, lowercase names, matches lab terpene names; ranks by presence/amount) ·
  `terpene_total_min` (percent) · `tags` (string[] matched against ProductDetail tags + name tokens) ·
  `q` (free text over name/brand/strain/tags, <=80 chars) ·
  `subcategory` (existing; concentrate types now include live-resin, live-rosin, hash-rosin, rosin, cured-resin, plus
  sauce, diamonds, shatter, wax/budder/badder, sugar, hash, kief, distillate, ... derived from names/tags) ·
  `infusion` (string: e.g. "diamond", "hash", "kief", "live-resin", "distillate"; infused pre-rolls/edibles) ·
  `solventless` (bool; ice-water hash / rosin / "trichome"-only extractions) ·
  `pack` (existing pack sizes for pre-rolls via `size` "single"|"Npk"; lab "pk" = pack count).
  Existing keys unchanged (category, size, price_min/max, doh_only, aroma, sort_by, effect_desired, dominant_terpene...).
- Result items keep the current public_product shape (incl. `lab`, `info`, `size`).

## Narrowing options (every one honours all current slots)
Common request: `POST` JSON `{store, category, slots}` where `slots` = everything chosen so far.
Common response: `{options:[{value,label,count}], skip:bool}` (options sorted by the step's natural order).
- `GET /products/subtypes`, `/products/sizes`, `/products/price-bands`, `/products/doh-options` — EXISTING (query-string);
  they now ALSO accept the extra slots as repeated/JSON params and never list an option with count 0. A POST twin is fine.
- `POST /api/v1/products/facets` — NEW. `{store, category, slots, facet}` with `facet` in
  `thc | terpenes | cannabinoids | extraction | infusion | tags | lab | pack | solventless`.
  Returns only values that exist among in-stock matches (e.g. `terpenes` -> top terpenes present with counts;
  `thc` -> bands like 20-25/25-30/30+; `extraction` -> live-rosin etc.). `skip:true` when it can't narrow usefully.
- `POST /api/v1/products/specify-more` — NEW. `{store, category, slots}` -> `{groups:[{facet,label,options:[...]}]}`: the extra
  questions the "Specify more" button reveals for this category (terpenes, trichome/solventless, lab, hash/extraction,
  infusion type), each already filtered to non-empty. Empty `groups` => the UI hides the button.

## Find similar
`POST /api/v1/products/similar` — keeps its current route if one exists; results MUST be `public_product` items incl.
`lab` and `info` (same card as every other suggestion), in-stock only.

## Events (analytics) — website -> `POST /api/v1/track/` (existing TrackView, extended)
Names are `<=32 chars`, snake_case. Required props on every event: `visitor_id`, `session_id`, `store`, `path`, `device_type`, `ts`.
Event types (frontend emits, backend stores in AnalyticsEvent):
chat_open, chat_close, chat_resume, chat_restart, chat_message_sent, chat_message_received, chip_click (props: label, step),
questionnaire_step_view (step), questionnaire_step_answer (step, value), questionnaire_skip (step), specify_more_open,
search_run (slots_summary, count), picks_view, picks_collapse (reason: reply|button|manual), show_more_click (offset),
product_card_click, product_expand (what: lab|info|explain), order_ahead_click (sku), find_similar_open, find_similar_result,
similar_pick, pair_upsell_view, pair_upsell_accept, phone_capture_submit, phone_capture_skip, voice_offer_click,
bounce (props: last_step, seconds_open), nav_away (props: path, had_open_chat).
Backend never trusts props: `_safe_props` caps size/depth; phone is hashed.

## Backend notes (backend agent, 2026-10-08) — all additive
Code: `budtender/product_attrs.py` (what we know about a product), `ranking.eligible` (THE hard-filter set that
search, every options endpoint and similar share), `facets.py` (v2 options), `product_similarity.similar_products`.
Tests: `budtender/tests/test_search_v2.py`.

### Mapping (master -> `value` -> catalog slugs it draws from)
| master | value (send as slots.category) | label | catalog slugs + rule |
|---|---|---|---|
| Flower | `flower` | Flower | flower |
| Pre-roll | `regular-pre-rolls` | Pre-Rolls | pre-rolls + blunt, NOT infused |
| Infused Pre-roll | `infused-pre-rolls` | Infused Pre-Rolls | pre-rolls (name says infused/diamond/hash hole/moon rock/thca, or e-comm subcategory "infused") + infused-blunt |
| Concentrate | `concentrates` | Concentrates | concentrates |
| Vape Cartridge | `vape-carts` | Vape Cartridges | vape-cartridges, not disposable |
| Disposable Vape | `disposable-vapes` | Disposable Vapes | vape-cartridges whose name/tags say disposable/dispo/AIO/all-in-one/ready-to-use/rechargeable or ProductDetail ecom_subcategory "disposables" |
| Solid Edible | `solid-edibles` | Edibles | edibles (not liquid) + mints |
| Liquid Edible | `liquid-edibles` | Drinks & Liquid Edibles | beverages + edibles that are drinks (subtype drinks, or name: drink/soda/seltzer/shot/syrup/tea/...; e-comm beverage) |
| Topical | `topicals` | Topicals | topicals |
| (extra) Tincture / Capsule | `tinctures` / `capsules` | | listed after the nine only while in stock |
Product.category stays the collapsed catalog slug (dutchie._norm_category); the master split is derived per request.
Legacy values keep their whole-category meaning: `pre-rolls`, `vape-cartridges`, `edibles`, slot keys `cartridge` etc.
Master names themselves ("Vape Cartridge") are also accepted as slots.category.
Categories response items also carry `catalog_slugs`; the response carries `options` (= categories) and
`unlisted` (in-stock products whose catalog slug maps to no master, e.g. an unknown category the sync slugified). It honours store-wide slots
(q, thc, terpenes, doh...) but not subcategory/size/price.

### Search
- Response adds `offset`, `limit` (after clipping). `rank` continues across pages (6..10 on page 2).
- Stock gate: `availability=True` always, plus quantity >= MIN_STOCK from the live sales-floor pull when usable,
  else from the table. Applied first; nothing after it adds a product back.
- `thc_min/max` use the DISPLAYED potency (`thc_percent`: inventory value, else lab total) — not "lab preferred" —
  so a "30%+" pick never shows a 28% card. Products with no THC on file never match a THC filter.
- Lists use ANY: `terpenes`, `tags`, `infusion` (string or string[]). Different slots combine with AND. `q` = ALL
  tokens (substring) over name/brand/strain/type/category/master/tags/subtype words.
- `cbd_min` reads lab `cbd_total` (stored only when >= 1%); `cbg/cbn/cbc/thcv_min` read the lab's three strongest
  stored minors (a weaker minor reads as 0). Terpenes match the stored top-5, canonical names (`beta-myrcene` = `myrcene`).
- `subcategory` is multi-label: a product answers to its legacy subtype (rosin still covers live rosin; live-resin
  still covers cured resin) AND every kind its name/tags name: live-rosin, hash-rosin, rosin, live-resin, cured-resin,
  hash, kief, distillate, diamonds, sauce, badder, shatter, wax, crumble, sugar, rso (+ edible/pre-roll legacy
  subtypes). The strain name is cut out before reading kinds ("Hash Plant" is not hash).
- `infusion` values: diamond, live-rosin, rosin, live-resin, hash, kief, moonrock, distillate (infused pre-rolls/blunts
  and edibles/drinks only). `solventless`: rosin/hash rosin/hash/kief/ice-water/dry-sift/"solventless"/"trichome".
- NEW slot `lab_tested` (bool): a stored lab is on file. `pack` = int or "Npk"/"single" (same as `size`).
- Pack sizes are now exact (no fill with other pack counts). GRAM sizes keep the legacy nearest-weight fill
  (4g -> 3.5g/7g, inside every other hard filter), so a gram size option's count is the exact-weight count and
  the search may show a few more.
- `total_matching` is the length of the one ranked list (capped 20); `has_more = offset + len(results) < total`.

### Options (subtypes / sizes / price-bands / doh-options / facets / specify-more / categories)
- All accept POST `{store, category, slots}` (legacy `{slots:{store,...}}` still works) and GET query strings
  (`?store=&category=&terpenes=a&terpenes=b` or `?slots=<json>`). Legacy keys kept: `subtypes`, `sizes`,
  `bands`+`count`, `doh/non_doh/total/meaningful`; each also returns `options` and `skip`.
- Every option carries `slots`: the exact keys to merge into slots to follow it (list values: append).
- Own-slot rule: step/partition facets (subtypes, sizes, price-bands, doh, thc, pack) and list facets (terpenes,
  tags, infusion) ignore their own slot; refinement facets (extraction, cannabinoids, lab, solventless) honour
  it and only offer options narrower than the current set.
- `skip`: partition steps (sizes, bands, thc, pack) when < 2 options; label steps when no option is both
  non-empty and narrower than the whole set; DOH when not both DOH and non-DOH exist.
- Subtypes now list any count >= 1 (was >= 2).
- THC bands: flower/pre-rolls u20/20-25/25-30/30+; concentrates/vapes u70/70-80/80-90/90+; no category
  u20/20-30/30-60/60-80/80+; edibles/topicals: skip. Each option has `min`/`max` (non-overlapping, inclusive).
- `lab` facet options: `lab_tested`, `terpenes_1|2|3` (`terpene_total_min`). Cannabinoid options: CBD 1%+,
  CBG/CBN/CBC 0.5%+, THCV 0.3%+.
- Specify-more groups per family: flower terpenes/thc/cannabinoids/lab/tags; pre-rolls infusion/terpenes/thc/lab/tags;
  infused pre-rolls infusion/solventless/terpenes/thc/tags; concentrates extraction/solventless/terpenes/thc/
  cannabinoids/lab/tags; vapes extraction/solventless/terpenes/thc/tags; edibles infusion/solventless/tags; else tags.
  A thc/pack group is omitted once that slot is answered. Response also has `skip` and `family`.
- Results are cached 60 s per (store, inventory version, slots).

### Find similar
`POST /api/v1/products/similar` `{store|location, sku | product_id | slug (Product.slug or the website catalog
slug from the name), slots?, limit 1..20, offset}` -> `{anchor:{sku,name,category}, results:[public_product incl.
lab/info], total_matching, has_more, offset, limit, source}`. Same catalog category as the anchor (same master
first), in stock, never the anchor or a listing with its exact name; up to two same-strain picks lead; `why_this`
names only customer-safe reasons (strain/terpene/effects/flavors). No such route existed before (the website's
/api/catalog/similar re-ranks /products/search/ results).
