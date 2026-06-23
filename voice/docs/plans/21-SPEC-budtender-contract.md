# 21 — SPEC — VOICE ⇄ BUDTENDER HTTP CONTRACT — Authoritative Interface Spec

> **Status:** EXECUTABLE SPEC (authoritative for the voice⇄budtender data-plane boundary). Written 2026-06-22.
> **Subsystem:** S2 (Dutchie suggestions) — the data plane of `01-ARCHITECTURE.md` §3. **Implements/honors:** ADR-004 (budtender is a separate HTTP microservice; voice never re-implements Dutchie/ranking), ADR-005 (margin-first `W_ANON` when UNKNOWN / taste-first `W_KNOWN` when KNOWN), ADR-006 (returning-caller via peppered phone-hash), ADR-007 (ONE gated pairing upsell), ADR-008 (leak-safe allowlist serializer — cost/margin can NEVER be spoken), ADR-009 (speak OUT-THE-DOOR prices), ADR-019 (per-store Dutchie keys ONLY in budtender; constant-time Bearer compare; fail-closed), ADR-020 (`voice/tools/` package + registry).
> **Read order before executing (mandatory):** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → `_research-suggestion-engine.md` → this file. P1 (`11-P1-DUTCHIE-SUGGESTIONS.md`) is the build doc that consumes this contract; P4 (`14-P4-dashboard-publish.md` §4.6) consumes the one *admin* endpoint (`/admin/ranking-weights`); P5 (`15-P5-polish-brand.md` §3.2/§3.6) consumes the cartridge forward + `/analytics/summary`.
> **Ports from / cites (real paths):** the **happytime-budtender** service at `C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender\budtender\` — `urls.py` (route map), `views.py` (request/response shapes), `serializers.py` (`PUBLIC_PRODUCT_FIELDS`~L13, `public_product`~L24, `profile_summary`~L47), `ranking.py` (`W_ANON`~L14, `W_KNOWN`~L15, `MIN_STOCK`~L24, `CATEGORY_BY_SLOTKEY`~L32, `rank_products`~L465, `_why`~L676), `pairing.py` (`pair_for`~L122, `MAX_PAIR_PRICE_RATIO`~L35, strength gate ~L195), `auth.py` (`ServiceTokenPermission`, `hmac.compare_digest`~L23), `dutchie.py` (`get_customers`~L305, `get_transactions_detailed`~L351, `_is_purchasable`~L211). swedish-bot phone-hash: `C:\Users\vladi\OneDrive\Desktop\swedish-bot\crm\models.py` + `crm/profile.py` (`PHONE_HASH_PEPPER`).
>
> **One-line goal:** pin the EXACT HTTP contract `voice/budtender_client.py` speaks to the happytime-budtender microservice — every endpoint the voice repo calls, request params, leak-safe response JSON, Bearer auth, the margin-first/taste-first selection trigger, the returning-caller personalization handshake, timeouts/caching/latency budget for one voice turn, and the NEW endpoints budtender must add (as budtender-side TODOs) — so P1 can build `budtender_client.py` and the tool handlers against a frozen interface, and so the Leak-Guard + idempotency contract tests have a fixed shape to assert.

---

## 1. Goal & scope

### 1.1 In scope (this doc freezes)
1. **The Bearer auth contract** the voice repo uses to call budtender (`Authorization: Bearer <HHT_BACKEND_TOKEN>`; constant-time; fail-closed) — §3.
2. **Every budtender endpoint the voice repo calls**, with the exact verb, path (trailing-slash-accurate, copied from `urls.py`), purpose, and which voice tool/handler maps to it — §4. The five the voice surface needs: **search/suggest**, **check-by-sku** (inventory gate), **pairing** (upsell), **facets** (slot options), **customer-history** (returning-caller).
3. **Request params + leak-safe response JSON** for each, grounded in `views.py`/`serializers.py` (NOT the research brief's paraphrase where it drifted — see the `price` vs `price_otd` reconciliation in §5.3 + TODO-B1) — §5.
4. **The margin-first (`W_ANON`) vs taste-first (`W_KNOWN`) selection rule** — which request field flips it (presence of a resolvable caller identity), and the resulting ordering guarantees — §6.
5. **The returning-caller personalization handshake** end-to-end: caller number → peppered phone-hash (PII discipline) → caller lookup → history → affinity → taste-first ranking — §7. Includes the **phone-hash vs raw-phone reconciliation** (budtender currently keys on a *normalized raw phone*, not the voice repo's peppered hash — a real boundary mismatch resolved here with a budtender-side TODO).
6. **Timeouts, caching, and the per-voice-turn latency budget** — §8.
7. **`voice/budtender_client.py` method list** (the thin Bearer client P1 builds) — §9.
8. **NEW endpoints budtender must add** (budtender-side TODOs, none of which block P1 — the voice repo ships against the contract with documented fallbacks) — §10.
9. **Acceptance criteria + the contract test suite**, including the non-negotiable **no-leak assertion** (no `"cost"`/`"margin"` substring in ANY response the voice repo handles) and the HMAC/timeout/idempotency gates — §11/§12.

### 1.2 Out of scope (owned elsewhere)
- The Vapi tool *definitions* + `server.url` wiring + the `entry_router`/`budtender` assistant prompts — `11-P1-DUTCHIE-SUGGESTIONS.md` (P1) + `tools/provision_vapi.py` (P0).
- Re-implementing Dutchie access or ranking — **forbidden** (ADR-004). The voice repo only *calls* budtender.
- The Vapi webhook HMAC verify (that is the voice⇄Vapi boundary, `core/middleware.py`, P0) — this doc is the voice⇄budtender boundary only.
- KB/FAQ retrieval — that is `kb/` (`faq_lookup` tool), not budtender.
- The dashboard weights-tuner UI — `14-P4-dashboard-publish.md`; this doc only freezes the *admin* HTTP contract it posts to (§10 TODO-A1 + §5.6).

### 1.3 Non-negotiable boundaries (binding, restated from the ADRs)
- **Leak-safe (ADR-008):** the voice repo treats ANY field outside budtender's `PUBLIC_PRODUCT_FIELDS` allowlist as forbidden. `cost`, `margin`, `margin_pct`, `velocity`, `bucket`, `price_z` **never** appear in a budtender response (the allowlist serializer never references them — `serializers.py`~L13). The voice repo adds a defensive contract test asserting no `"cost"`/`"margin"` substring in any response body (§12, mandatory gate). The agent is therefore **physically incapable** of speaking cost/margin.
- **Token stays server-side (ADR-019):** `HHT_BACKEND_TOKEN` lives only in the voice repo's env + budtender's env; it is never sent to Vapi, a browser, or a device. budtender's `auth.ServiceTokenPermission` constant-time-compares it and **fails closed** if unset (`auth.py`~L16-17).
- **No Dutchie key in the voice repo (ADR-004/019):** per-store Dutchie POS keys live ONLY in budtender. The voice repo holds `HHT_BUDTENDER_BASE_URL` + `HHT_BACKEND_TOKEN` (+ `HHT_BUDTENDER_TIMEOUT`) and nothing else POS-related.
- **OTD spoken (ADR-009):** when the agent quotes a price it quotes **out-the-door (tax-included)**. budtender today returns a pre-tax **`price`** (not `price_otd`) — §5.3 + TODO-B1 resolve where the OTD uplift happens until budtender ships `price_otd`.
- **Graceful-empty (ADR-004 latency property / research §7):** with no Dutchie creds or a cold cache, budtender returns `[]` safely and still boots. Every voice tool handler MUST treat an empty `results` as "I don't have anything in stock for that right now" — never an error, never a hallucinated pick.

---

## 2. Dependencies (what must exist first)

| # | Dependency | Where it comes from | What this contract consumes from it |
|---|---|---|---|
| D1 | The happytime-budtender service reachable over HTTPS with `HHT_BUDTENDER_BASE_URL` set + per-store Dutchie keys configured **inside budtender** | budtender repo (O-1 placeholder — the URL/keys are owner-supplied; the contract ships against a stub until confirmed) | The base URL every call targets; the live `/health/` probe. |
| D2 | `HHT_BACKEND_TOKEN` shared secret, identical in the voice repo env and budtender env | `03-CONVENTIONS.md` §3.4; budtender `auth.py` | The `Authorization: Bearer` header on every non-health call. |
| D3 | swedish-bot peppered phone-hash mechanism ported into the voice repo (`crm/models.Caller` + `PHONE_HASH_PEPPER`) | **P0** ports `swedish-bot/crm/` (roadmap §7); `03-CONVENTIONS.md` §3.9 (`PHONE_HASH_PEPPER` MUST differ from `DJANGO_SECRET_KEY`) | The returning-caller handshake (§7) hashes the caller number with the pepper before any DB write. |
| D4 | `voice/tools/` package + `TOOL_REGISTRY` dispatch (the parallel-safe scaffold) | **P0** (ADR-020) | The suggest/check/pair handlers register here; this contract defines what each one sends to budtender. |
| D5 | `voice/models.VoiceCall` durable log (stores per-call `store`, outcome, suggested SKUs; never the raw phone) | **P0** | The handshake persists the caller's resolved profile-summary + suggested SKUs onto the call row, not in process memory (stateless-turn discipline, roadmap §8). |

**Graceful-degradation rule (so this contract is never hard-blocked by O-1):** `HHT_BUDTENDER_BASE_URL` and `HHT_BACKEND_TOKEN` are *read*, never *required at import*. When budtender is unreachable or returns non-2xx, the client returns a typed empty result (`{"results": []}` / `{"pairing": None}` / `{"in_stock": False}` / `{"profile_summary": {...empty...}}`) and logs a warning — the voice turn still completes with an honest "nothing right now" instead of crashing (mirrors budtender's own no-creds → `[]` posture, research §7).

---

## 3. Auth contract (voice → budtender)

**Every endpoint except `/health/`** requires the Bearer service token. Source of truth: budtender `auth.py::ServiceTokenPermission`.

```
Authorization: Bearer <HHT_BACKEND_TOKEN>
Accept: application/json
Content-Type: application/json            # on POST
User-Agent: happytime-voice/0.1
```

- **Constant-time + fail-closed (budtender side):** `hmac.compare_digest(provided, expected)`; if `settings.HHT_BACKEND_TOKEN` is empty budtender returns 403 for everything but health (`auth.py`~L16-17). The voice client mirrors this: if `HHT_BACKEND_TOKEN` is empty it does NOT call (logs "budtender token not configured", returns the typed-empty result) — it never sends an unauthenticated request hoping it works.
- **Token discipline (ADR-019):** the token is attached ONLY by `voice/budtender_client.py` (one place). No tool handler, webhook, or template ever sees it. It is never logged (the client's request/response logging redacts the `Authorization` header — §9 + §11 H3).
- **Transport:** HTTPS only in prod (`HHT_BUDTENDER_BASE_URL` is an `https://` URL). The token never crosses a non-TLS hop.
- **No cookies / no CSRF:** this is a service-to-service Bearer boundary, distinct from the cookie-authed dashboard. budtender's DRF default permission is `ServiceTokenPermission` (token only).

