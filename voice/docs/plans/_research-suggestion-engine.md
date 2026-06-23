# Happy Time Budtender — Suggestion-Engine Mechanism (Voice Stack Research)

> **Purpose:** Exact, code-grounded documentation of the `happytime-budtender` recommendation
> mechanism so the **voice stack** can reuse it: how to get **high-margin** picks (anonymous),
> how to **personalize** for a recognized returning caller (purchase history → taste-first), how
> **co-purchase pairing** works, and the **precise leak-safe HTTP contract**.
>
> **Source root:** `C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender\budtender\`
> **Files read:** `ranking.py`, `pairing.py`, `serializers.py`, `dutchie.py`, `tasks.py`,
> `facets.py`, `auth.py`, `urls.py`, `views.py`.
> **Generated:** 2026-06-22.

---

## 0. TL;DR for the voice stack

1. **High-margin (anonymous) recs:** call **`POST /api/v1/products/search/`** with `slots` and **no
   phone** → ranking uses **`W_ANON` (margin 0.55)**, and even when known, the **final ordering hard-codes
   slot #1 = highest gross-margin $ and slot #2 = highest velocity**. So margin leads by construction.
2. **Personalized (taste-first) recs:** pass the caller's **`phone`** (or a session linked to a
   `CustomerProfile`). The phone resolves a profile; ranking switches to **`W_KNOWN` (affinity 0.34,
   margin drops to 0.22)**. Recognize a returning caller = phone → `CustomerProfile` (built nightly
   from Dutchie transactions).
3. **Pairing/upsell:** **`POST /api/v1/pairing/for-sku`** → ONE lighter, ≤50%-price, complementary,
   high-margin add-on, boosted by the **nightly co-purchase matrix** (confidence-based) + the caller's
   own history.
4. **Leak safety:** every product response is built from an **explicit allowlist
   (`PUBLIC_PRODUCT_FIELDS`)** — **`cost`/`margin`/`velocity` NEVER serialize**. Auth is a single
   **service token** (`Bearer HHT_BACKEND_TOKEN`, constant-time). The voice stack must call through a
   **server-side proxy holding the token** — never expose it to a browser/device.

---

## 1. Data model the engine ranks over

`Product` rows are synced per store from Dutchie POS (`dutchie.fetch_inventory`) and classified
nightly. Fields the ranker uses:

- **Public-ish:** `sku`, `name`, `brand`, `strain`, `strain_type`, `category`, `subcategory`
  (size label like `3.5g`/`10mg`), `price`, `price_was`, `thc_percent`, `dominant_terpene`,
  `unit_weight`, `potency_mg`, `quantity_on_hand`, `availability`, `image_url`, `slug`.
- **SERVER-ONLY (must never leak):** `cost`, **`margin`** (gross-margin **$**, not %), `margin_pct`,
  `margin_z`, `price_z`, **`velocity`** (recency-blended net units/day), `bucket`
  (`core`/`traffic`/`profit`), `bucket_source`.

**Stock gate (everywhere):** `availability=True AND quantity_on_hand >= MIN_STOCK (5)`. `MIN_STOCK=5`
is an **owner policy** — never suggest/pair anything with fewer than 5 on the **sales floor**
(`dutchie._sales_floor_qty` counts the "Sales Floor" room ONLY — back-stock / Quarantine-Returns
never count). This is the single purchasability gate (`dutchie._is_purchasable`): floor stock > 0,
not medical-only, sellable category, has a retail price, on the live menu (`/products` flags +
e-comm category), and **fresh** (`lastModifiedDateUtc` within 60 days — kills "zombie" stock that
404s on Shop Now).

---

## 2. HOW TO GET HIGH-MARGIN RECOMMENDATIONS (anonymous)

### 2.1 The two weight sets (`ranking.py`)

```python
# margin  affinity  effect  category  bucket  quality  budget
W_ANON  = {"margin":0.55, "affinity":0.00, "effect":0.18, "category":0.05, "bucket":0.12, "quality":0.00, "budget":0.10}
W_KNOWN = {"margin":0.22, "affinity":0.34, "effect":0.10, "category":0.04, "bucket":0.12, "quality":0.14, "budget":0.04}
```

- **Anonymous (no profile) → `W_ANON`: margin is the dominant term (0.55).** `affinity`/`quality` are
  0 (no customer to personalize to). The chosen one is picked by `W = W_KNOWN if profile else W_ANON`.
- `BUCKET_NUDGE = {"profit":1.0, "core":0.4, "traffic":0.0}` — a small additive lean toward
  **profit-bucket** items (the store's genuine high-GP SKUs from the bell-curve classifier), weighted
  by `W["bucket"]=0.12`. Never overrides taste/budget gates.

### 2.2 The DETERMINISTIC final ordering overrides the score (the real margin lever)

Even with a profile, `rank_products` **does not return the raw score order** in the normal path. It
hard-pins the first two slots (`ranking.py` "Owner ordering" block):

- **Slot #1 = the single highest gross-margin `$` item** in the matching set: `max(scored, key=lambda t: float(t[1].margin))`.
- **Slot #2 = the highest-`velocity` item** among the rest (ties break to top demand score; with no
  transaction data yet velocity=0 so it falls to best demand — slot never wasted).
- **Slots #3..limit = real demand** (the affinity/effect/budget/bucket score), greedily, with a
  **soft brand-variety penalty** (`0.6 ** brand_count` so a different brand wins ties but a clearly
  stronger same-brand item can still earn its slot).

> **Voice-stack takeaway:** to maximize margin, call search **anonymously** (no phone) — you get
> `W_ANON` margin-0.55 scoring AND the slot-#1 highest-margin pin. The engine is **margin-first by
> design** unless you deliberately hand it a profile.

### 2.3 Premium intent (a separate ordering)

If `slots.price_tier == "top"` OR the low bound `>= $100`, `premium_intent` flips: results order by
**price descending** within the requested weight (score breaks ties), capped to 2 of any one brand for
variety. Price becomes a *preference* (show priciest of the requested weight), not a hard gate — but
**weight always wins over price**.

### 2.4 Filters applied before scoring (all hard, with honest fallbacks)

`location` → `category` (mapped via `CATEGORY_BY_SLOTKEY`) → optional `subcategory` (granular subtype:
rosin/gummies/etc., HARD) → price (`price_min`/`price_max` or tier bounds; skipped for premium intent)
→ `doh_only` (HARD, "DOH" in name; returns nothing rather than off-spec) → **size** (HARD with a
capped nearest-weight fallback: a scarce 4g borrows 3.5g/7g within [0.5×,2×], never a 1g shake or a
28g ounce; if nothing of/near the size survives, returns `[]` — "no matches for these filters",
honest miss over a wrong substitution).

### 2.5 `_why(p, desired, profile)` — the spoken "why this" hook

Returns a short persuasive reason built **only from real signals** (never a fake claim), strongest-
converting first, surfacing the top 2:
1. **Personal hook** — "your go-to {brand}" / "right in your {indica} lane" / "your usual {subcategory}"
   / "exactly your usual quality" (only when profile present).
2. **Live deal** — "on sale — save $X" (only when `price_was - price >= 1`).
3. **Asked-for effect** — "dialed in for relaxed".
4. **Real potency** — "hits hard at 28% THC" (only when `thc_percent >= 25`).
5. **Scarcity** — "almost gone" (only when `0 < quantity_on_hand <= 5`).
6. **Flavor/strain fallback** — "{terpene}-forward" / strain name.

This is the **voice agent's recommendation script source** — read `why_this` aloud verbatim-ish.

---

## 3. HOW TO PERSONALIZE WITH USER INFO (recognize caller → taste-first)

### 3.1 Recognize a returning caller

**Identity key = normalized phone.** `tasks._normalize_phone` → `+1XXXXXXXXXX` (US 10/11-digit;
junk dropped). A `CustomerProfile` is keyed on that phone.

Resolution order inside `ProductSearchView` (`views.py`):
1. The session's linked `customer` (if the session was already tied to a phone), else
2. `_profile_for_phone(request.data["phone"])` — and if found, **link it onto the session**.

To explicitly recognize/resume a caller: **`POST /api/v1/chat/resume-by-phone`** (links the in-flight
session to the customer, fires `recompute_affinity.delay(phone)`, returns prior session + a non-PII
`profile_summary`). **`POST /api/v1/customer/profile-upsert`** ensures a profile exists and recomputes.

### 3.2 Where the profile comes from (Dutchie history → taste)

`tasks.sync_transactions(location, days=90)` builds it nightly:
- Pulls **`/reporting/transactions?includeDetail=true`** (line items: productId, qty, unitPrice,
  unitWeight, isReturned) in **≤31-day windows** (a wide call 400s) via `dutchie.get_transactions_detailed`.
- Maps **customerId → phone** (`dutchie.get_customers`, prefers cellPhone) and **productId → Product**
  (synced inventory) to attach brand/category/strain/strain_type/subcategory/bucket/price_z.
- **Returns & voids skipped** (header `isVoid`/`isReturn`, line `isReturned`); only lines resolving to
  a real synced Product count (trade samples can't inflate anything).
- Folds per-phone lines into `CustomerProfile.purchase_history` (`_fold_history`): one entry per
  product with **`times_bought`**, **`last_bought_at`**, `last_price`, and the joined attributes.
- **Conversion attribution:** a previously-`SuggestedProduct` the caller later bought → `accepted=True`.

### 3.3 `recompute_affinity(phone)` — history → the taste vectors ranking consumes

Frequency-weighted (`times_bought`) and normalized to sum-1:
- `brand_affinity`, `category_affinity`, `strain_type_affinity`, `subcategory_affinity`,
  `terpene_affinity` (joined from each SKU's `dominant_terpene`), `bucket_mix` (core/traffic/profit
  share of what they actually buy).
- **`price_tier`** from mean peer-relative `price_z`: `>=0.4 → "top"`, `<=-0.4 → "value"`, else `"mid"`.
- **`novelty_score`** = distinct products ÷ total purchases (~1 = explorer, low = creature of habit).
- `last_purchase_at`, `total_orders`.

### 3.4 How the profile flips ranking to taste-first

With a profile present, `W = W_KNOWN` (**affinity 0.34 leads, margin 0.22**), and these terms turn on:

- **`_affinity_score(p, profile)`** (capped 1.0): `1.6×brand + 1.0×strain_type + 0.6×category +
  0.6×subcategory + 0.4×terpene` affinity. **Brand + strain_type are the strongest "feels like mine"
  signals.**
- **`_quality_fit`** (W 0.14): peaks when the product's `price_z` sits at the caller's usual tier
  center (`value -0.6 / mid 0 / top 0.6`), fading with distance.
- **`_novelty_bias`** (±0.3 additive): **habitual** buyers get a boost for brands they already buy;
  **explorers** get a boost for brands they have NOT bought (same taste envelope, something new).
- **`_recency_boost`** (+0.10 brand / +0.05 category additive): nudges toward the caller's **most
  recent** purchases (top-3 by `last_bought_at`), so picks track what they're buying lately, not just
  lifetime favorites.
- **`bucket_mix` blend:** the business profit-nudge is blended `0.6×business + 0.4×caller's own mix`,
  so picks match the *kind* of products (core/traffic/profit) they actually buy. **Value-tier callers**
  make traffic-driver loss-leaders acceptable (nudge raised).

