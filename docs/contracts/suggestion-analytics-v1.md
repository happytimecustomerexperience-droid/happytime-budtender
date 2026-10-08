# Suggestion tracking + conversation analytics v1 (budtender API <-> voice dashboard <-> website)

Goal (owner): for EVERY product we suggest (phone, website chat, questionnaire, find-similar, pairing) keep the FULL product
details (not just a SKU) and track, for 10 days, whether the customer bought that product or a SIBLING (same product,
different strain or size). Show it in the analytics dashboard and on each customer's profile.

## Data (budtender DB, migration owner = the backend-suggestions agent)
`SuggestedProduct` (exists: session, customer, location_slug, sku, kind primary|pairing, source, paired_with_sku, shown_at) gains:
- `snapshot` JSON — what the customer was shown, frozen at suggestion time: `{product_id, name, brand, category, subcategory,
  strain, strain_type, size_label, unit_weight, potency_mg, price, thc_percent, slug, rank, why, kind, source}` (customer-facing
  fields only: never cost/margin).
- `sibling_key` CharField(indexed) — see below; `channel` CharField(indexed) one of `phone|chat|questionnaire|similar|pairing|menu|unknown`
  (the website sends `source` in the search body; phone sends `source:"phone"`; allowlist, default from session.channel);
  `identity_via` CharField (copy of the session's identity_via at suggestion time and updated when the session later links).
`SuggestionOutcome` (one per SuggestedProduct, created with it): `status` in
`pending | bought_exact | bought_sibling | not_bought | unattributable`; `match_kind` in `exact | sibling_size | sibling_strain | sibling_both | ""`;
`matched_sku`, `matched_name`, `matched_amount` (line total, customer-facing), `matched_at`, `window_ends_at` (= shown_at + 10 days,
setting `HHT_SUGGESTION_WINDOW_DAYS` default 10), `evaluated_at`.

## Rules
- Attribution is EVENT-DRIVEN: when a Dutchie transaction line is ingested for a customer, every open suggestion for that customer
  with `shown_at <= tx_time <= window_ends_at` is matched: same sku/product_id => `bought_exact`; same `sibling_key` => `bought_sibling`
  (with match_kind). First qualifying purchase wins; a suggestion gets one outcome. Re-ingesting the same transaction is idempotent.
- `sibling_key` = normalised `brand | category-family | product-line` where product-line is the product name with size tokens
  (10pk, 3.5g, 1g, 100mg...), the product's own strain and pack/size words removed, lowercased. Two products with the same key but
  different strain and/or size are siblings; different brand or different product line are NOT. Document the algorithm + tests with real-looking names.
- A suggestion made to an anonymous session becomes attributable if the session later links to a customer (`link_session`);
  at window close an unlinked one is `unattributable` (never `not_bought`).
- Closing: a periodic job marks `pending` rows whose `window_ends_at + 1 day grace` (transaction sync lag) has passed as
  `not_bought` (customer known) or `unattributable`. Idempotent.
- Backfill: existing SuggestedProduct rows get a best-effort snapshot from `Product` (when the sku still exists; else mark `snapshot_partial`)
  and are evaluated against purchase history only where `purchase_history[].last_bought_at` gives certainty (bought after shown_at AND
  within the window); everything else stays `unattributable` — never guess.

## API (backend token only; add to STAFF_ONLY lists)
- `POST /api/v1/analytics/suggestions` `{days<=365 default 30, store?, channel?, kind?, category?, brand?}` ->
  `{window_days, totals:{suggested, products, customers_known, pending, bought_exact, bought_sibling, bought_any, not_bought, unattributable,
  conversion_rate, exact_rate, sibling_rate}, by_channel[], by_store[], by_category[], by_kind[], by_rank[], by_day[], top_products[], never_bought[],
  recent_buyers[]}` where each group row = `{key,label,suggested,pending,bought_exact,bought_sibling,not_bought,unattributable,conversion_rate}`;
  `top_products[]` rows carry the FULL snapshot fields (name, brand, category, strain, size_label, price, thc_percent) + times_suggested,
  customers, outcome counts, last_suggested_at. `conversion_rate = bought_any / (bought_any + not_bought)` (decided, attributable rows only).
- `POST /api/v1/analytics/suggestions/list` `{days, offset, limit<=100, filters..., sort}` -> paged rows: suggested_at, channel, store, customer
  {id, name?}, full snapshot, status, match_kind, matched_name/sku/amount/matched_at, days_to_purchase, session ref (opaque id).
- `POST /api/v1/customer/suggestions` `{id}` -> that customer's rows (same shape). Customer name allowed (staff), never a phone.
- `POST /api/v1/chat/history` (exists) additionally accepts `customer_id` (budtender CustomerProfile id) to list that customer's sessions
  (metadata + message_count + channel + identity_via; bodies only via `{id}` as today).
- The search body gains optional `source` (allowlist `chat|questionnaire|similar|pairing|menu|phone`).