**Health probe (open, no token):** `GET {BASE}/health/` → `{"status":"ok"}` (budtender `views.HealthView`, `is_public=True`). Used by `core/views.healthz` (P0) to surface budtender reachability, and by the P5 pre-warm ping (`15-P5` §3.4). A non-200 health → the dashboard shows "budtender unreachable"; suggestions degrade to graceful-empty.

---

## 4. Endpoint map — every budtender route the voice repo calls

Routes copied **verbatim** from `budtender/urls.py` (trailing slashes are load-bearing — `/products/search/` HAS a trailing slash; `/pairing/for-sku` does NOT). Each row: the voice need → the Vapi tool (if any) → the budtender route → the handler in this repo.

| Voice need | Vapi tool (P1) | Method · budtender path | budtender handler | voice handler module | §ref |
|---|---|---|---|---|---|
| **search / suggest** (ranked, in-stock, leak-safe picks) | `suggest_products` | **POST** `/api/v1/products/search/` | `ProductSearchView` | `voice/tools/suggest.py::suggest_products` | §5.2 |
| **check-by-sku** (is this SKU buyable + its price/qty) | `check_inventory` | **POST** `/api/v1/products/search/` (sku-scoped) | `ProductSearchView` | `voice/tools/suggest.py::check_inventory` | §5.3 |
| **pairing** (ONE gated upsell) | `pair_upsell` | **POST** `/api/v1/pairing/for-sku` | `PairingView` | `voice/tools/suggest.py::pair_upsell` | §5.4 |
| **facets** (slot options that exist in live stock) | *(none — server-side slot prep)* | **POST** `/api/v1/products/subtypes` · `/api/v1/products/sizes` · `/api/v1/products/price-bands` · `/api/v1/products/doh-options` | `SubtypesView`/`SizesView`/`PriceBandsView`/`DohOptionsView` | `voice/budtender_client.py::facets_*` | §5.5 |
| **customer-history** (returning-caller recognition → taste-first) | *(none — server-side handshake before suggest)* | **POST** `/api/v1/chat/resume-by-phone` | `ResumeByPhoneView` | `voice/budtender_client.py::resume_by_phone` | §5.1, §7 |
| **session persist** (carry corrected slots across stateless turns) | *(none — server-side)* | **POST** `/api/v1/chat/persist/` | `PersistView` | `voice/budtender_client.py::persist_session` | §5.7 |
| **(P4 admin) ranking-weights push** | *(dashboard, not a Vapi tool)* | **POST** `/api/v1/admin/ranking-weights` ⚠️ **NEW — TODO-A1** | *(does not exist yet)* | `dashboard/weights.py::push_to_budtender` | §5.6, §10 |
| **(P5) analytics merge** | *(dashboard, not a Vapi tool)* | **POST** `/api/v1/analytics/summary` | `AnalyticsSummaryView` | `voice/analytics.py` (P5) | §5.8 |
| **health** | *(none)* | **GET** `/api/v1/health/` | `HealthView` (open) | `voice/budtender_client.py::health` | §3 |

> **Base path:** every route is under `/api/v1/` (budtender `core/urls.py` mounts `budtender/urls.py` at `api/v1/`). The voice client builds `f"{HHT_BUDTENDER_BASE_URL}/api/v1/<path>"`.
> **Optional/by-request:** `/products/in-stock/?store=` exists (`InStockProductsView`) but the voice surface doesn't need it (it's the website's "find similar" feed); listed for completeness, NOT called by the voice repo.
> **`/track/`, `/feedback/`, `/customer/profile-upsert`, `/chat/session/start`:** available but not required by the voice flow. The voice repo records its OWN durable call log (`VoiceCall`, P0) and does NOT mirror analytics into budtender per-turn (keeps the turn fast). `profile-upsert` is a budtender-side nightly concern; the voice repo only *reads* a profile via `resume-by-phone`.

