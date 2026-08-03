# Custom Order storefront — how it works, and what the URL must carry

**Status:** built and tested, 2026-08-02.

We host the whole shopping experience ourselves at `/custom-order`: our own menu over
live register inventory, our own cart, and a checkout that drops the order straight into
the POS queue for a budtender to claim. **The Dutchie embedded menu is not used**, and the
shopper never signs in to Dutchie.

**Owner decisions captured:** discount applied manually in-store by a budtender; customer
identified by a tokenized phone in the link; emails come from `alpine-automations`
(AlpineIQ); out-of-stock fallback widens to *same category + same weight, any brand*;
checkout collects full name + phone + optional email.

**The flow:**

```
 AlpineIQ email ──signed URL──▶ /custom-order          bundle live-resolved,
                                                        seeded into the cart
                                     │
                                     ▼
                               /custom-order/menu       our menu, live inventory,
                                                        POS filters, add to cart
                                     │
                                     ▼
                            /custom-order/checkout      name + phone + optional email
                                     │
                                     ▼
                       PhoneCartDraft(released, online) ──▶ POS "Orders waiting"
                                                             budtender clicks Load,
                                                             applies bundle discount,
                                                             takes payment in store
```

Nothing here writes a Dutchie order. The register remains the only thing that can check
out — the same line the voice channel already refuses to cross.

---

## 0. Why we host the menu instead of embedding Dutchie's

The original idea was:

> …then you populate all the items exactly how you see that in the .json with dutchie api calls, and refresh so the embedded menu gets updated with the items.

**That cannot work.** Not "is hard" — cannot. Here is the proof, gathered from your own capture plus live testing against `happytimeweed.com` and `dutchie.com`.

### The Dutchie cart never leaves the browser

Your capture (`net-capture-dutchie.com-2026-08-02T20-25-07-249Z.json`, 77 requests) records a real session where **two items were added to the cart**. The only cart-related network traffic in the entire capture is:

| Operation | What it is |
|---|---|
| `ComputeWithPriceCartV2` ×3 | A **pricing** query. Takes the *whole cart array as input*, returns totals. |
| `PersistCheckoutV2` ×3 | Fires at checkout, not at add-to-cart. |

There is no `AddToCart`, no `CreateCart`, no `UpsertCart`. The cart array simply grows from one line to two between two `ComputeWithPriceCartV2` calls:

```jsonc
// request #36
"cart": [
  {"productId": 3483543, "ecommKey": "67d31b5f14fbb574430a8cf4-1/8oz", "quantity": 1, "unitPrice": 20, "unitWeightGrams": 3.5}
]
// request #60 — one more item, still no mutation in between
"cart": [
  {"productId": 3483543, "ecommKey": "67d31b5f14fbb574430a8cf4-1/8oz", "quantity": 1, "unitPrice": 20, "unitWeightGrams": 3.5},
  {"productId": 3554685, "ecommKey": "69c59555b1133ba4775c561b-1g",   "quantity": 1, "unitPrice": 45, "unitWeightGrams": 1}
]
```

Confirmed directly: the cart is `localStorage["stored-cart"]` on the **dutchie.com** origin. Clicking ADD TO CART in a live session materialised that key and flipped the badge 0 → 1. Each line stores Dutchie's full Apollo-normalised `Products` object (`__typename`, `_id`, `POSMetaData.canonicalID`, `Options`, `measurements`, …) — not a `{productId, qty}` tuple. There is no server-side cart to POST into, and the shape could not be synthesised externally even with write access.

### And we cannot reach into the iframe

The menu on `happytimeweed.com` is a cross-origin iframe:

```html
<iframe id="dutchie--embed__iframe" src="https://dutchie.com/embedded-menu/happytime/products/flower?dutchie_sid=…">
```

Reading it from our page throws, as the browser is designed to:

```
iframe.contentWindow.localStorage
→ SecurityError: Blocked a frame with origin "https://happytimeweed.com"
  from accessing a cross-origin frame.
```

### And no URL parameter does it