> **Voice-stack takeaway:** anonymous = margin-first; **the moment you pass a recognized phone,
> taste leads** (affinity 0.34 > margin 0.22) AND the `_why` strings gain personal hooks ("your go-to
> Phat Panda"). Slot #1 is still the margin pin even when known — so personalization never fully
> abandons margin; it reshuffles slots #3+ to taste.

---

## 4. HOW CO-PURCHASE PAIRING WORKS (`pairing.py` + `tasks.build_copurchase`)

### 4.1 What gets paired

`pair_for(location, anchor, profile)` returns **ONE** add-on: `(pair|None, reason_code, reason_text,
strength)`. It is intentionally a **lighter, cheaper, complementary** impulse item — never a second
main purchase.

- **Complement ladder (`LADDER_COMPLEMENTS`):** each anchor maps to **lighter categories only**,
  ordered by natural attachment. flower/concentrate/cart/disposable → **pre-rolls → edibles →
  beverages**; pre-roll → edibles → beverages; edible → beverages → tinctures; etc. (Pre-rolls lead —
  highest attachment rate in cannabis retail.)
- **Price gate (hard):** `pair.price <= 50% of anchor.price` (`MAX_PAIR_PRICE_RATIO`). The
  `price_fit` term **peaks at ~25%** of anchor (`IDEAL_PAIR_PRICE_RATIO`) — the impulse sweet spot.
  If nothing lighter-and-cheaper is in stock → **show NO pair** (honest miss beats a bad upsell).
- **Admin override:** an active `ManualPairing(anchor_sku → pair_sku)` wins when its product is in
  stock → reason `staff_pick`, strength 1.0.

### 4.2 The "bought together" signal (3 sources, take the max)

`basket = max(co_score, sku_pop, attr_pop)`:
1. **Caller's own history** (`_copurchase_signal`): `times_bought>=2 → 1.0 "bought_2plus_times"`;
   bought-before-but-**not in last 30 days** → `0.7 "bought_before_not_recent"`; bought **very
   recently → 0.0** (don't re-suggest).
2. **Exact-SKU global matrix** (`_global_popularity`): Redis `pair:{loc}:{anchor_sku} → {sku: weight}`.
3. **Durable attribute matrix** (`_attr_popularity`): Redis `pairattr:{loc}:{category|size} →
   {category|size: weight}` — **survives SKU rotation** ("people who buy 3.5g flower add a 1g
   live-resin cart" keeps working as inventory churns).

### 4.3 How the matrices are built (`tasks.build_copurchase`, nightly)

- Treats each `CustomerProfile.purchase_history` as a **basket**; counts co-occurrences across all
  customers.
- **Weight = CONFIDENCE** `confidence(A→B) = P(A∧B)/P(A)` = "what fraction of people who bought A also
  bought B" — the right measure for an **upsell** (deliberately NOT lift, which would filter out a
  universally-popular complement like a cart, which is exactly what you DO want to upsell).
- **Noise gate `MIN_CO = 3`** co-buyers. Writes both the SKU matrix and the durable attr matrix; a
  dict is written for **every** anchor (empty when nothing qualifies) so no stale weights linger.
- Same-category confidences are simply never queried (candidates are restricted to complement
  categories), so the matrix being category-agnostic is fine.

### 4.4 Pairing score & gating

```
score = 0.40·basket + 0.25·customer_fit + 0.15·ladder_rank + 0.15·margin_norm + 0.25·price_fit
```
- `customer_fit = 0.6·_affinity_score + 0.4·_quality_fit` (0 when anonymous).
- `ladder_rank`: earlier in the lighter ladder = higher; **×0.5 if same subcategory/size as anchor**
  (prefer variety, not a near-dupe).
- **`strength` ∈ [0,1]** = `0.45·basket + 0.35·cust + 0.20·price_fit (+0.15 if real history match)`
  — **the gate the voice agent should use to decide whether to even voice the upsell** (only surface a
  genuinely strong pair). The reason text (`_reason_text`) is built from attributes (never the product
  name) so it stays correct as stock rotates.

---

## 5. THE PRECISE HTTP CONTRACT (leak-safe)

**Base:** `/api/v1/` (`budtender/urls.py`). **Auth:** every endpoint except health requires
`Authorization: Bearer <HHT_BACKEND_TOKEN>` (`auth.ServiceTokenPermission`, `hmac.compare_digest`,
**fails CLOSED** if the token isn't configured). **Caller must be the website/voice server-side
proxy** — the token never reaches a browser/device.

### 5.1 Endpoints (method · path · purpose)

| Method | Path | Purpose |
|---|---|---|
| GET  | `/health/` | Open (no token). `{status:"ok"}`. |
| POST | `/chat/session/start` | New session → `{session_token, stage:"WELCOME"}`. Body: `{location, channel}`. |
| **POST** | **`/products/search/`** | **Primary recommender.** See 5.2. |
| GET  | `/products/in-stock/?store=<slug>` | Live sales-floor slugs+stock for the site's "find similar". |
| POST | `/products/price-bands` | Data-driven budget buckets for `{store,category,size,subcategory}`. |
| POST | `/products/subtypes` | In-stock granular subtypes for `{store,category}`. |
| POST | `/products/sizes` | In-stock sizes for `{store,category[,subcategory]}`. |
| POST | `/products/doh-options` | Whether "DOH only?" is a real choice for the filters. |
| **POST** | **`/pairing/for-sku`** | **Upsell add-on.** See 5.3. |
| POST | `/chat/resume-by-phone` | Recognize/resume a caller by phone; returns prior session + `profile_summary`. |
| POST | `/chat/persist/` | Persist session slots/stage/messages (202). |
| POST | `/customer/profile-upsert` | Ensure a profile by phone + recompute affinity. |
| POST | `/track/` | Analytics ingest (phone always HASHED; never errors caller; 202). |
| POST | `/analytics/summary` | Funnel/merchandising counts (owner dashboard). |
| POST | `/feedback/` | Store rating/message (phone hashed). |

**Valid `store` slugs:** `yakima`, `mount-vernon`, `pullman` (from `models.STORES`). Default `yakima`.

### 5.2 `POST /products/search/` — request/response

**Request body:**
```json
{
  "slots": {
    "store": "yakima",
    "category": "flower",           // flower|concentrate|cartridge|edible|tincture (CATEGORY_BY_SLOTKEY)
    "subcategory": "rosin",         // optional granular subtype (HARD filter)
    "size": "3.5g",                 // gram cats: "1g".."28g"; pre-rolls: "single"|"5pk"; HARD+nearest fallback
    "price_min": 20, "price_max": 40,   // OR "price_tier": "value"|"mid"|"top"
    "effect_desired": "relaxed",    // relaxed|uplifted|middle
    "doh_only": false
  },
  "limit": 5,
  "location": "yakima",             // fallback if slots.store absent
  "phone": "+1509...",              // OPTIONAL — presence flips to taste-first (W_KNOWN)
  "session_token": "s-...",         // optional; records SuggestedProduct impressions
  "exclude_skus": ["..."]           // "show me something else" support
}
```

**Response (leak-safe — `serializers.public_product`):**
```json
{
  "results": [
    {
      "rank": 1, "sku": "...", "name": "...", "brand": "...", "strain": "...",
      "price": 38.0, "price_was": null, "thc_percent": 27.3,
      "dominant_terpene": "Limonene", "stock_on_hand": 14,
      "dutchie_link": "/catalog/product/<slug>", "image_url": "...",
      "why_this": "Your go-to Phat Panda · on sale — save $5"
    }
  ],
  "source": "vps"
}
```

### 5.3 `POST /pairing/for-sku` — request/response

**Request:** `{ "location":"yakima", "sku":"<anchor sku>"  // or "slug", "phone":"+1...", "session_token":"..." }`

**Response:**
```json
{ "pairing": { /* public_product shape, or null */ },
  "reason_code": "popular_pair",            // staff_pick|bought_2plus_times|bought_before_not_recent|popular_pair|your_brand|your_lane|pairs_well|none
  "reason_text": "Folks who grab a flower almost always toss in a pre-roll like this — an easy add-on.",
  "strength": 0.62 }                        // gate the upsell on this (only voice a strong pair)
```

### 5.4 THE LEAK GUARANTEE (critical for the voice stack)

- `serializers.PUBLIC_PRODUCT_FIELDS` is an **explicit allowlist**: `rank, sku, name, brand, strain,
  price, price_was, thc_percent, dominant_terpene, stock_on_hand, dutchie_link, image_url, why_this`.
- **`cost`, `margin`, `margin_pct`, `velocity`, `bucket`, `price_z` are never referenced in the
  serializer → they can never reach a client.** A regression test (`tests/test_no_leak.py`) enforces
  this. The ranker uses margin/velocity **server-side only** to order; only the order survives.
- `profile_summary` returns **non-PII** hints only: `{has_history, top_categories[], price_tier}` —
  no raw purchase history. `track/` and `feedback/` **hash the phone** (`sha256`).
- **The voice stack must keep the Bearer token server-side** and surface only `public_product` fields
  to the device/caller. Speak `why_this` and `reason_text`; never speak/store margin or cost.

---

## 6. Facets (questionnaire steps — optional for voice, useful for slot-filling)

`facets.py` precomputes, per store + inventory version, the **subtypes / sizes / price-bands / DOH**
options that ACTUALLY exist in live stock (warmed on every inventory sync; cold combos cached on
first hit). The voice agent can call `/products/subtypes`, `/products/sizes`, `/products/price-bands`,
`/products/doh-options` to know which spoken options are real **before** asking the caller — so it
never offers a size/type/cert that can't be fulfilled. A per-store version stamp invalidates the whole
snapshot atomically on sync.

---

## 7. Freshness & sync cadence (so recs aren't stale)

- **Inventory sync** (`sync_inventory` / `sync_inventory_all`): per store from `/reporting/inventory`
  (single call, ~8k rows incl. unitCost); sales-floor-room-only stock; drops medical-only, off-menu,
  priceless, and **stale >60-day "zombie"** packages. Unknown cost → margin estimated at
  `price × 0.72` (never full price, so cost-less SKUs don't falsely top the margin ranking).
- **Classification** (`classify_products`): buckets every in-stock product **core/traffic/profit** on
  a bell curve within its (category × subcategory) peer group; folds in Redis velocity. Re-runs on
  every inventory refresh.
- **Transactions sync** (`sync_transactions`, 90d): builds purchase history + velocity (recency-blend
  `0.7·28d + 0.3·84d` net units/day, returns/voids excluded).
- **Co-purchase** (`build_copurchase`): nightly confidence matrices.
- **On-request self-heal:** `ProductSearchView` checks `inventory_is_stale(location)` (≥24h) and fires
  `ensure_inventory_fresh.delay()` async — never blocks the response; ranking already filters in-stock.

> **Voice-stack takeaway:** the engine self-heals stock, but it depends on the **Celery beat + Dutchie
> per-store POS keys** being wired. With no creds, every sync is a safe no-op and the service still
> boots (returns `[]`) — so the voice stack must handle empty `results` gracefully.

---

## 8. What the voice + budtender-contract plans (P1) should adopt

1. **Reuse the API as-is** behind the voice server-side proxy (Bearer token) — do **not** re-implement
   ranking; call `/products/search/` and `/pairing/for-sku`.
2. **Margin vs taste switch = presence of `phone`.** Anonymous caller → margin-first; recognized
   caller → taste-first. Make caller-ID (phone) the first thing the voice flow resolves
   (`resume-by-phone`).
3. **Speak `why_this` / `reason_text`;** gate the upsell on pairing `strength` (suggest a threshold,
   e.g. only voice when `strength >= ~0.4`).
4. **Slot-fill from facets** so the agent only asks about options that exist in live stock.
5. **Never voice or log cost/margin** — rely on the allowlist; treat any field outside
   `PUBLIC_PRODUCT_FIELDS` as forbidden on the voice surface.
6. **Honor `MIN_STOCK=5`, 21+/ID, and WA limits** — the engine already enforces stock; the voice
   layer must add the legal/age gating (see `_research-education-blogs.md` §10).
