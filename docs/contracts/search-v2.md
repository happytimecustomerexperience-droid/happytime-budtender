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