The embed script (`api.dutchie.com/api/v2/embedded-menu/r8kjngwN38XgnWq9a.js`) was decompiled. Its `initialFrameURL()` builder recognises exactly: bare `dtche=<path>`, `dtche[product]`, `dtche[poscid]`, `dtche[category]`, `dtche[path]`, `dtche[location]`, `dtche[debug]` — and passes every other `dtche[x]=y` through verbatim as `?x=y` on the iframe URL. **None touch the cart.** A grep of the 5.5 MB Dutchie menu bundle confirms: `query.cart` 0 hits, `addToCart` 0, `shareCart` 0, `restoreCart` 0.

> **Bug found in passing:** `marketing_dashboard/.../storefront/templates/storefront/checkout.html:483` calls `navigateEmbed({'dtche[showCart]': 'true'})`. `showCart` is a MobX action only — it is never read from the URL. That call is a silent no-op today.

### The one path that looked promising — tested, and it doesn't restore

`PersistCheckoutV2` and the 13 `checkoutToken` occurrences in the capture looked like a genuine server-side cart, so this was tested end-to-end rather than reasoned about. Findings, all directly observed:

**Server-side cart state does exist, and we can write it.** From a browser context, `PersistCheckoutV2` with `token: null` returns HTTP 200 and mints a real UUID — with no pre-existing local cart:

```jsonc
{"data":{"persistCheckoutV2":{"checkoutToken":"f85b3914-be67-4b8e-a7d2-749d67cb9612"}}}
```

Passing an existing token back overwrites that cart and echoes the same token. So this is a real, writable server-side record.

**But nothing reads it back.** A real 35 KB cart object (1 item, added through the UI, full Apollo `Products` payload) was persisted under a fresh token, local storage was wiped, and the resume URL loaded:

| Test | Payload | Restored |
|---|---|---|
| `/embedded-menu/happytime/?checkoutToken=…` | hand-built | 0 items |
| `/embedded-menu/happytime/?checkoutToken=…` | real 35 KB cart | 0 items |
| `/dispensary/happytime?checkoutToken=…` | real 35 KB cart | 0 items |

`getTrackedCart` exists as a type but does not rehydrate the consumer cart. The conclusion: `PersistCheckoutV2` is **abandoned-cart tracking — write-only from the shopper's side**, not cart restore.

Two further notes: bare server-to-server POSTs to `dutchie.com/api-0/graphql` are **Cloudflare 403'd** (the call only succeeds from a browser context), and GraphQL introspection is disabled on the ecom endpoint.

### What *does* work

| Capability | Status |
|---|---|
| Deep-link to a **product page** — `?dtche[product]=<slug>` | ✅ works, already used in production |
| Deep-link to a **filtered menu** — `?dtche[category]=flower` | ✅ works, already used on the homepage |
| Put items **in the cart** programmatically | ❌ impossible from our origin |

There is one escape hatch we did not take: **Dutchie Plus** (`plus.dutchie.com/plus/2021-07/graphql`) is a genuine server-side cart API — `createCheckout`, `addItem`, `updateQuantity`, plus a `redirectUrl`. It is partner-key gated, and Dutchie is sunsetting Plus during 2026 in favour of "Ecommerce Pro". Building on a sunsetting, partner-gated API to reach a cart we don't control was the worse trade.

**So we host the storefront ourselves.** We already have the two things that matter: live register inventory (`pos.catalog`) and a staff handoff the POS understands (`PhoneCartDraft`). Owning the menu means no Dutchie sign-in, no iframe, no cart we cannot write to — and the shopper's session is ours to retain.

---

## 1. The architecture

```
  ┌─ AlpineIQ email (alpine-automations) ────────────────┐
  │  "Roll & Relax Bundle — 20% off"  [Add bundle to cart]│
  └───────────────────────┬───────────────────────────────┘
                          │  signed URL (§2)
                          ▼
        /custom-order?b=…&loc=yakima&i=…&exp=…&sig=…
                          │
   verify HMAC ▸ resolve every item against LIVE stock
   ▸ substitute what sold out (§5) ▸ SEED THE CART
                          │
                          ▼
        /custom-order/menu — our menu, live inventory,
        the same filters the POS uses (§4). Add anything.
                          │
                          ▼
        /custom-order/checkout — full name, phone,
        optional email. Repriced from live stock.
                          │
                          ▼
     PhoneCartDraft(status=released, source=online)
                          │
                          ▼
        POS "Orders waiting" ▸ budtender clicks Load
        ▸ applies the bundle % ▸ takes payment in store
```