---

## 5. Per-endpoint request/response contracts (leak-safe)

> All shapes are grounded in `budtender/views.py` + `serializers.py` as they exist today. Where the research brief (`_research-suggestion-engine.md`) used a field name that does NOT match the live serializer (`price_otd`, `qty_band`, `price_tier` summary), this doc uses the **live** name and flags the gap as a budtender-side TODO so P1 does not build against a phantom field.

### 5.1 `POST /api/v1/chat/resume-by-phone` — returning-caller recognition (the personalization gate)

This is called **first** in a retail turn, before `suggest_products`, to decide margin-first vs taste-first (§6/§7).

**Request (what the voice repo sends):**
```json
{
  "phone": "+15095551234",            // see §7 + TODO-B2 for the phone-hash reconciliation
  "location": "yakima",               // yakima | mount-vernon | pullman
  "current_session_token": "vc-<vapi_call_id>"   // the voice repo's per-call session token
}
```
**Response (budtender `ResumeByPhoneView`, leak-safe — `profile_summary` is non-PII):**
```json
{
  "resumed": true,
  "session_token": "s-...",
  "stage": "RESULTS",
  "slots": { "store": "yakima", "category": "flower", "...": "..." },
  "messages": [ /* public_message shape, prior turns; usually unused by voice */ ],
  "prior_suggestions": ["SKU1","SKU2"],
  "profile_summary": { "has_history": true, "top_categories": ["flower","cartridge"], "price_tier": "mid" }
}
```
- **The ONLY field the voice flow needs:** `profile_summary.has_history`. `has_history == true` ⇒ the caller is KNOWN ⇒ subsequent `suggest_products` MUST carry the caller identity so budtender ranks taste-first (`W_KNOWN`). `has_history == false` (or the call failed) ⇒ anonymous ⇒ margin-first (`W_ANON`).
- `top_categories`/`price_tier` are spoken-safe hints the agent MAY use ("last time you grabbed flower — want to start there?"). They are non-PII (`serializers.profile_summary`~L47 — no raw history, no phone).
- **No PII leaves budtender:** the response never contains the raw phone, the purchase line items, or cost/margin. The voice repo never persists the raw phone (§7).
- **Side effects (budtender):** links the in-flight session to the customer and fires `recompute_affinity.delay(phone)` so the affinity vectors are fresh for the immediate `suggest_products` call.

### 5.2 `POST /api/v1/products/search/` — the primary recommender (`suggest_products`)

**Request:**
```json
{
  "slots": {
    "store": "yakima",                 // REQUIRED-ish; falls back to top-level "location" then "yakima"
    "category": "flower",              // flower | concentrate | cartridge | edible | tincture  (CATEGORY_BY_SLOTKEY)
    "subcategory": "rosin",            // OPTIONAL granular subtype — HARD filter when present
    "size": "3.5g",                    // OPTIONAL — gram cats "0.5g".."28g"; pre-rolls "single"/"5pk"; HARD + nearest fallback
    "price_min": 20, "price_max": 40,  // OR "price_tier": "value"|"mid"|"top"
    "effect_desired": "relaxed",       // relaxed | uplifted | middle
    "doh_only": false
  },
  "limit": 3,                          // voice cap = 3 (a phone caller can't track more)
  "location": "yakima",               // fallback if slots.store absent
  "phone": "+15095551234",            // OPTIONAL — PRESENCE is the margin-vs-taste switch (§6); omit ⇒ W_ANON
  "session_token": "vc-<vapi_call_id>",// optional; records SuggestedProduct impressions for attribution
  "exclude_skus": ["SKU_A"]           // "show me something else" support
}
```
**Response (leak-safe — `serializers.public_product`, EXACT live field set):**
```json
{
  "results": [
    {
      "rank": 1,
      "sku": "1234",
      "name": "Phat Panda — Blue Dream 3.5g",
      "brand": "Phat Panda",
      "strain": "Blue Dream",
      "price": 38.0,                    // ⚠ PRE-TAX net today — see §5.3 OTD note + TODO-B1
      "price_was": 43.0,                // or null
      "thc_percent": 27.3,
      "dominant_terpene": "Limonene",
      "stock_on_hand": 14,
      "dutchie_link": "/catalog/product/<slug>",
      "image_url": "https://...",
      "why_this": "Your go-to Phat Panda · on sale — save $5"
    }
  ],
  "source": "vps"
}
```
- **Allowlist (binding):** `PUBLIC_PRODUCT_FIELDS = (rank, sku, name, brand, strain, price, price_was, thc_percent, dominant_terpene, stock_on_hand, dutchie_link, image_url, why_this)`. **Nothing else.** `cost`/`margin`/`velocity`/`bucket`/`price_z` are physically absent (`serializers.py`~L13/L24).
- **Voice consumption:** speak `name` + `brand` + the OTD price (see §5.3) + `why_this` (verbatim-ish — it is the script). Cap at `limit:3`. Empty `results` ⇒ honest "nothing in stock for that" (never invent).
- **`why_this` is the spoken reason** (`ranking._why`~L676): personal hook → live deal → asked-for effect → real potency (≥25% THC) → scarcity (≤5 left) → terpene/strain. Built from real signals only — never fabricated.

### 5.3 `check_inventory` — sku-scoped purchasability + price (reuses `/products/search/`)

There is **no dedicated single-SKU endpoint** in budtender today. `check_inventory` is implemented as a sku-scoped `/products/search/` call: budtender's ranking already filters to `availability=True AND quantity_on_hand >= MIN_STOCK(5)` and the `_is_purchasable` gate (sales-floor stock, sellable category, has price, on live menu, fresh <60d — `dutchie.py`~L211). So "is this SKU buyable?" = "does a search constrained to it return a row?".