### The cart is a `PhoneCartDraft`

The shopper's cart is literally a `PhoneCartDraft` in `open` state, keyed by a 30-day
`htco` cookie. One decision buys three things:

* **retention** — close the tab, come back next week, cart intact
* **the POS already understands it** — `_phone_cart_queue`, `phone_cart_claim`, the queue panel
* **one concept for staff** — a phone order and an online order are the same row with a different `source`

Checkout flips `open → released`, which is the moment it becomes claimable. An `open`
row is explicitly excluded from the POS queue: a shopper still browsing must never have
their cart loaded out from under them at a register.

### Prices are never trusted from the client

Every mutation and every render re-reads live inventory. The browser only ever sends a
product id and a quantity. A cart that sat in a cookie for a week is repriced before the
shopper sees it; anything sold out is **flagged and kept visible**, not silently removed,
and checkout is blocked until they deal with it.

### Every order is wired to a Dutchie customer

`cart_submit` refuses to run without an `AcctId`, so an order that reaches the register
with nobody attached is a dead end — the budtender has to re-find the customer by hand
with the shopper standing there. So the order carries the customer:

| When | Where | What happens |
|---|---|---|
| Order placed | public checkout | **Read-only** lookup by phone. Match → stamp `dutchie_acct_id`. No match → `customer_status = new`. Lookup down → `unresolved`. |
| Order claimed | POS, staff auth | Matched → **auto-selects** the customer into the session. `new` → re-checks, then **creates the guest and selects it**. `unresolved` → tells the budtender to look them up. |

The split is deliberate. A read is safe to expose publicly; a *create* is not — an
unauthenticated guest-create endpoint is a spam vector, and every other Dutchie write in
this repo sits behind staff auth. Three details that matter:

* Phones match on **digits**, not string form. Dutchie stores whatever shape the guest was
  created with, so `(509) 555-1212` and `5095551212` are the same person.
* `unresolved` is never treated as "no account". Creating a duplicate guest for an existing
  customer because Dutchie was briefly down is worse than asking staff to search.
* The claim **re-checks before creating** — the shopper may have been created at the door
  between ordering and the budtender clicking Load.
* DOB is never collected online. Guests are created without one; the customer shows ID at
  the counter, which is the same thing the POS `start` path already does.

A Dutchie outage never blocks an order: the lookup degrades, the order still lands, and
the queue shows a `LOOK UP` tag.

### What the budtender sees

The POS queue panel (`_queue_panel.html`) is now "Orders waiting" and carries both phone
and online orders. Each row shows the customer's name and contact plus loud tags:
`ONLINE`, the bundle discount (`20% Roll & Relax`), and `NEW CUSTOMER` / `LOOK UP`. The
bundle discount tag matters most — it is applied **by hand** at the register, so if a
budtender misses it the customer silently doesn't get it.

---

## 2. The URL contract — what the email must carry

Everything the page needs arrives in the query string. Nothing is looked up from a prior session.

### Format

```
https://happytimeweed.com/custom-order
  ?b=<bundle_slug>
  &loc=<store_slug>
  &i=<sku>:<qty>          (repeated, one per bundle slot)
  &c=<customer_token>     (optional)
  &exp=<unix_ts>
  &sig=<hmac_sha256>
```

### Parameters

| Param | Req | Format | Notes |
|---|---|---|---|
| `b` | ✅ | slug | `roll-relax` \| `vape-munch` \| `weekend`. Selects discount depth + slot rules. |
| `loc` | ✅ | slug | `yakima` \| `mount-vernon` \| `pullman`. Drives which inventory is queried **and is printed on the page**. |
| `i` | ✅ | `<sku>:<qty>` | Repeated. `sku` = Dutchie **`product_id`** preferred (integer, e.g. `3483543`); a `sku` string also resolves. `qty` is an integer ≥ 1. Order is preserved and defines slot order. |
| `c` | ⬜ | opaque | HMAC-SHA256 of the E.164 phone, truncated to 32 hex chars, keyed by `BUNDLE_URL_SECRET`. **Never the raw phone.** Enables personalised substitution ranking from `CustomerProfile`. Omit for anonymous sends. |
| `exp` | ✅ | unix seconds | Link expiry. Recommend `send_time + 14 days`. Past expiry the page still renders but the bundle badge reads "this offer has ended". |
| `sig` | ✅ | hex | HMAC-SHA256 over the canonical query string (see below), keyed by `BUNDLE_URL_SECRET`. |

### Why `sig` is not optional

The page tells a budtender "this customer gets 30% off". Without a signature, anyone can hand-edit the URL to claim a 30% bundle on arbitrary items. The signature makes the URL a coupon that cannot be forged. **I need one shared secret**, `BUNDLE_URL_SECRET`, present in both `alpine-automations/.env` (to sign) and `happytime-budtender/.env` (to verify). 32+ random bytes, hex or base64.

### Canonical string to sign

Sort params by name, exclude `sig`, join as `k=v` with `&`, no URL-encoding of the separator:

```
b=roll-relax&c=a3f9…&exp=1755302400&i=3483543:1&i=3554685:2&loc=yakima
```

Repeated `i` values sort lexicographically among themselves. Signature = `hmac_sha256(BUNDLE_URL_SECRET, canonical).hexdigest()`.

### Worked example

```
https://happytimeweed.com/custom-order?b=roll-relax&loc=yakima
  &i=3483543:1&i=3554685:2&i=3601122:1
  &c=a3f91c88de4b7205ff31c0a9e7b6d412
  &exp=1755302400
  &sig=9f2c1b7e04a6d38851cc90fe2b7a4d1e6c3f80a95d2e7b14cc0af39e8d5b6127
```

### What `alpine-automations` must do

The AlpineIQ creative CTA needs a per-contact URL. Two options:

1. **Pre-baked per contact** — `alpine-automations` builds the full signed URL per recipient at campaign-build time and injects it as a merge field. Most robust; works with any AlpineIQ template. **Recommended.**
2. **Merge-field template** — only viable if AlpineIQ can compute an HMAC, which it cannot. So: option 1.

`python manage.py build_bundle_urls` in `alpine-automations` emits a `contact_id,phone,url` CSV ready for upload as a custom field. Existing bundle campaign spec to align with: `alpine-automations/campaign_specs/august-bundles-yakima.json` (creative `august-bundles-email-yakima`, discounts `292200`/`292201`/`292202`).

> **Worth knowing:** those three discount IDs are real Dutchie discounts and were verified live (`PauseType=0`) on 2026-08-02, so the discount may already auto-apply at the register rather than needing manual budtender action. The page copy tells the shopper to mention the bundle either way, which is safe under both, and the POS queue now shows a loud `20% Roll & Relax`-style tag on the order so a budtender can apply it by hand if it doesn't fire.

### Bundle slot rules

Encoded server-side, keyed by `b`, so a tampered `i` list cannot claim the wrong depth:

| `b` | Depth | Slots |
|---|---|---|
| `roll-relax` | 20% | 1× flower 3.5g · 2× pre-roll 1pk (regular or infused) · 1× edible/drink 10pk |
| `vape-munch` | 25% | 1× vape cart or disposable · 1× pre-roll 1pk (regular or infused) · 1× edible/drink 10pk |
| `weekend` | 30% | 1× flower 3.5g · 1× vape cart or disposable · 2× single pre-roll (regular or infused) · 1× edible/drink 10pk |

Each incoming `i` is matched to a slot by category + size. An item that fits no slot renders as a normal add-on line, outside the discount.

---

## 3. Inventory freshness — the repo-wide fix

Your rule: *only accurate current inventory, updated by API calls, no stale DB table.*

**There are currently two inventory brains, and only one obeys that rule.**

- ✅ **The POS obeys it.** `pos/catalog.get_inventory()` (`pos/catalog.py:89`) pulls live `product_SearchV2` from the register, caches per store in Redis with a stampede lock, and `warm_menu` keeps it hot so requests almost never pay the slow pull.
- ❌ **The public/voice API does not.** Every stock and price answer the website, the chatbot and the phone agent give is read from the `budtender_product` table, refreshed by celery-beat with a staleness guard that **fails open** — `budtender/tasks.py:35-38` reports "fresh" when `SyncState` is missing, and `tasks.py:94`'s `if seen:` means an empty Dutchie pull leaves stale rows in place and stamps nothing. A silently broken sync looks healthy forever.