**Request (voice handler builds):**
```json
{ "slots": { "store": "yakima", "category": "<known category or omit>" },
  "limit": 5, "location": "yakima" }
```
Then the handler filters `results` for the target `sku` client-side (budtender's search does not accept a `sku` filter param today — see TODO-B3 for a first-class `check_inventory` endpoint). **Handler-shaped result the voice tool returns to Vapi (leak-safe):**
```json
{ "in_stock": true, "sku": "1234", "price_otd": 41.20, "stock_on_hand": 14 }
```
- `in_stock` ⇐ the SKU appears in the in-stock search results (already past `MIN_STOCK`). Absent ⇒ `{"in_stock": false}`.
- **`price_otd` is computed in the voice repo** (ADR-009) by uplifting budtender's pre-tax `price` through `compliance.tax_engine`-equivalent per-store rates — UNTIL budtender ships a native `price_otd` (TODO-B1). The voice repo owns a tiny `voice/pricing.py::otd(price, store)` for this (per-store WA excise 37% + local sales: Yakima 8.4% / Mt Vernon 8.8% / Pullman 8.9%). It NEVER speaks the raw pre-tax `price`. (This mirrors the marketing_dashboard tax-inclusive customer-facing convention; the tax math is small, deterministic, unit-tested.)
- **Leak-safe:** `price_otd`/`stock_on_hand` are derived from allowlisted fields only; no cost/margin path exists.

> **Recommended:** prefer TODO-B3 (a real `/products/check/` returning `{in_stock, price, price_otd, stock_on_hand}` for a single sku) so the voice repo stops over-fetching a full search to answer a yes/no. Until then the search-and-filter path is correct and leak-safe.

### 5.4 `POST /api/v1/pairing/for-sku` — ONE gated upsell (`pair_upsell`)

**Request:**
```json
{ "location": "yakima", "sku": "1234", "phone": "+15095551234", "session_token": "vc-<vapi_call_id>" }
```
(`slug` accepted instead of `sku`. `phone` optional — when present the pair is personalized via the caller's affinity + co-purchase history.)

**Response (`PairingView`, leak-safe — `pairing` is `public_product` shape or null):**
```json
{
  "pairing": { /* public_product shape (price pre-tax) — or null */ },
  "reason_code": "popular_pair",        // staff_pick|bought_2plus_times|bought_before_not_recent|popular_pair|your_brand|your_lane|pairs_well|none
  "reason_text": "Folks who grab flower almost always toss in a pre-roll like this — an easy add-on.",
  "strength": 0.62                      // [0,1] — the gate
}
```
- **The upsell GATE (binding, ADR-007):** the voice agent voices the upsell **only when `strength >= 0.40`** AND `pairing != null`. Below threshold (or null) → stay silent; offering nothing is correct, not a bug. The `0.40` threshold is the voice-repo policy constant (`voice/tools/suggest.py::PAIR_STRENGTH_GATE = 0.40`); research §8 recommends ~0.4.
- **Hard price gate (budtender side):** `pair.price <= 50% of anchor.price` (`MAX_PAIR_PRICE_RATIO=0.50`, `pairing.py`~L35) — a pairing is always a lighter, cheaper impulse add-on, never a second main purchase.
- **Spoken line:** read `reason_text` (built from attributes, not the product name, so it stays correct as stock rotates) + the pair's **OTD** price (uplift per §5.3). Never speak cost/margin (absent anyway).

### 5.5 Facets — slot options that exist in live stock (server-side slot prep)

Called by the budtender member's slot-fill logic (server-side, before asking the caller) so the agent only offers sizes/subtypes/price-bands/DOH that are actually fulfillable (research §6). Four POSTs, all leak-safe (no product money beyond band edges; no cost/margin):

| Path | Request | Response |
|---|---|---|
| `/api/v1/products/subtypes` | `{"slots":{"store","category"}}` | `{"subtypes": ["rosin","live resin","diamonds", ...]}` |
| `/api/v1/products/sizes` | `{"slots":{"store","category","subcategory"?}}` | `{"sizes": ["1g","3.5g","7g","28g", ...]}` (empty list ⇒ category has no size axis, skip the step) |
| `/api/v1/products/price-bands` | `{"slots":{"store","category","size"?,"subcategory"?}}` | `[{"value":"u30","label":"Under $30","min":0,"max":30}, ..., {"value":"any","label":"Any price"}]` |
| `/api/v1/products/doh-options` | `{"slots":{"store","category","size"?,"subcategory"?,"price_min"?,"price_max"?}}` | `{"doh": <bool/structured — whether DOH-only is a real choice>}` |

- **These are pre-cached** per store + inventory version (`facets.py`), so the request path does no product scan → sub-100ms. The voice repo MAY cache them per `(store, category[, subcategory, size])` for the call duration (§8).
- **price-bands** is the only one touching prices; the band edges are **pre-tax** budtender numbers. If the agent SPEAKS a band ("under thirty / thirty to fifty"), it should speak OTD-uplifted edges (or speak ranges loosely). The agent need not read bands aloud — they primarily constrain the next search's `price_min/price_max`.

### 5.6 `POST /api/v1/admin/ranking-weights` — weights push (P4 only; ⚠ NEW, TODO-A1)

Consumed by `dashboard/weights.py::push_to_budtender` (`14-P4` §4.6), NOT by a voice tool. **This endpoint does not exist in budtender yet** (TODO-A1). The frozen contract so both sides agree when it lands:
```json
// request
{ "w_anon": {"margin":0.55,"affinity":0.0,"effect":0.18,"category":0.05,"bucket":0.12,"quality":0.0,"budget":0.10},
  "w_known": {"margin":0.22,"affinity":0.34,"effect":0.10,"category":0.04,"bucket":0.12,"quality":0.14,"budget":0.04},
  "margin_emphasis": 1.0 }
// response
{ "ok": true, "applied": { /* normalized weights budtender will use */ } }
```
- **Degrade-to-local (until TODO-A1 ships):** a 404/501 ⇒ `dashboard/weights.py` keeps the weights persisted locally (`RankingWeights` singleton) and shows "saved locally; budtender sync pending" (`14-P4` §4.6). The voice suggestion path is unaffected — budtender keeps using its compiled `W_ANON`/`W_KNOWN` constants until the admin endpoint can override them.
- **Auth:** same Bearer `HHT_BACKEND_TOKEN`. This is the ONE place the voice stack touches budtender's *admin* surface.

### 5.7 `POST /api/v1/chat/persist/` — persist corrected slots across stateless turns (P5)

Used by the P5 correction handling (`15-P5` §3.3) and any multi-turn slot state, so the corrected slot state lives in budtender's session, NOT in voice process memory (roadmap §8 stateless-turn discipline).
```json
// request
{ "session_token": "vc-<vapi_call_id>", "slots": {"store":"yakima","category":"edible","...":"..."},
  "stage": "RESULTS", "phone": "+1509...", "messages": [] }
// response (202)
{ "ok": true }
```
budtender accepts `session_id` OR `session_token` (`PersistView`~L453). The voice client sends `session_token`. 202 = accepted; treat any non-2xx as a soft failure (log, continue — the next turn re-sends slots).

### 5.8 `POST /api/v1/analytics/summary` — funnel/merch counts (P5 dashboard)

Consumed by `voice/analytics.py` (P5 `15-P5` §3.6) to merge suggestion-accept-rate + merch counts into the dashboard analytics. Request `{"days":30}`; response is the large funnel object in `views.AnalyticsSummaryView` (opens/searches/conversions/engagement/top_suggested_products/...). **Leak-safe:** it returns counts + product names/SKUs only (no cost/margin). The voice repo reads only the funnel/accept-rate fields it needs; AC-7 of P5 asserts no cost/margin substring in the merged output.

---

## 6. Margin-first vs taste-first selection (which params trigger `W_ANON` vs `W_KNOWN`)

**The single switch is the presence of a resolvable caller identity on the `suggest_products`/`pairing` request.** Source of truth: `ProductSearchView` (`views.py`~L126-132) → `W = W_KNOWN if profile else W_ANON` (`ranking.py` chooses by `profile`).

| Caller state | What the voice repo sends to `/products/search/` | budtender resolves a `CustomerProfile`? | Weight set | Behavior |
|---|---|---|---|---|
| **UNKNOWN** (anonymous, no history, or recognition skipped/failed) | `slots` **without** `phone`, **without** a customer-linked `session_token` | No | **`W_ANON`** (margin 0.55) | **HIGH MARGIN FIRST** (owner emphasis). Slot #1 is also pinned to the highest gross-margin $ item; affinity/quality terms are 0. |
| **KNOWN** (returning caller; `resume-by-phone` returned `has_history:true`) | `slots` **with** `phone` (the caller number) OR a `session_token` already linked to the profile | Yes | **`W_KNOWN`** (affinity 0.34, margin 0.22) | **TASTE-FIRST** from real Dutchie history. Slot #1 stays the margin pin (margin never fully abandoned); slots #3+ reshuffle to taste; `why_this` gains personal hooks. |

**Precise rules the voice repo follows:**
1. **Resolve identity first.** In a retail turn, before the first `suggest_products`, the budtender member calls the handshake (§7): hash the caller number, `resume-by-phone`, read `profile_summary.has_history`.
2. **KNOWN ⇒ pass identity.** If `has_history:true`, include the caller `phone` (or the linked `session_token`) on every subsequent `suggest_products`/`pairing` call so budtender ranks `W_KNOWN`.
3. **UNKNOWN ⇒ omit identity.** If `has_history:false` (or the handshake failed/was skipped), send NO `phone` and NO customer-linked `session_token` → budtender uses `W_ANON` → margin-first. This is the **default** and is the owner's intended behavior for a first-time/anonymous caller (maximize margin).
4. **Never re-rank in the voice repo.** The voice repo consumes the order budtender returns (ADR-005). It does not reorder, re-weight, or second-guess. The "margin lever" is entirely server-side (the weight choice + the slot-#1 margin pin + the slot-#2 velocity pin live in `rank_products`).
5. **`margin_emphasis` (P4 lever)** only affects `W_ANON` (the anonymous margin term) and is applied **inside budtender** once TODO-A1 ships; the voice repo never multiplies a margin term itself (it has no margin field to multiply — leak-safe).

> **Edge:** a known caller who explicitly says "just show me your best deals / I don't care about my usual" — the agent MAY drop the identity on that one search to force `W_ANON` (margin-first). This is an explicit caller-driven override, logged on the `VoiceCall`; default remains taste-first for a recognized caller.

---

## 7. Returning-caller personalization handshake (phone-hash → lookup → history → affinity)

End-to-end, exactly as the voice repo performs it. This is the mechanism behind §6's KNOWN branch and honors ADR-006 (peppered phone-hash) + the PII discipline (raw caller numbers never persisted in the voice repo).

```
1. Vapi delivers the caller number on the call (assistant-request / call.customer.number).
2. voice repo computes the PEPPERED phone-hash:
       phone_hash = sha256( PHONE_HASH_PEPPER + e164_normalize(caller_number) )
   (PHONE_HASH_PEPPER != DJANGO_SECRET_KEY — prod-fail-closed checks this, 03-CONVENTIONS §3.9.)
   The voice repo stores ONLY phone_hash on crm/models.Caller + VoiceCall (never the raw number).
3. voice repo calls budtender  POST /chat/resume-by-phone  (§5.1).
       ⚠ See TODO-B2: budtender keys CustomerProfile on a NORMALIZED RAW phone (+1XXXXXXXXXX),
         not on the voice repo's peppered hash. Resolution below.
4. budtender resolves CustomerProfile (built nightly from Dutchie /reporting/transactions
       via dutchie.get_transactions_detailed + customerId→cellPhone via get_customers),
       fires recompute_affinity, returns profile_summary {has_history, top_categories, price_tier}.
5. voice repo reads has_history:
       has_history == true  → KNOWN  → pass caller identity on suggest/pairing → W_KNOWN (taste-first)
       has_history == false → UNKNOWN → omit identity → W_ANON (margin-first)
6. voice repo persists profile_summary + the suggested SKUs onto the VoiceCall row (stateless-turn
   discipline — state in VoiceCall/budtender, never process memory). The raw number is NOT persisted.
```

### 7.1 The phone-hash vs raw-phone reconciliation (a real boundary mismatch — resolve, don't paper over)

`01-ARCHITECTURE.md` §1.2 says "phone-hash → budtender `chat/resume-by-phone`". But budtender today keys `CustomerProfile` on a **normalized raw phone** (`tasks._normalize_phone` → `+1XXXXXXXXXX`), and only sha256-hashes the phone in `track/`/`feedback/` analytics — `resume-by-phone` matches `CustomerProfile.objects.filter(phone=_normalize_phone(phone))` on the **raw** value (`views.ResumeByPhoneView`~L409). A *peppered-hash* sent as `phone` would never match a profile keyed on the raw number → every caller would look UNKNOWN.

**Resolution (binding for P1):**
- **Default (correct + minimal):** the voice repo sends budtender the **E.164-normalized raw caller number** (`+1XXXXXXXXXX`) as `phone` on `resume-by-phone`/`suggest`/`pairing`, because that is the key budtender resolves a profile by. This crosses ONE trusted server-to-server TLS hop to a service that already holds the full Dutchie purchase history (and the customers' raw `cellPhone`s) — so the raw number is not "new" PII to budtender. The voice repo still **persists only the peppered `phone_hash`** in its OWN DB (`crm/models.Caller`, `VoiceCall`) — the raw number is used transiently in-request and never written to the voice repo's storage. This satisfies ADR-006's intent (voice-repo PII discipline) while matching budtender's real key.
- **Hardened alternative (TODO-B2):** add a budtender endpoint `POST /chat/resume-by-phone-hash` that accepts the SAME peppered hash the voice repo computes (budtender stores a `phone_hash` column alongside `phone`, populated in the nightly `sync_transactions` using the shared `PHONE_HASH_PEPPER`). Then NO raw number ever crosses the boundary. This is the privacy-maximal design; it is a budtender-side change (shared pepper + a hash column + the endpoint) and is **not required for P1** — ship the default now, flip to the hash endpoint when budtender adopts it.
- **The contract test (§12) pins the default:** `resume_by_phone(raw_e164)` is what `budtender_client` sends; a follow-up test guards that the voice repo's OWN DB rows contain only the hash, never the raw number.

### 7.2 Unknown-number / blocked-caller-ID path
If Vapi delivers no caller number (blocked/anonymous), the handshake is skipped, `has_history` is treated as `false`, and the call is margin-first (`W_ANON`) — the correct default. The agent simply does not personalize. No error.

---

## 8. Timeouts, caching, latency budget (one voice turn)

A voice turn is latency-critical: the caller is on the phone and Vapi is waiting on the tool webhook before it can speak. budtender ranks over a **pre-synced per-store `Product` table** (not a live Dutchie call — research §7, `01-ARCHITECTURE.md` §3), so the suggestion call itself is fast; the budget protects against a cold/slow/unreachable budtender.

### 8.1 The per-turn budget (server-side handler time the voice repo owns)
| Segment | Target | Note |
|---|---|---|
| **Whole `/api/voice/vapi` tool handler** (incl. the budtender hop) | **p95 ≤ 1500 ms** | The P5 target (`15-P5` §3.4, `_p5-latency-budget.md`). Vapi owns LLM/TTS turn latency separately. |
| budtender `/products/search/` round-trip | typically ≤ 400 ms | ranks over the synced table; the freshness self-heal is async (never blocks). |
| budtender facets (`subtypes`/`sizes`/`price-bands`/`doh`) | ≤ 100 ms each | pre-cached per inventory version. |
| `resume-by-phone` round-trip | ≤ 400 ms | profile lookup + an async `recompute_affinity` (does not block the response). |

### 8.2 Timeouts (client side)
- **`HHT_BUDTENDER_TIMEOUT`** env (default `8` per `03-CONVENTIONS.md` §3.4) → **tightened to 3–4 s for voice** (`15-P5` §3.4). A single tool call must not hold the turn longer than the budget.
- **Per-call hard timeout:** the client uses a connect timeout (≈2 s) + a read timeout (`HHT_BUDTENDER_TIMEOUT`). On timeout → **fast graceful-empty**: `suggest`→`{"results":[]}`, `pairing`→`{"pairing":None,"strength":0.0}`, `check`→`{"in_stock":False}`, `resume`→`{"profile_summary":{"has_history":False,...}}` — logged as a warning, **never raised into the turn** (research §7; `15-P5` AC-4).

### 8.3 Caching
- **Connection reuse (binding):** `voice/budtender_client.py` uses a single pooled `requests.Session`/`httpx.Client` (keep-alive) for the process — NOT a fresh connection per tool call (`15-P5` §3.4). Saves TLS handshake on every turn.
- **Facets per-call cache:** facet responses are stable within a call; cache them in the `VoiceCall`/turn context keyed by `(store, category[, subcategory, size])` so the slot-fill doesn't re-fetch sizes/subtypes it already has.
- **Do NOT cache `/products/search/` results across turns** — inventory changes; the caller's "show me something else" relies on a fresh ranked set (with `exclude_skus`). Each suggest is a fresh call.
- **Pre-warm (P5):** a startup/health ping from the client warms the connection pool so the first real call isn't a cold connect (`15-P5` §3.4). budtender's own inventory sync (every 10 min) keeps its table warm independent of the voice repo.
- **budtender self-heal is async:** when budtender sees its inventory is ≥24h stale it fires an async refresh and still answers from the current table (`views.ProductSearchView`~L137) — the voice turn is never blocked by a sync.

---

## 9. `voice/budtender_client.py` — method list (the thin Bearer client P1 builds)

A single class `BudtenderClient` (or module-level functions) — the ONLY place the Bearer token is attached and the ONLY place that knows budtender's base URL. Thin: build URL, attach auth header, POST/GET JSON, parse, graceful-empty on any failure, redact the auth header in logs. No ranking, no Dutchie, no business logic (ADR-004).

```python
class BudtenderClient:
    def __init__(self, base_url=settings.HHT_BUDTENDER_BASE_URL,
                 token=settings.HHT_BACKEND_TOKEN,
                 timeout=settings.HHT_BUDTENDER_TIMEOUT):
        self._session = requests.Session()   # pooled / keep-alive (§8.3)

    # ── health ──
    def health(self) -> dict: ...                          # GET  /health/  → {"status":"ok"} | {} on failure

    # ── suggestions (the data plane) ──
    def search(self, slots: dict, *, limit: int = 3, phone: str | None = None,
               session_token: str | None = None, exclude_skus: list[str] | None = None,
               location: str | None = None) -> dict: ...    # POST /products/search/ → {"results":[...≤3 leak-safe...]}; {"results":[]} on failure
    def check_sku(self, store: str, sku: str,
                  category: str | None = None) -> dict: ...  # search-and-filter → {"in_stock":bool,"sku","price_otd","stock_on_hand"} (§5.3; price_otd via voice/pricing.otd)
    def pair_for_sku(self, store: str, anchor_sku: str, *, phone: str | None = None,
                     session_token: str | None = None) -> dict: ...  # POST /pairing/for-sku → {"pairing","reason_code","reason_text","strength"}; null-pair on failure

    # ── facets (slot prep) ──
    def facets_subtypes(self, store: str, category: str) -> list[str]: ...     # POST /products/subtypes
    def facets_sizes(self, store: str, category: str, subcategory: str | None = None) -> list[str]: ...  # POST /products/sizes
    def facets_price_bands(self, store: str, category: str, size: str | None = None,
                           subcategory: str | None = None) -> list[dict]: ...  # POST /products/price-bands
    def facets_doh(self, store: str, category: str, **filters) -> dict: ...    # POST /products/doh-options

    # ── returning-caller handshake (§7) ──
    def resume_by_phone(self, phone_e164: str, *, location: str | None = None,
                        current_session_token: str | None = None) -> dict: ...  # POST /chat/resume-by-phone → {"resumed","profile_summary":{...}}
    def persist_session(self, session_token: str, *, slots: dict | None = None,
                        stage: str | None = None, phone: str | None = None,
                        messages: list | None = None) -> dict: ...  # POST /chat/persist/ → {"ok":true} (202)

    # ── P4 admin (weights) — uses TODO-A1 endpoint; degrades to {"ok":False} ──
    def push_ranking_weights(self, w_anon: dict, w_known: dict,
                             margin_emphasis: float) -> dict: ...  # POST /admin/ranking-weights

    # ── P5 analytics merge ──
    def analytics_summary(self, days: int = 30) -> dict: ...        # POST /analytics/summary
```

**Cross-cutting client invariants (binding):**
- **Auth header attached here only**, redacted in any log line (`Authorization: Bearer ***`). Never logs the token, never logs a raw phone.
- **Graceful-empty on every method** (§8.2): a connect/read timeout or non-2xx returns the typed-empty result + a logged warning; never raises into the voice turn.
- **`check_sku` computes `price_otd`** via `voice/pricing.otd(price, store)` (ADR-009) — the raw pre-tax `price` is never returned to the tool layer for speaking.
- **No re-ranking, no Dutchie, no margin math** (ADR-004/008). The client is a transport.
- **Tool handlers** (`voice/tools/suggest.py`) call these methods and shape the result for Vapi — they own the `PAIR_STRENGTH_GATE=0.40` decision and the `limit=3` cap, not the client.

---

## 10. NEW endpoints budtender must add (budtender-side TODOs — none block P1)

These are **budtender-repo** follow-ups (a separate service, ADR-004). P1 ships against the contract above with the documented fallbacks; flip each on when budtender lands it.

| TODO | Endpoint / change (budtender repo) | Why | Voice-repo fallback until it lands | Consumed by |
|---|---|---|---|---|
| **TODO-A1** | `POST /api/v1/admin/ranking-weights` (write `W_ANON`/`W_KNOWN`/`margin_emphasis` overrides; Bearer-auth; normalize + return `applied`) | The P4 dashboard weights-tuner needs a way to push the owner's "more margin" lever to the engine (`14-P4` §4.6). | `dashboard/weights.py` persists locally + shows "sync pending"; budtender keeps its compiled `W_ANON`/`W_KNOWN`. | P4 dashboard |
| **TODO-B1** | Add **`price_otd`** to `PUBLIC_PRODUCT_FIELDS` (out-the-door, tax-included per store) in `serializers.public_product` — so the voice repo speaks budtender's authoritative OTD instead of re-deriving tax. | ADR-009 (speak OTD). Today `public_product` returns only the pre-tax `price`; the voice repo must uplift it itself (§5.3), duplicating tax logic that budtender could own once (it already knows the store + price). | `voice/pricing.otd(price, store)` uplifts the pre-tax `price` (WA excise 37% + per-store local). Deterministic, unit-tested. **Leak-safe — adding `price_otd` adds NO cost/margin.** | `suggest_products`, `check_inventory`, `pair_upsell` |
| **TODO-B2** | `POST /api/v1/chat/resume-by-phone-hash` accepting the voice repo's **peppered phone-hash** (budtender stores a `phone_hash` column populated in `sync_transactions` with the shared `PHONE_HASH_PEPPER`). | Privacy-maximal returning-caller recognition — NO raw number crosses the boundary (§7.1). | Default: send the E.164 **raw** number (the key budtender resolves by today); persist only the hash in the voice repo's own DB. | returning-caller handshake (§7) |
| **TODO-B3** | `POST /api/v1/products/check/` returning `{in_stock, sku, price, price_otd, stock_on_hand}` for ONE sku. | `check_inventory` currently over-fetches a full ranked search and filters client-side (§5.3) — a dedicated yes/no endpoint is cheaper + clearer. | search-and-filter via `/products/search/` (correct + leak-safe, just heavier). | `check_inventory` |
| **TODO-B4** *(nice-to-have)* | Reserve/confirm `channel:"voice"` attribution on `SuggestedProduct`/`AnalyticsEvent` writes triggered by voice `session_token`s. | Clean per-channel analytics (budtender already reserves `channel="voice"` — research §0). | The voice repo records its own `VoiceCall` outcomes; budtender attribution is a reporting nicety. | P5 analytics |

**Explicitly NOT a budtender change:** ranking weights/order, the leak-safe serializer, the pairing gate, facets, the `resume-by-phone` profile_summary shape — all reused **unchanged** (ADR-004). The voice repo adapts to budtender, not the reverse, except for the four additive TODOs above.

---

## 11. Acceptance criteria (concrete, testable, lettered)

**A. Auth + reachability**
- A1. `BudtenderClient` attaches `Authorization: Bearer <HHT_BACKEND_TOKEN>` on every non-health request; `GET /health/` carries NO token and returns `{"status":"ok"}` against a live/stub budtender.
- A2. With `HHT_BACKEND_TOKEN` empty, the client does NOT issue a request — it returns the typed-empty result and logs "budtender token not configured" (fail-closed, mirrors budtender `auth.py`).
- A3. A non-2xx / connection error from budtender on any method returns the method's typed-empty result (`{"results":[]}` / null-pair / `{"in_stock":false}` / empty `profile_summary`) and a logged warning — never raises into the caller.

**B. Endpoint contracts (shape-pinned)**
- B1. `search(slots, limit=3)` POSTs to `/api/v1/products/search/` (trailing slash) with the exact body of §5.2 and returns `results` of ≤3 items, each containing EXACTLY the `PUBLIC_PRODUCT_FIELDS` keys (no extra keys).
- B2. `pair_for_sku(store, sku)` POSTs to `/api/v1/pairing/for-sku` (NO trailing slash) and returns `{pairing, reason_code, reason_text, strength}`; `pairing` is either the `public_product` shape or `null`.
- B3. `resume_by_phone(e164)` returns `profile_summary` with keys `{has_history, top_categories, price_tier}` and nothing else identity-bearing (no raw phone, no line items).
- B4. Facet methods return the documented shapes (`subtypes`→list[str], `sizes`→list[str], `price-bands`→list[dict], `doh`→dict); an empty `sizes` list is handled (the slot step is skipped, not errored).

**C. Margin-first vs taste-first selection (§6)**
- C1. When the caller is UNKNOWN (no `phone`, no linked `session_token`), `search` sends NO `phone` field ⇒ budtender ranks `W_ANON` (margin-first). Asserted by the absence of `phone` in the request body.
- C2. When the caller is KNOWN (`resume_by_phone` returned `has_history:true`), the budtender member's subsequent `search`/`pair` calls INCLUDE the caller identity ⇒ `W_KNOWN` (taste-first). Asserted by the presence of `phone` (or the linked `session_token`) in the request body.
- C3. The voice repo NEVER re-orders `results` — the order returned by budtender is the order spoken (no client-side sort).

**D. Returning-caller handshake (§7)**
- D1. The voice repo persists only the **peppered phone-hash** in its own DB (`crm/models.Caller`/`VoiceCall`); a test greps the voice DB rows and asserts the raw caller number string is absent.
- D2. `PHONE_HASH_PEPPER != DJANGO_SECRET_KEY` is enforced at boot (prod-fail-closed) — a test sets them equal and asserts the app refuses to start.
- D3. A blocked/absent caller number ⇒ handshake skipped ⇒ margin-first; no error.

**E. OTD pricing (ADR-009)**
- E1. Any price the tool layer exposes for speaking is the **OTD** value (`price_otd`), never budtender's raw pre-tax `price`. A test asserts `check_sku`/the suggest shaper emit `price_otd` and that `price_otd >= price` for a positive price.
- E2. `voice/pricing.otd(price, store)` is deterministic per store (Yakima/Mt Vernon/Pullman/Combined) and unit-tested against hand-authored expected values.

**F. Upsell gate (ADR-007)**
- F1. `pair_upsell` voices a pairing ONLY when `strength >= 0.40` AND `pairing != null`; below threshold or null ⇒ the tool returns "no upsell" and the agent stays silent.

**G. Latency / resilience (§8)**
- G1. The client uses ONE pooled session (keep-alive) — asserted by a test that two calls reuse the same underlying connection/session object.
- G2. A simulated budtender timeout returns the graceful-empty result within `HHT_BUDTENDER_TIMEOUT` and logs a warning; the turn still completes (no exception).

**H. Leak-safety + secret hygiene (NON-NEGOTIABLE)**
- H1. **No-leak (mandatory gate):** no `"cost"` or `"margin"` substring appears in ANY response body the voice repo handles from budtender — `search`, `check_sku`, `pair_for_sku`, `resume_by_phone`, facets, `analytics_summary`. (Reuse budtender's `tests/test_no_leak.py` philosophy on the voice side against recorded fixtures.)
- H2. The Bearer token never appears in any log line, exception message, or the `VoiceCall` record (the client redacts `Authorization`).
- H3. The raw caller phone never appears in any log line or the voice repo's persisted rows (only the peppered hash).

---

## 12. Test plan

Mirrors `03-CONVENTIONS.md` §5 planes. The boundary this doc owns is **Contract** + a slice of **Unit**; the Leak-Guard and timeout/auth tests are the mandatory gates.

### 12.1 Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network)
- `tests/test_pricing_otd.py` — `voice/pricing.otd(price, store)` per store == hand-authored expected (E1/E2); monotonic (`otd >= price`); guards a $0/negative price.
- `tests/test_budtender_client_buildreq.py` — request-body builders: `search` includes/omits `phone` correctly per known/unknown (C1/C2); correct path + trailing-slash per method (B1/B2); `limit` defaults to 3; `exclude_skus` forwarded.
- `tests/test_phone_hash.py` — peppered hash is stable + `PHONE_HASH_PEPPER`-dependent; `== DJANGO_SECRET_KEY` ⇒ boot refusal (D2); blocked number ⇒ skip path (D3).
- `tests/test_pair_gate.py` — `PAIR_STRENGTH_GATE=0.40`: strength 0.39 ⇒ silent, 0.40/0.62 ⇒ voiced, null pair ⇒ silent (F1).

### 12.2 Contract (`pytest -m integration`, budtender stubbed/recorded — Vapi not involved here)
- `tests/test_budtender_contract_shapes.py` — against a recorded/stubbed budtender, assert the exact response shapes of §5.1–§5.5 (B1–B4); assert `results` items carry ONLY `PUBLIC_PRODUCT_FIELDS` keys (no extras).
- `tests/test_no_leak_budtender.py` (**mandatory gate**) — for every client method, assert no `"cost"`/`"margin"` substring in the raw response body AND in the tool-shaped output (H1). Use fixtures with the full `public_product` shape.
- `tests/test_auth_failclosed.py` — empty `HHT_BACKEND_TOKEN` ⇒ no request issued, typed-empty returned (A2); non-2xx ⇒ typed-empty + warning (A3); health needs no token (A1).
- `tests/test_timeout_graceful.py` — a stubbed slow budtender (sleep > timeout) ⇒ graceful-empty within budget, warning logged, no raise (G2); pooled session reused across two calls (G1).
- `tests/test_selection_switch.py` — drive the known/unknown branch: stub `resume_by_phone` to return `has_history:true|false`, assert the subsequent `search` body includes/omits `phone` accordingly (C1/C2); assert the voice repo does not reorder a fixed `results` list (C3).
- `tests/test_handshake_pii.py` — after a full handshake, the voice DB rows contain only the peppered hash, never the raw number (D1/H3); the token never appears in logs (H2).
- `tests/test_weights_push_degrade.py` (P4 surface) — `push_ranking_weights` against a 404 stub ⇒ `{"ok":false}` + local-persist path (TODO-A1 fallback).

### 12.3 Manual call slice (the data-plane proof, folded into P1's manual script)
On a real inbound call (Dial `VAPI_PHONE_NUMBER_ID`), paste transcript + the `VoiceCall` row + the recorded tool args:
1. **Anonymous suggestion:** "recommend an indica for sleep under $40" with an UNRECOGNIZED number → the `suggest_products` tool args carry NO `phone`; ≤3 in-stock picks spoken with `why_this` + OTD price; one upsell only if `strength≥0.40`. Confirm in the budtender logs that `W_ANON` was used (margin-first). **No `cost`/`margin` anywhere.**
2. **Returning caller:** call from a number with Dutchie history → `resume_by_phone` returns `has_history:true`; the next `suggest_products` carries the caller identity; picks reflect taste (personal `why_this` hooks); slot #1 still margin-pinned. The voice DB row stores only the hash.
3. **Empty-stock honesty:** a filter with no in-stock match → `results:[]` → the agent says "nothing in stock for that right now" and offers an alternative — never invents a SKU/price.

**Test-data discipline:** deterministic recorded budtender fixtures; expected values hand-authored, not generated by the code under test. The **no-leak** test (12.2) and the **auth-fail-closed/timeout** tests are non-negotiable gates on any change touching `voice/budtender_client.py` or the suggest tools.

---

## 13. Risks / open questions

| Risk / open item | Impact | Mitigation / disposition |
|---|---|---|
| **`price` vs `price_otd` drift** — budtender returns pre-tax `price`; the agent must speak OTD. | If the agent spoke the raw `price`, it would quote under-tax. | The voice repo uplifts to OTD in `voice/pricing.otd` and NEVER exposes the raw `price` for speaking (§5.3, E1). TODO-B1 moves this into budtender once. Both leak-safe (no cost/margin). |
| **phone-hash vs raw-phone key mismatch** (§7.1) — budtender resolves a profile by the normalized RAW phone; the voice repo's PII rule says "store only the hash". | A peppered hash sent as `phone` would never match → every caller looks anonymous (taste-first never fires). | Default: send the E.164 raw number on the request (budtender's real key) but persist only the hash in the voice DB. TODO-B2 adds a hash endpoint for the privacy-maximal design. Pinned by the contract test. |
| **budtender unreachable / no Dutchie creds (O-1 placeholder)** | No suggestions / no personalization. | Graceful-empty everywhere (§2, §8.2); the turn completes with an honest "nothing right now"; flip the base URL/keys when owner confirms. The client ships against the contract + a stub. |
| **Latency under load** — a cold/slow budtender stalls the turn. | Caller hears dead air. | Tight timeout (3–4 s), pooled keep-alive session, fast graceful-empty, P5 pre-warm ping; budtender ranks over a pre-synced table (not live Dutchie) so the happy path is sub-400ms (§8). |
| **`check_inventory` over-fetch** — a yes/no answered by a full search. | Slightly heavier turn. | Acceptable + leak-safe now; TODO-B3 adds a dedicated single-sku endpoint. |
| **Admin weights endpoint absent (TODO-A1)** — the owner's margin lever can't reach the engine yet. | "More margin" toggle is local-only. | Degrade-to-local (§5.6); budtender keeps its compiled weights; flip when TODO-A1 ships. Suggestion path unaffected. |
| **OTD tax rates duplicated** in `voice/pricing.otd` vs budtender vs marketing_dashboard. | Drift risk on a rate change. | Keep `voice/pricing.otd` a tiny, single-source-of-truth module with the per-store rates as named constants + a unit test; collapse into budtender via TODO-B1 (one place) when adopted. |
| **`strength` gate threshold (0.40)** is a judgment call. | Too many / too few upsells. | Make it a single named constant (`PAIR_STRENGTH_GATE`) tunable later; research §8 recommends ~0.4; revisit with call data (P5 analytics). |
| **Open — does budtender ever speak a price band aloud?** | Minor UX. | Default: bands constrain the next search's `price_min/max`; if spoken, uplift edges to OTD. Confirm with owner during P1 manual call review. |

---

## 14. Documentation protocol (close-out — binding, `03-CONVENTIONS.md` §6)

On completing/using this contract, **in the same change:** if any budtender TODO (A1/B1–B4) lands, update §5/§10 here, bump the status line, and record the flip in `02-DECISIONS.md` (an ADR if it changes the boundary — e.g. "ADR: OTD owned by budtender via `price_otd`"); note the `voice/budtender_client.py` method set + `voice/pricing.otd` in `01-ARCHITECTURE.md` §8; append to `brain/Daily/`. The phone-hash reconciliation (§7.1) is the one boundary decision most likely to need an ADR — if TODO-B2 is adopted, log it. **No task completes without docs updated.**

---

## 15. Source-file anchors (for the executor)

- **budtender (the service this contract speaks to):** `C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender\budtender\` — `urls.py` (routes), `views.py` (`ProductSearchView`~L109, `PairingView`~L377, `ResumeByPhoneView`~L407, `PersistView`~L450, `AnalyticsSummaryView`~L211, `HealthView`~L91), `serializers.py` (`PUBLIC_PRODUCT_FIELDS`~L13, `public_product`~L24, `profile_summary`~L47), `ranking.py` (`W_ANON`~L14, `W_KNOWN`~L15, `MIN_STOCK`~L24, `CATEGORY_BY_SLOTKEY`~L32, `rank_products`~L465, `_why`~L676), `pairing.py` (`MAX_PAIR_PRICE_RATIO`~L35, `pair_for`~L122, strength ~L195), `auth.py` (`ServiceTokenPermission`~L8, `compare_digest`~L23), `dutchie.py` (`_is_purchasable`~L211, `get_customers`~L305, `get_transactions_detailed`~L351).
- **swedish-bot (phone-hash port):** `C:\Users\vladi\OneDrive\Desktop\swedish-bot\crm\models.py` + `crm/profile.py` (`PHONE_HASH_PEPPER`).
- **Foundation:** `C:\happytime-voice\docs\plans\{00-MASTER-ROADMAP,01-ARCHITECTURE,02-DECISIONS,03-CONVENTIONS}.md`.
- **Research:** `C:\happytime-voice\docs\plans\_research-suggestion-engine.md` (the engine teardown — §2 weights, §5 HTTP contract, §7 freshness), `_research-education-blogs.md` (taxonomy parity for slot vocabulary).
- **Consuming phase docs:** `11-P1-DUTCHIE-SUGGESTIONS.md` (builds `budtender_client.py` + the 3 tool handlers against this contract), `14-P4-dashboard-publish.md` §4.6 (TODO-A1 weights push), `15-P5-polish-brand.md` §3.2/§3.4/§3.6 (cartridge forward, latency budget, `/analytics/summary`).