### Must change (stock/price → live)

| # | Path | Lines | Why it matters |
|---|---|---|---|
| 1 | `budtender/views.py` `InStockProductsView` | 245-279 | The website's in-stock guarantee |
| 2 | `budtender/views.py` `ProductBySkuView` | 282-308 | **Highest priority** — this *is* the voice agent's `check_inventory`. It tells a live caller "yes, in stock, $X." |
| 3 | `budtender/ranking.py` | 407, 416 | Candidate set + price-band filter behind `ProductSearchView` |
| 4 | `budtender/engine.py` | 520-522, 529-530 | Pairing/upsell candidate sets |
| 5 | `budtender/views.py` phone-cart quote | 964-971, 134-151, 172 | The `not_in_stock` decision and every line price. Its `"source": "current_public_product_price"` label is currently a lie. |
| 6 | `budtender/facets.py` | 75-76 | Price bands only |
| 7 | `budtender/serializers.py` | 32, 36 | `price` and `stock_on_hand` on the public serializer |

### Must NOT change — legitimate enrichment

`strain_type`, `dominant_terpene`, `effects`, `flavors`, `image_url`, `subcategory`, `bucket`, `velocity`, `margin_pct`, `price_z`, and the whole `CustomerProfile` affinity model are **enrichment**, not stock. They are correctly DB-backed and joined onto live rows by `customers/intelligence.load_product_enrichment`. Ripping these out would break ranking, the chatbot and the POS. The rule is about *stock and price*, not about every DB read.

### Also fix while in there

- `budtender/tasks.py:35-38`, `:94` — make the staleness guard fail **closed**; stamp a failure on a 0-row pull.
- `budtender/engine.py:40 MIN_STOCK = 5` vs `pos/catalog.py:162 qty > 0` — these disagree *by accident*. Recommend keeping **≥ 5 for anything a customer sees** (protects against promising the last unit to two people) and `> 0` in-store where a budtender can see the shelf. Decide it deliberately; don't inherit it.
- `price_was` is a **price** riding in on the enrichment dict (`budtender/reads.py:71` → `pos/catalog.py:72`) and gets rendered next to a live price. Stale compare-at pricing beside live pricing is a trust and compliance problem, not a cosmetic one.
- `pos/catalog.py:56` — `thc` passes through uncoerced (`float | str | None`). Any THC band filter or substitution score will throw or mis-rank. Add a parse helper.

---

## 4. The menu — reuse, don't rewrite

The public menu runs on the same data layer as the in-store one, so staff and shoppers
see one truth. Category tabs, search, brand, strain type and sort all come from
`pos.catalog`, and in-stock-only is enforced inside `query` itself.

**Compose as-is** (pure, no request/user/session/DB):

| Function | `pos/catalog.py` | Gives you |
|---|---|---|
| `_normalize` | 45-86 | live register row → 38-key display dict |
| `categories` | 113-119 | category tabs with counts |
| `facets` | 122-133 | brands, strain types, effects, price range |
| `query` | 158-192 | search + filter + sort; **hard-filters in-stock at 162**; supports `cat`, `subcat`, `q`, `brand`, `brand_q`, `strain_type`, `effect`, `price_min`, `price_max`, `thc_min`, `doh_only`, `sort` |
| `_SORTS` | 136-142 | price asc/desc, THC, popular |
| `get_inventory` | 89-110 | live-cached per-store inventory |
| `find_item` | 145-155 | authoritative row by product_id or serial |

**Do NOT reuse:**

- `pos/templates/pos/_product_card.html` — reverses `{% url 'cart_add' %}` and `{% url 'product' %}`, both `@login_required`; `product` also needs `session["acct_id"]` (`pos/views.py:1483-1484`). Anonymous hits 302 or 500.
- `pos/templates/pos/_menu.html` — renders `acct_name` (lines 22, 74) and staff persona segmentation (lines 4-9). Customer PII leak.

Build new public templates; reuse the *data layer* only.

### The leak surface is the real risk

Auth in this app is per-view `@login_required` — there is no middleware gate (`core/settings.py:71-80`), so **a new view is public by omission**. `_normalize` emits `margin_pct`, `velocity`, `price_z`, `bucket` (staff-only scoring) plus register plumbing `ProductId`/`BatchId`/`SerialNo`/`package_id`/`UnitPrice`/`RecUnitPrice`. And `budtender.engine.rank` stamps a margin-derived `score` on every row (`engine.py:604`) that `pos/tests/test_no_leak.py` does **not** currently guard.

Every public response goes through an explicit allowlist serializer — extend the existing pattern at `budtender/serializers.py:13-40`, do not invent a second one — and `score` gets added to `test_no_leak.py`.

---

## 5. Substitution — what is actually achievable

When a bundle item is out of stock: same category + same weight, any brand (your call), ranked by everything else we know.

**Hard gates (never cross):**
1. `raw_category` exact — a vape does not substitute for flower.
2. Size equality (`unit_grams`) for flower and pre-rolls — a 1 g cart does not satisfy a 3.5 g flower slot, and crossing it breaks the bundle's discount math.

**Ranking signals, in weight order:**
3. `subcategory` exact (`3.5g`, `1g`, `10mg`, …) — from enrichment, high weight
4. Price band — `abs(price − target) / target ≤ 0.20`, computed on the **live** register price
5. `strain_type` match (indica/sativa/hybrid)
6. Dominant terpene match
7. `effects` / `flavors` Jaccard overlap
8. Potency band — `potency_mg` for edibles (clean float); `thc` for flower *only after the parse helper above*
9. Co-purchase affinity from the `pair`/`pairattr` Redis matrices (`tasks.py:463`), plus the shopper's own `CustomerProfile` when `c` is present
10. **Brand as tiebreak only, never as a gate** — per your decision, we widen past brand rather than leave a slot empty

**The taxonomy trap, closed:** `budtender/dutchie.py _norm_category` and `pos/imagemap.category_key` produce *different* slug sets (`vape-cartridges` vs `vapes`, `concentrates` vs `concentrate`). A substitution engine crossing those vocabularies mis-matches silently. `bundles/catalog.py CATEGORY_ALIASES` is the single bridge; everything canonicalises through `canon_category()`.

Brand, category and price are reliably available on the **live** row. Size/weight and the taste signals come from the enrichment join — meaning a product missing from `budtender_product` can still be matched on the hard gates, just ranked less well. That degradation is safe.

### Two store-identity bugs found and fixed in passing

The POS keys stores as `yakima | mtvernon | pullman`; `PhoneCartDraft`, `Product` and
`CustomerProfile` key them as `yakima | mount-vernon | pullman`. Mount Vernon is the one
that differs, and two places joined them without translating:

* `pos/views.py _phone_cart_queue` filtered `location_slug` by the POS store key — so a
  **Mount Vernon order was invisible to the Mount Vernon register**. This affected the
  existing voice path too, not just the new one.
* `pos/views.py phone_cart_claim` assigned `session["store"] = draft.location_slug`.
  `"mount-vernon"` is not in `load_stores()`, so `_active_store` silently fell back to the
  **first** store — a Mount Vernon draft would load against Yakima stock.

`dutchie/stores.py` now owns the mapping (`location_slug()` / `store_key()`) and both call
sites translate. Covered by `StoreKeyTranslationTests`.

---

## 6. Where the code lives — as built

| Piece | Repo | Path |
|---|---|---|
| Signed-URL verify | `happytime-budtender` | [`bundles/signing.py`](../bundles/signing.py) |
| Bundle + slot definitions, store identity | `happytime-budtender` | [`bundles/catalog.py`](../bundles/catalog.py) |
| Live resolution + substitution engine | `happytime-budtender` | [`bundles/resolver.py`](../bundles/resolver.py) |
| Cart (session retention, repricing) | `happytime-budtender` | [`bundles/cart.py`](../bundles/cart.py) |
| Landing, menu, cart, checkout, success | `happytime-budtender` | [`bundles/views.py`](../bundles/views.py) |
| Templates / CSS / JS | `happytime-budtender` | `bundles/templates/bundles/`, `bundles/static/bundles/` |
| Live stock + price for the API and voice | `happytime-budtender` | [`budtender/live_stock.py`](../budtender/live_stock.py) |
| Store key ⇄ location_slug | `happytime-budtender` | [`dutchie/stores.py`](../dutchie/stores.py) |
| Signed-URL **builder** + CSV export | `alpine-automations` | `audiences/bundles.py`, `audiences/management/commands/build_bundle_urls.py` |

The `bundles` app owns **no models of its own** — the cart and the resulting order are both
`PhoneCartDraft` rows. Migration `budtender/0006` adds `source`, `contact_phone`,
`contact_email` and `bundle_slug` to support online orders.

### Routes

| Route | What it does |
|---|---|
| `GET /custom-order` | emailed bundle: verify → resolve live → seed cart |
| `GET /custom-order/menu` | full storefront |
| `GET /custom-order/results` | product grid partial (HTMX/fetch target; `?format=json` for JSON) |
| `GET /custom-order/cart` | cart partial |
| `POST /custom-order/cart/add\|update\|remove` | cart mutations, repriced server-side |
| `GET\|POST /custom-order/checkout` | order form → released draft → success |

**Hosting:** Django serves these, mounted in `core/urls.py` *before* the POS root include so
a POS route can never shadow them. Point `happytimeweed.com/custom-order` at it with a
Next.js rewrite to keep the customer-facing URL on-brand.

---

## 7. Running it

Generate the shared secret once and put the SAME value in both `.env` files:

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

Build the links for a campaign:

```bash
python manage.py build_bundle_urls --bundle roll-relax --store yakima --items 3483543:1,3554685:2 --contacts contacts.csv --write urls.csv
```

`contacts.csv` needs a `phone` column (`contact_id` passed through when present). Output is `contact_id,phone,url` — upload as an AlpineIQ custom field and reference it from the creative's CTA. AlpineIQ cannot compute an HMAC, so the URL must be pre-baked per contact.

You do **not** need to rebuild links when stock moves. Every product id is re-resolved against live inventory when the shopper opens the page; anything sold out is substituted then.

### Settings

| Setting | Default | What it does |
|---|---|---|
| `BUNDLE_URL_SECRET` | — | Shared HMAC key. **Required** when `DEBUG=0`; the app refuses to boot without it. |
| `BUNDLE_MIN_STOCK` | `2` | "More than one in stock" before a product may be offered. |
| `BUNDLE_DRAFT_TTL_HOURS` | `4` | How long a placed order is held. |
| `BUNDLE_MAX_ORDER_TOTAL` | `300` | **Floor** for the order cap, not the cap. The live cap is calibrated from real sales — see below. |
| `EMAIL_HOST` etc. | unset | Order confirmations. Unset ⇒ dummy backend, nothing sent, checkout unaffected. |

### The order cap is calibrated, not guessed

Nothing is paid up front, so an unbounded order is unbounded staff labour and real held
inventory against no commitment. The cap is derived from the **p99 of real completed
basket totals** (`bundles/calibration.py`), recomputed weekly by
`budtender.tasks.calibrate_order_caps` and stored per store in `Setting`.

Measured over 51,930 real baskets (90-day window):

| Store | Baskets | p50 | p90 | p95 | **p99** | max | Cap applied |
|---|---|---|---|---|---|---|---|
| Yakima | 43,868 | $45 | $130 | $170 | **$295** | $2,059.50 | $300 |
| Mount Vernon | 4,177 | $50 | $135 | $175 | **$268** | $470 | $300 |
| Pullman | 3,885 | $65 | $173 | $230 | **$370** | $1,385 | **$370** |

The flat $300 would have been **wrong for Pullman** — its p99 is $370, so roughly 1% of
genuine Pullman baskets would have been rejected at checkout. That is exactly the kind of
error a hand-picked number produces and a calibrated one doesn't.

p99 rather than max, deliberately: Yakima's largest basket is $2,059 and Pullman's is
$1,385: single outliers that should not set the ceiling for everyone. The configured floor
also stops a quiet quarter calibrating the cap *down* to something that rejects ordinary
orders, and a store with fewer than 50 baskets is left on the floor rather than calibrated
off noise.

Inspect or re-run any time:

```bash
python manage.py calibrate_order_cap --apply
```

### Staff alert

The queue panel already re-polls every 5s. It now carries `data-order-tokens`, and
`pos/static/pos/app.js` compares each poll to the previous one: any token that wasn't
there before rings a two-tone chime (synthesised via WebAudio — no asset, CSP-safe),
pulses the panel, and marks the tab title `(1) New order`. The marker clears on the next
click, keypress or tab focus. The first poll after page load establishes the baseline, so
existing orders never trigger it, and `prefers-reduced-motion` swaps the pulse for a
static outline.

### Confirmation email

When the shopper gives an email, `bundles/emails.py` sends the order back to them — items,
subtotal, pickup name, order code, hold time, the bundle reminder, and the 21+/ID note.
Best-effort throughout: no email address, no mail server, or a dead SMTP all return
`False` quietly. The order is already saved and already visible to staff, so a mail
failure must never surface as a failed checkout.

### Verified behaviour

| Check | Evidence |
|---|---|
| Signature round-trips across both repos | Live cross-repo run: alpine builds → budtender verifies |
| Forged bundle slug (20% → 30%) rejected | `bad signature` |
| Injected extra item / attacker-signed link rejected | `bad signature` |
| Sold-out bundle item substituted same category + size, any brand | `test_sold_out_item_is_substituted_from_the_same_category_and_size` |
| Size boundary never crossed (28g ≠ 3.5g slot) | `test_size_is_a_hard_gate_for_flower` |
| Cart survives across sessions | `test_cart_survives_across_requests_via_the_cookie` |
| Cart repriced from live inventory, never from the client | `test_price_is_repriced_from_live_inventory_not_stored` |
| Sold-out cart line flagged, not silently dropped | `test_a_sold_out_line_is_flagged_not_silently_dropped` |
| Checkout blocked while a line is unavailable | `test_checkout_blocked_while_a_line_is_sold_out` |
| Order total computed server-side | `test_total_is_computed_server_side_from_live_prices` |
| Over-cap order refused | `test_order_over_the_cap_is_refused` |
| Existing customer matched on digits, not string form | `test_matches_on_digits_not_string_form` |
| Dutchie outage → `unresolved`, never a duplicate guest | `test_dutchie_outage_is_unresolved_not_new` |
| Claim auto-selects the matched customer | `test_claim_auto_selects_the_matched_customer` |
| Claim creates + selects when there's no account | `test_claim_creates_an_account_when_there_is_none_then_selects_it` |
| Public endpoint never creates a guest | `test_checkout_never_creates_a_guest_from_the_public_endpoint` |
| Nothing writes a Dutchie order | `test_it_never_writes_a_dutchie_order` |
| No margin/velocity/price_z/register plumbing on any public page | `FORBIDDEN` sweep on all six pages |
| Mount Vernon orders visible to the Mount Vernon register | `test_pos_queue_finds_a_mount_vernon_order` |
| Open carts not claimable at a register | `test_open_carts_are_not_claimable` |
| Missing secret fails at boot, not at open rate | `guard_check` + `test_prod_guard_rejects_missing_bundle_secret` |

Plus, on the operational side: cap calibrated from real sales, chime + flash + tab marker
on a new order, and a confirmation email that degrades silently when unconfigured.

**393 tests pass** in `happytime-budtender`, **12** in `alpine-automations`. Django system
check clean, migrations apply from empty, ruff clean on every file touched, and the
production guard verified both ways.

---

## 8. Still open

**One item, and it needs you rather than code:**

**Request the PreOrder scope on the Dutchie POS key.** `GET /preorder/Status` returns 403
with an otherwise-valid key. That scope would let an order land in Dutchie's own
fulfilment queue in addition to ours (support.dutchie.com article 27660267271187).
Everything above ships and works without it — this is upside, not a blocker.

**Worth knowing before launch:**

* Email confirmations are wired but **dormant until `EMAIL_HOST` / `DEFAULT_FROM_EMAIL`
  are set**. Until then checkout works exactly as now and simply sends nothing.
* The chime needs one user interaction on the POS screen before browsers permit audio.
  In practice a budtender clicks something within seconds of loading; the visual flash and
  tab marker work regardless.
* Caps recalibrate weekly. If you change pricing significantly, run
  `python manage.py calibrate_order_cap --apply` rather than waiting for the cron.
