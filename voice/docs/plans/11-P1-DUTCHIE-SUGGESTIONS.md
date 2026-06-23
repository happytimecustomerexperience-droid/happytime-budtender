# 11 — P1 — DUTCHIE SUGGESTION PATH — Executable Plan

> **Status:** DONE (built + GREEN, 2026-06-22). The budtender HTTP client, the three bound tools
> (`suggest_products`/`check_inventory`/`pair_upsell`), returning-caller recognition (peppered
> phone-hash → budtender, ADR-022 Option A), OTD pricing, the leak-safe `_speakable_pick` wall, and
> the `budtender` + `entry_router` Squad members + the `entry_router →(retail)→ budtender` transition
> are shipped on `p1/dutchie-suggest`. budtender HTTP is fully MOCKED in tests (offline, no keys):
> 66 P1 tests + the full 124-test suite GREEN; ruff + `makemigrations --check` (exit 0) clean;
> provisioning verified create→zero-drift with the squad edge resolving. The no-leak contract test
> (`tests/test_leak_guard.py`) + HMAC-fail-closed on the tool path are the non-negotiable gates and
> both pass. Live flip awaits O-1 (`HHT_BUDTENDER_BASE_URL` + matching `HHT_BACKEND_TOKEN`); the
> graceful-empty path keeps the agent honest until then. Written 2026-06-22.
> **Subsystem:** S2 (Dutchie suggestions). **Capability:** C3 (Dutchie inventory + product suggestion) from the synthesis brief §3.
> **Implements / honors (binding, never contradicted here):** ADR-004 (budtender is a separate HTTP microservice; voice repo never re-implements Dutchie/ranking), ADR-005 (margin-first `W_ANON` when UNKNOWN / taste-first `W_KNOWN` when KNOWN), ADR-006 (returning-caller recognition via swedish-bot's peppered phone-hash), ADR-007 (ONE gated `pairing.py` upsell), ADR-008 (leak-safe allowlist — cost/margin can NEVER be spoken), ADR-009 (speak OUT-THE-DOOR prices), ADR-010 (gpt-4.1-mini assistants), ADR-011 (voice/persona set ONCE per member), ADR-019 (HMAC fail-closed; per-store Dutchie keys ONLY in budtender), ADR-020 (`voice/tools/` package + registry).
> **Read order before executing (mandatory):** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → `_research-suggestion-engine.md` → `_research-education-blogs.md` → **this file**.
> **Ports / consumes from:** `happytime-budtender` (`ranking.py`, `pairing.py`, `serializers.py`, `auth.py`, `urls.py`, `views.py` — REUSED UNCHANGED over HTTP, never copied), `swedish-bot` (`crm/models.py::phone_hash` peppered-hash pattern). **The voice repo writes only the thin client + tool handlers + the budtender Squad member.**
>
> **One-line goal:** a real inbound call — *"recommend an indica for sleep under $40"* — gets ≤3 **real, in-stock, leak-safe** picks spoken with a `why_this` line + exactly one gated upsell, at the **out-the-door** price; an **anonymous** caller is ranked **high-margin first** (`W_ANON`), a **recognized returning caller** (phone-hash → budtender purchase history) is ranked **taste-first** (`W_KNOWN`). This fixes the export's #1 (tools are prose, never bound) and #2 (no Dutchie wiring) — the core production blocker.

---

## 1. Goal & scope

### 1.1 In scope (this phase ships all of)

1. **The budtender HTTP client** (`voice/budtender_client.py`) — a thin, Bearer-authed, pooled, per-method, timeout-bounded, fail-graceful HTTP client to the **happytime-budtender** microservice (ADR-004). One method per data-plane endpoint P1 needs: `search_products`, `check_inventory`, `pair_for_sku`, `resume_by_phone`, plus the facet helpers (`subtypes`/`sizes`/`price_bands`/`doh_options`) for slot-filling and a `health` ping. It NEVER re-implements ranking, pairing, Dutchie access, or the leak-safe serializer — it only calls budtender and returns budtender's already-leak-safe JSON.
2. **Three Vapi custom tools, fully bound** (the export's #1 fix) with complete arg JSON schemas + handlers in `voice/tools/suggest.py`, registered in the P0 `TOOL_REGISTRY` (ADR-020):
   - `suggest_products` — slot-filled product recommendation → budtender `POST /api/v1/products/search/` → ≤3 leak-safe picks each with a speakable `why_this`.
   - `check_inventory` — purchasability/stock/OTD-price check for a specific SKU → budtender stock gate.
   - `pair_upsell` — ONE complementary add-on by anchor SKU → budtender `POST /api/v1/pairing/for-sku`, surfaced only when `strength` clears a gate (ADR-007).
3. **Returning-caller recognition** (ADR-006) — the caller's number → swedish-bot's **peppered phone-hash** → a budtender profile lookup (`POST /api/v1/chat/resume-by-phone`). A hit → `session_token` + non-PII `profile_summary` carried into `suggest_products`/`pair_upsell` so budtender ranks with `W_KNOWN` (taste-first); a miss → anonymous → `W_ANON` (margin-first). **Raw caller numbers are never persisted** (PII discipline).
4. **The `budtender` Squad member** — split off `entry_router` (the retail brain), with a focused slot-filling system prompt (effect → activity → preferences → past-wins → explore → budget → select → quantity/upsell), the three tools attached by `toolIds`, voice/transcriber/model set ONCE (ADR-011), and the squad transition `entry_router →(retail intent)→ budtender`.
5. **The speakable contract** — every pick carries a `why_this` line (built server-side by budtender's `_why()` from real signals only); the agent speaks it verbatim-ish. Prices spoken are **OUT-THE-DOOR** (`price_otd`, ADR-009).
6. **The leak-safety guarantee + a no-leak contract test** — budtender's `public_product` allowlist serializer means `cost`/`margin`/`velocity`/`bucket` physically never reach the voice repo; P1 adds a **defensive voice-repo contract test** asserting no `"cost"`/`"margin"` substring appears in any tool response (ADR-008) — so the agent is structurally incapable of speaking them.

### 1.2 Out of scope (other phases / EXP)

- **Cartridge entry directly from `entry_router`** (export #4) — **P5** (`15-P5-polish-brand.md` §3.2). P1 supports `category:"cartridge"` as a `suggest_products` arg value (it's in budtender's category enum), but the *router-level up-front cartridge classification* is P5.
- **Back-edge / mid-flow correction** ("actually make it edibles") — **P5** (`15-P5` §3.3). P1's budtender member slot-fills forward; the correction-reset FSM is P5.
- **The dashboard ranking-weights tuner + Publish-to-Vapi** — **P4** (`14-P4-dashboard-publish.md`). P1 consumes budtender's *default* `W_ANON`/`W_KNOWN`; the owner-editable weights surface is P4.
- **Escalation/transfer, vendor routing, staff email, the durable-record email sink** — **P2/P3**. P1 writes the `VoiceCall` outcome (`suggested` + the SKUs) into the durable log that P0 created and P2/P3 enrich; it does not own the email path.
- **Re-implementing Dutchie, ranking, pairing, facets, or the serializer** — NEVER (ADR-004). budtender is reused unchanged as a separate service.
- **Standing up / deploying budtender itself, or rotating its per-store Dutchie keys** — owner item O-1; the voice repo holds only `HHT_BUDTENDER_BASE_URL` + `HHT_BACKEND_TOKEN` and ships against the contract + a stub until O-1 confirms.

### 1.3 Non-negotiable boundaries (binding)

- **Leak-Guard (ADR-008).** Cost/margin/velocity/bucket can never appear in a tool response. Enforced at budtender's `serializers.PUBLIC_PRODUCT_FIELDS` allowlist AND re-asserted by a voice-repo contract test (`tests/test_leak_guard.py`). The voice repo reads **only** `public_product`-shaped fields; treating any field outside `PUBLIC_PRODUCT_FIELDS` as forbidden is a code-review block.
- **Numbers-Guard (`03-CONVENTIONS.md` §1.5).** The LLM NEVER originates a figure — every price/stock/SKU/quantity in a spoken pick comes from a budtender response field, not the model. The agent phrases; it never invents a price, a stock count, or a "this is X% THC."
- **OTD-only price speech (ADR-009).** When the agent quotes a price it quotes `price_otd` (out-the-door, tax-included) — never a pre-tax net, never cost.
- **budtender is the ONLY ranking authority (ADR-004/005).** The voice repo consumes the order budtender returns; it does NOT re-rank, re-sort, re-filter, or second-guess. The margin-vs-taste switch is *the presence of a recognized caller*, expressed by passing the resolved profile handle to budtender — not by the voice repo applying weights.
- **One gated upsell (ADR-007).** `pair_upsell` returns at most one complement; the agent voices it ONLY when `strength` clears the gate (recommend `>= 0.40`, §4.3). A silent (no-pair) response is correct, not a bug.
- **PII discipline (ADR-006/019).** Raw caller numbers are never persisted in the voice repo. The returning-caller key is the peppered hash; the Bearer token stays server-side (never reaches the device/Vapi).
- **HMAC fail-closed (ADR-019).** Every tool-call webhook arrives via `/api/voice/vapi`, verified by `core/middleware.py` before any P1 handler runs; a missing/bad signature → 401 with no handler execution. P1 does not relax this.

---

## 2. Dependencies (what MUST exist first)

P1 depends on **P0** (the chassis + webhook contract + Squad scaffold) and on the **happytime-budtender** service being reachable (O-1, an env placeholder — ship against the contract + stub, do not block). P1 runs **in parallel** with P2/P3 in its own worktree `wt-p1-suggest` (roadmap §6); it touches **disjoint files** (`voice/budtender_client.py`, `voice/tools/suggest.py`, the `budtender` member) from P2 (`voice/webhooks.py` eocr + escalation) and P3 (`voice/tools/vendor.py` + entry_router classifier).

| # | Dependency | Where it comes from | What P1 consumes from it |
|---|---|---|---|
| D1 | `voice/tools/__init__.py` with `TOOL_REGISTRY` dispatch (ADR-020) | **P0** (roadmap §7; ADR-020 "P0 ships the registry scaffold") | P1's `suggest.py` registers `suggest_products`/`check_inventory`/`pair_upsell` into the registry; the webhook routes by tool name. **Parallel-safe:** P1 adds its OWN module, never edits a shared `tools.py`. |
| D2 | `voice/webhooks.py` — `tool-calls` event dispatch into `TOOL_REGISTRY`; HMAC-verified, fail-closed | **P0** (`01-ARCHITECTURE.md` §0/§6) | The handler entry point; P1 plugs handlers in, does not modify the webhook router (P2/P3 own the eocr/status edits → no file collision). |
| D3 | `core/middleware.py` Vapi-webhook HMAC verify (constant-time, fail-closed) | **P0** (ADR-019) | P1's tools inherit it for free; the contract test re-asserts it on the tool path. |
| D4 | `voice/models.VoiceCall` / `VoiceTurn` / `Outcome` durable log | **P0** (the durable record) | P1 stamps the `suggested` outcome + the suggested SKUs onto the in-flight `VoiceCall` (read by P4's call log / analytics). |
| D5 | swedish-bot `crm/models.py::phone_hash` (peppered SHA-256) ported into the voice repo's `crm/` | **P0** ports `crm/models.py` (`01-ARCHITECTURE.md` §8 "ported"); the function lives at `swedish-bot/crm/models.py:17-29` | P1's recognition flow computes the phone-hash with this exact function (`PHONE_HASH_PEPPER`, distinct from `SECRET_KEY`). |
| D6 | The `budtender` assistant scaffolded as a member + `entry_router →(retail)→ budtender` transition in the Squad | **P0** scaffolds the empty member; P1 fills its prompt + attaches the 3 tools | P1 writes the budtender system prompt, the `tool_names`, and the toolIds wiring. |
| D7 | `core/services/vapi.py` REST client + `tools/provision_vapi.py` idempotent provisioner | **P0** (ADR-003) | P1's three tools are provisioned (create-once, GET-then-PATCH) and their `vapi_tool_id` written back onto local rows; the budtender member's `toolIds` resolve to these. |
| D8 | happytime-budtender reachable: `HHT_BUDTENDER_BASE_URL` + `HHT_BACKEND_TOKEN` (must equal budtender's), `/health/` 200 | **budtender** (O-1 placeholder); the contract is `_research-suggestion-engine.md` §5 | The client calls the documented endpoints; until O-1 is confirmed, the contract test uses a **recorded/stub** budtender response. |

**Graceful-degradation rule (so P1 is not hard-blocked by env placeholders).** Every owner-supplied placeholder (O-1 budtender base URL/token, O-4 store routing) is *read*, never *required at import*. With budtender unreachable/cold, the client returns a **fast graceful-empty** (`{"results": []}` / `{"pairing": null}`) — never a raised exception into the turn — and the budtender member says an honest *"I'm not finding a match in stock right now — want me to grab the team?"* line (budtender itself boots and returns `[]` safely with no Dutchie creds — `_research-suggestion-engine.md` §7). The phase ships, is testable against a stub, and flips live when O-1 lands.

---

## 3. File-by-file task list

Each entry: **exact path → responsibility → key functions/shape → source file to port/consume from (with its path)**. New files are marked **★ NEW**; the P0 files P1 plugs into are marked **PLUG** (P1 registers into them, never rewrites them — parallel-safe with P2/P3). budtender files are **CONSUMED OVER HTTP** (never copied — ADR-004).

### 3.1 `voice/budtender_client.py` ★ NEW — the thin Bearer HTTP client

**Responsibility:** the single seam between the voice repo and the happytime-budtender microservice. Per-method (one method per endpoint P1 needs), Bearer-authed, pooled, timeout-bounded, fail-graceful. Holds NO Dutchie key and NO ranking logic — it forwards slots and returns budtender's already-leak-safe JSON. **Cross-reference the wire contract in `_research-suggestion-engine.md` §5 (the budtender HTTP contract) — that doc is the canonical request/response shape; this client must match it field-for-field.**

| Element | Shape / behavior | Source to consume (with path) |
|---|---|---|
| `class BudtenderClient` | Constructed once (module-level singleton `budtender()`), holds a pooled `requests.Session` (keep-alive — avoids a fresh TCP+TLS handshake per voice turn, the latency property tuned in P5). Base URL from `settings.HHT_BUDTENDER_BASE_URL`; per-call timeout from `settings.HHT_BUDTENDER_TIMEOUT` (default 8s, tightened in P5). | `_research-suggestion-engine.md` §5.1 (base `/api/v1/`); `03-CONVENTIONS.md` §3.4 (env). |
| `_headers()` | `{"Authorization": f"Bearer {settings.HHT_BACKEND_TOKEN}", "Accept": "application/json", "User-Agent": "happytime-voice/0.1"}`. Token read from env once; **never logged**. Fail-closed: if `HHT_BACKEND_TOKEN` is empty, log + raise on the first call (mirrors budtender's own fail-closed auth). | budtender `auth.py:8-23` (`ServiceTokenPermission`, `Bearer`, `hmac.compare_digest`) — the matching auth side; `03-CONVENTIONS.md` §1.2 fail-closed. |
| `_post(path, payload)` / `_get(path, params)` | The two private HTTP primitives: build URL, attach headers, `session.post/get(..., timeout=…)`. On `Timeout`/`ConnectionError`/non-2xx → log a warning (redacting the token) and return a typed **graceful-empty** for the caller to interpret, **never raise into the turn**. | standard `requests.Session`; degrade rule §2. |
| `search_products(slots, *, limit=3, session_token=None, exclude_skus=None) -> dict` | `POST /api/v1/products/search/` with body `{slots, limit, location: slots["store"], session_token, exclude_skus}`. **No `phone` field is sent** (see §3.4 recognition design — the session is already profile-linked via `resume_by_phone`). Returns budtender's `{"results": [...], "source": ...}` verbatim (already leak-safe). Graceful-empty = `{"results": []}`. | `_research-suggestion-engine.md` §5.2 (request/response); budtender `views.py:109` `ProductSearchView`, `ranking.py:465` `rank_products`. |
| `check_inventory(store, sku) -> dict` | A SKU-scoped purchasability/stock/price check. Implemented as `POST /api/v1/products/search/` with a sku-scoped slot **or** `GET /api/v1/products/in-stock/?store=<slug>` then filter by sku (whichever budtender exposes cleanly — `_research-suggestion-engine.md` §5.1 lists both). Returns `{"in_stock": bool, "qty_band": str, "price_otd": float|null, "name": str|null}` (mapped from the leak-safe pick; `stock_on_hand` → a coarse `qty_band` so we never speak an exact count we don't need). Graceful-empty = `{"in_stock": false}`. | `_research-suggestion-engine.md` §1 (purchasability gate `MIN_STOCK=5`), §5.1 (`products/in-stock/`); budtender `dutchie.py` `_is_purchasable`. |
| `pair_for_sku(store, anchor_sku, *, session_token=None) -> dict` | `POST /api/v1/pairing/for-sku` body `{location: store, sku: anchor_sku, session_token}`. Returns `{"pairing": {...public_product}|null, "reason_code", "reason_text", "strength"}` verbatim. Graceful-empty = `{"pairing": null, "strength": 0.0}`. | `_research-suggestion-engine.md` §5.3; budtender `pairing.py:122` `pair_for`, `MAX_PAIR_PRICE_RATIO=0.50` (`pairing.py:35`). |
| `resume_by_phone(phone_hash=None, phone=None) -> dict` | `POST /api/v1/chat/resume-by-phone`. **Recognition seam — see §3.4 for the phone-hash-vs-phone decision.** Returns `{"session_token", "profile_summary": {has_history, top_categories[], price_tier}}` or `{"session_token": null, "profile_summary": {"has_history": false}}` on a miss. Graceful-miss never raises. | `_research-suggestion-engine.md` §3.1/§5.1/§5.4 (`profile_summary` is non-PII); budtender `views.py:85` `_profile_for_phone`, `views.py:28` `_hash_phone`. |
| `facets(kind, store, **filters) -> dict` | Thin GET/POST wrappers for `subtypes`/`sizes`/`price-bands`/`doh-options` so the slot-filling prompt only offers options that exist in live stock (`_research-education-blogs.md` §2 KB-build TODO: vocab parity). Optional in P1 (the budtender member can slot-fill from the KB taxonomy), but cheap and worth shipping. | `_research-suggestion-engine.md` §6 (facets); budtender `facets.py`, `urls.py:10-13`. |
| `health() -> bool` | `GET /api/v1/health/` (open, no token) → `True` on `{status:"ok"}`. Used by `core/views.healthz` (P0) to report budtender reachability, and by a P5 pre-warm ping. | `_research-suggestion-engine.md` §5.1; budtender `urls.py:6`. |

> **Binding:** this client is the ONLY place the voice repo talks to budtender. It does not import any budtender code; it speaks HTTP. Per ADR-004 the secret sauce (ranking/pairing/leak-serializer/Dutchie) stays in budtender.

### 3.2 `voice/tools/suggest.py` ★ NEW — the three tool handlers (registered into P0's `TOOL_REGISTRY`)

**Responsibility:** parse + validate the Vapi tool-call args, resolve the caller's recognition handle, call `budtender_client`, and shape the **leak-safe, OTD, speakable** tool result the assistant reads. One handler per tool; each registered by name in `TOOL_REGISTRY` (ADR-020 — P1 adds this module, P0 already shipped the registry). **PLUG**, not a rewrite of any shared file.

| Function | Responsibility | Key shape | Port/consume from |
|---|---|---|---|
| `handle_suggest_products(args, ctx) -> dict` | Validate slots (§4.1 schema), pull the recognized `session_token` from `ctx` (set by the recognition step §3.4), call `budtender_client.search_products(slots, limit=3, session_token=…, exclude_skus=…)`, map each result to the **speakable** shape (§4.5), stamp the suggested SKUs onto `ctx.voice_call`. Returns `{"picks":[…≤3], "spoken_summary": "<pick #1 line>"}`. Honest-empty when `results==[]`: `{"picks": [], "spoken_summary": "I'm not finding that in stock right now."}`. | budtender `ranking.py` (`_why` is already in `why_this`); `_research-suggestion-engine.md` §2.5. |
| `handle_check_inventory(args, ctx) -> dict` | Validate `{store, sku}`, call `budtender_client.check_inventory`, return `{"in_stock", "qty_band", "price_otd"}` (OTD price only; never cost). | `_research-suggestion-engine.md` §1 (`MIN_STOCK=5` gate). |
| `handle_pair_upsell(args, ctx) -> dict` | Validate `{store, anchor_sku}`, call `budtender_client.pair_for_sku`, **apply the strength gate** (`strength >= PAIR_STRENGTH_GATE`, §4.3): if it clears, return `{"offer": true, "pair": {…speakable…}, "reason_text", "strength"}`; else `{"offer": false}` (the agent stays silent — ADR-007). | budtender `pairing.py:122` `pair_for`, `strength` (`pairing.py:195-198`). |
| `_speakable_pick(result) -> dict` | The leak-safe → spoken mapper: copies ONLY `{rank, name, brand, strain, price_otd, thc_percent, why_this, sku}` from a budtender result; **drops anything not on the allowlist** (defense-in-depth even though budtender already serialized leak-safe). Renames `price`→`price_otd` is NOT needed (budtender returns `price` already OTD per the contract; we relabel as `price_otd` in the spoken shape to make the OTD invariant explicit). Asserts (in tests) no `cost`/`margin` key present. | budtender `serializers.py:13` `PUBLIC_PRODUCT_FIELDS`; ADR-008/009. |
| `register()` | Registers the three handlers into `TOOL_REGISTRY` under their Vapi tool names (`suggest_products`/`check_inventory`/`pair_upsell`). Called by `voice/tools/__init__.py` (the P0 registry loads each tool module). | ADR-020; P0 `voice/tools/__init__.py`. |

### 3.3 `voice/tools/__init__.py` PLUG — register P1's module (P0 owns the file)

P0 ships the `TOOL_REGISTRY` + a loader that imports each tool module and calls its `register()`. P1's ONLY edit here is adding `from . import suggest` to the import list (or, if P0 made it auto-discover, nothing). **This is the single line P1 may touch in a P0-owned shared file; keep it to one line to avoid a merge conflict with P3 (which adds `from . import vendor`).** Document this in the worktree's commit so the merge of P1+P3 is two one-line additions, not a conflict.

### 3.4 Returning-caller recognition flow (the swedish-bot peppered phone-hash) — design + files

This is the ADR-005/006 margin-vs-taste switch. The flow, end to end:

```
Vapi delivers the caller's number on the call (assistant-request / tool-call context: customer.number).
  → voice/recognition.py::resolve_caller(number, ctx):
       hash = crm.models.phone_hash(number)          # peppered SHA-256, swedish-bot crm/models.py:17-29
       result = budtender_client.resume_by_phone(<see decision below>)
       if result["session_token"]:                    # HIT — known returning caller
           ctx.session_token = result["session_token"]   # carried into suggest_products → W_KNOWN (taste-first)
           ctx.profile_summary = result["profile_summary"]  # non-PII: {has_history, top_categories[], price_tier}
           ctx.known = True
       else:                                           # MISS — anonymous
           ctx.session_token = None                    # suggest_products runs W_ANON (margin-first, HIGH MARGIN)
           ctx.known = False
  → the voice repo stores ONLY the hash on the VoiceCall (PII discipline); the raw number is never persisted.
```

**The phone-hash-vs-phone decision (binding, resolve in the build):** budtender's `/products/search/` and `/chat/resume-by-phone` today key on a **normalized phone** (`views.py:28` `_hash_phone`, `views.py:85` `_profile_for_phone` → `_normalize_phone(phone)` → `+1XXXXXXXXXX`), NOT on the swedish-bot **peppered** hash. swedish-bot's peppered hash is one-way and pepper-specific, so it cannot be the lookup key into budtender's existing `CustomerProfile.phone` index. Two correct options — pick ONE and record it as an ADR:

- **Option A (recommended — minimal, leak-safe at rest, no budtender change needed for v1):** the voice repo sends the **normalized phone over the already-secured server-to-server Bearer channel** (`resume_by_phone(phone=normalized)`), exactly as the website proxy does today; budtender resolves the profile by its existing phone index. The voice repo persists ONLY the **peppered hash** on its own `VoiceCall` (so a voice-DB leak never exposes a reversible phone index — ADR-006's actual goal). The raw number lives only in-transit over TLS+Bearer to budtender (which already holds PII for transaction history) and is never written to the voice DB. **This satisfies ADR-006 (no raw number persisted in the voice repo) without changing budtender.**
- **Option B (if the owner wants budtender to never receive a raw number from voice):** add a budtender admin endpoint that accepts the peppered hash and resolves it — requires budtender to store the same peppered hash alongside each profile (a budtender migration + the same `PHONE_HASH_PEPPER`). More work, defers on O-1; only take it if the owner mandates "voice never sends a raw number."

**Recommendation: ship Option A in P1** (it is the documented website pattern, leak-safe at rest in the voice repo, and unblocks taste-first today), and record the choice as **ADR-022** in `02-DECISIONS.md` in the same change. The phone-hash is still computed and stored on the `VoiceCall` (the recognition *key* the dashboard/analytics use, never a raw number).

**Files:**

| Path | Responsibility | Port/consume from |
|---|---|---|
| `voice/recognition.py` ★ NEW | `resolve_caller(number, ctx) -> ctx` (the flow above) + `phone_hash(number)` re-exported from `crm.models`. Pure-ish (one budtender call); unit-testable with the client stubbed. Sets `ctx.session_token`/`ctx.known`/`ctx.profile_summary`. | swedish-bot `crm/models.py:17-29` (peppered hash); budtender `views.py:85`/`views.py:28` (resolution); `_research-suggestion-engine.md` §3. |
| `voice/webhooks.py` PLUG (read-only seam) | At the point where a `budtender`-member turn first needs recognition (the `assistant-request` for the budtender member, or the first `suggest_products` tool call), call `recognition.resolve_caller(customer.number, ctx)` once and cache `session_token` on the budtender session (`POST /chat/persist/`) / the `VoiceCall`. **Stateless-turn discipline:** the `session_token` is persisted in budtender/`VoiceCall`, not in process memory (roadmap §8). P1 adds ONE call site; it does not rewrite the webhook router (P2/P3 own other parts of the file — coordinate the merge, or place the call inside `voice/tools/suggest.py`'s first-use path to avoid touching `webhooks.py` at all — **preferred for parallel-safety**). | `_research-suggestion-engine.md` §5.1 (`/chat/persist/`); roadmap §8. |

> **Parallel-safety note:** to avoid editing `voice/webhooks.py` (which P2 also edits), prefer resolving recognition **lazily inside `handle_suggest_products`** on first use (memoized on `ctx`/the budtender session) rather than at the webhook router. This keeps P1 entirely within `voice/budtender_client.py` + `voice/tools/suggest.py` + `voice/recognition.py` + the budtender `AgentPrompt` row — zero shared-file edits except the one-line `tools/__init__.py` import.

### 3.5 The `budtender` Squad member — prompt + tool wiring

| Path | Responsibility | Port/consume from |
|---|---|---|
| `kb/seed.py` EDIT (the `budtender` `AgentPrompt` body) | Seed the budtender member's focused system prompt: the slot-filling ladder (effect → activity → preferences → past-wins → explore → budget → select → quantity/upsell — the export's good design, ported), the Koptza tone (warm/family/no-pressure, conservative on dosing — `_research-education-blogs.md` §8), the **house rules**: speak `why_this` verbatim-ish; quote **OTD** prices only; offer the upsell ONLY when the tool says `offer:true`; never invent a price/stock/SKU (Numbers-Guard); never speak cost/margin (it never receives them). The prompt instructs the model to call `suggest_products` with the filled slots, `check_inventory` before confirming a specific SKU, and `pair_upsell` after a selection. | the export's per-category slot prompts (Downloads JSON, roadmap §10 anchor) for the ladder; `_research-education-blogs.md` §8 (house style), §5 (don't over-promise strain-type effects); ADR-009/011. |
| `AgentPrompt.tool_names` (the budtender row) | `["suggest_products", "check_inventory", "pair_upsell"]` — resolved to `toolIds` by provision (P0 `tools/provision_vapi.py`) and published by P4. | §4.6 (provision); ADR-020. |
| `voice/constants.py` PLUG (read-only) | The budtender member reuses the P0 member-level constants: `VAPI_VOICE_ID` (Cartesia sonic-3 Koptza `a3520a8f-226a-428d-9fcd-b0a4711a6829`), `VAPI_ASSISTANT_MODEL` (`gpt-4.1-mini`), `DEEPGRAM_KEYTERMS` (the ~33-term list). Set ONCE at the member level (ADR-011) — P1 does NOT re-declare them per node. | ADR-010/011; `03-CONVENTIONS.md` §3.3. |

### 3.6 Tests (new test modules)

| Path | Plane | Asserts |
|---|---|---|
| `tests/test_budtender_client.py` ★ | Unit (SQLite-OK, network stubbed) | Each client method builds the right URL/headers/body; timeout/connection-error → graceful-empty, never raised; token never logged. |
| `tests/test_suggest_tools.py` ★ | Unit | Arg validation (good/bad slots), the `_speakable_pick` mapper drops non-allowlist fields, OTD relabel, honest-empty path, the pairing strength gate (offer vs silent at the threshold). |
| `tests/test_recognition.py` ★ | Unit | `phone_hash` is peppered + deterministic; HIT sets `session_token`/`known=True`/`W_KNOWN` path; MISS → anonymous/`W_ANON`; **raw number never persisted** (assert the `VoiceCall` stores only the hash). |
| `tests/test_leak_guard.py` ★ (**mandatory gate**) | Contract (budtender stubbed/recorded) | **No `"cost"`/`"margin"`/`"velocity"`/`"bucket"` substring** in ANY of `suggest_products`/`check_inventory`/`pair_upsell` responses (ADR-008). The single most important P1 test. |
| `tests/test_suggest_contract.py` ★ | Contract | The tool→budtender request/response round-trip against a **recorded** budtender response (`_research-suggestion-engine.md` §5.2/§5.3 shapes): ≤3 picks, each with non-empty `why_this`, each in-stock, `price_otd` present; anonymous vs known path selects the right budtender call. |
| `tests/test_hmac_fail_closed.py` PLUG (re-assert) | Contract | A bad/missing Vapi signature → 401 before any P1 tool handler runs (re-run the P0 HMAC test against the tool path). |

---

## 4. Data contracts / JSON schemas

These are the **Vapi custom-tool `function.parameters` JSON Schemas** (what Vapi sends in a `tool-calls` event) and the **tool result shapes** the handler returns. The budtender wire contract (request/response to the microservice) is canonical in `_research-suggestion-engine.md` §5 — cross-reference it; the schemas below are the *Vapi-facing* arg contracts the assistant fills.

### 4.1 `suggest_products` — Vapi tool arg schema

```json
{
  "type": "function",
  "function": {
    "name": "suggest_products",
    "description": "Recommend up to 3 in-stock cannabis products for the caller from the live store menu. Call after the caller has stated at least a category or an effect. Returns leak-safe picks (no cost/margin) each with a short spoken reason and an out-the-door price. Use the spoken reason verbatim-ish; quote only the out-the-door price.",
    "parameters": {
      "type": "object",
      "properties": {
        "store":          { "type": "string", "enum": ["yakima", "mount-vernon", "pullman"], "description": "Store slug. Default yakima if the caller hasn't said which location." },
        "category":       { "type": "string", "enum": ["flower", "concentrate", "cartridge", "edible", "tincture"], "description": "Top-level product category (budtender CATEGORY_BY_SLOTKEY). Cartridge is first-class — never send 'concentrate' for a cart/510/disposable." },
        "subcategory":    { "type": "string", "description": "Optional granular subtype (e.g. 'rosin', 'gummies', 'live resin', 'disposable'). HARD filter in budtender — only set when the caller is explicit." },
        "size":           { "type": "string", "description": "Optional size: gram cats '0.5g'..'28g'; pre-rolls 'single'|'5pk'; carts '0.5g'|'1g'. HARD filter with a nearest-weight fallback. Only set when stated." },
        "price_tier":     { "type": "string", "enum": ["value", "mid", "top"], "description": "Budget intent when the caller gives a vibe not a number. Mutually informative with price_max." },
        "price_max":      { "type": "number", "description": "Hard upper out-the-door budget in dollars when the caller gives a number (e.g. 'under $40' -> 40)." },
        "effect_desired": { "type": "string", "enum": ["relaxed", "uplifted", "middle"], "description": "The effect the caller asked for. Map sleep/calm/body -> relaxed; energy/focus/social -> uplifted; balanced -> middle." },
        "doh_only":       { "type": "boolean", "description": "True only if the caller explicitly wants DOH-compliant (medical/DOH-approved) products.", "default": false }
      },
      "required": ["store", "category"]
    }
  },
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" }
}
```

> **Mapping to budtender (`_research-suggestion-engine.md` §5.2):** the handler folds these args into the budtender `slots` dict (`store→store`, `category→category`, `subcategory`, `size`, `price_max→price_max` OR `price_tier`, `effect_desired`, `doh_only`), sets `limit=3`, adds `session_token` (from recognition) + `exclude_skus` (from "show me something else"), and **does NOT send `phone`** (the session is already profile-linked — §3.4). budtender applies the margin-vs-taste weights from whether the session resolved a profile.

### 4.2 `check_inventory` — Vapi tool arg schema

```json
{
  "type": "function",
  "function": {
    "name": "check_inventory",
    "description": "Check whether one specific product (by SKU) is in stock on the sales floor and get its out-the-door price. Call before confirming a specific item the caller named.",
    "parameters": {
      "type": "object",
      "properties": {
        "store": { "type": "string", "enum": ["yakima", "mount-vernon", "pullman"], "description": "Store slug." },
        "sku":   { "type": "string", "description": "The product SKU to check (from a prior suggest_products pick)." }
      },
      "required": ["store", "sku"]
    }
  },
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" }
}
```

### 4.3 `pair_upsell` — Vapi tool arg schema

```json
{
  "type": "function",
  "function": {
    "name": "pair_upsell",
    "description": "Get ONE complementary, lighter, cheaper add-on for a product the caller is buying (by anchor SKU). Only offer the add-on out loud if the tool returns offer=true. Never push a second main purchase.",
    "parameters": {
      "type": "object",
      "properties": {
        "store":      { "type": "string", "enum": ["yakima", "mount-vernon", "pullman"], "description": "Store slug." },
        "anchor_sku": { "type": "string", "description": "The SKU of the item the caller is buying; the pairing is a complement to this." }
      },
      "required": ["store", "anchor_sku"]
    }
  },
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" }
}
```

**Strength gate (binding, ADR-007):** `PAIR_STRENGTH_GATE = 0.40` (a module constant in `voice/tools/suggest.py`). The handler returns `offer:true` ONLY when budtender's `strength >= PAIR_STRENGTH_GATE` AND `pairing` is non-null; otherwise `offer:false` and the agent stays silent. budtender already hard-gates price ≤50% of anchor (`pairing.py:35`) and builds `reason_text` from attributes (stays correct as stock rotates) — the voice repo only applies the *speak-or-not* threshold.

### 4.4 Tool result shapes (what the handler returns to the assistant)

`suggest_products` result:
```json
{
  "picks": [
    { "rank": 1, "name": "Blueberry OG 3.5g", "brand": "Phat Panda", "strain": "Blueberry OG",
      "price_otd": 38.0, "thc_percent": 27.3,
      "why_this": "Indica-dominant — customers grab it for sleep · hits hard at 27% THC",
      "sku": "PP-BBOG-35" }
  ],
  "spoken_summary": "My top pick is the Phat Panda Blueberry OG eighth — indica-dominant, folks love it for sleep, and it's thirty-eight out the door."
}
```
`check_inventory` result:
```json
{ "in_stock": true, "qty_band": "plenty", "price_otd": 38.0, "name": "Blueberry OG 3.5g" }
```
`pair_upsell` result (offered):
```json
{ "offer": true, "pair": { "name": "Ten-pack sleep gummies", "brand": "Wyld", "price_otd": 12.0, "sku": "WYLD-SLEEP-10" },
  "reason_text": "Folks who grab an eighth almost always toss in a low-dose gummy — an easy add-on.", "strength": 0.62 }
```
`pair_upsell` result (gated → silent):
```json
{ "offer": false }
```

### 4.5 The leak-safe → speakable field allowlist (the only fields the agent ever sees)

The `_speakable_pick` mapper copies **exactly** these fields from a budtender result and nothing else:

```
rank, name, brand, strain, price_otd, thc_percent, why_this, sku
```

- This is a **subset** of budtender's `PUBLIC_PRODUCT_FIELDS` (`serializers.py:13`) — which itself already excludes `cost`/`margin`/`velocity`/`bucket`/`margin_pct`/`price_z`. Two layers: budtender never serializes them; the voice repo never copies anything outside this list. (`image_url`/`dutchie_link` are dropped — irrelevant on a voice channel.)
- `price_otd` is budtender's `price`, which the contract guarantees is the customer-facing out-the-door figure (ADR-009). We relabel to `price_otd` to make the OTD invariant explicit in the spoken shape and in tests.
- **The no-leak contract test (`tests/test_leak_guard.py`) asserts no `"cost"`/`"margin"` substring** anywhere in the serialized tool result — the structural guarantee that the agent cannot speak them.

### 4.6 budtender wire contracts (canonical in `_research-suggestion-engine.md` §5 — restated for the executor)

| Tool | budtender endpoint | Request (key fields) | Response (leak-safe) |
|---|---|---|---|
| `suggest_products` | `POST /api/v1/products/search/` | `{slots:{store,category,subcategory,size,price_tier|price_max,effect_desired,doh_only}, limit:3, location, session_token, exclude_skus}` | `{results:[{rank,sku,name,brand,strain,price,price_was,thc_percent,dominant_terpene,stock_on_hand,image_url,why_this}…≤3], source}` |
| `check_inventory` | `POST /api/v1/products/search/` (sku-scoped) or `GET /api/v1/products/in-stock/?store=` | `{store, sku}` | leak-safe pick(s) → mapped to `{in_stock, qty_band, price_otd}` |
| `pair_upsell` | `POST /api/v1/pairing/for-sku` | `{location, sku, session_token}` | `{pairing:{…public_product}|null, reason_code, reason_text, strength}` |
| recognition | `POST /api/v1/chat/resume-by-phone` | `{phone}` (Option A, over Bearer/TLS) | `{session_token, profile_summary:{has_history, top_categories[], price_tier}}` |
| facets | `GET /api/v1/products/{subtypes,sizes,price-bands,doh-options}` | `?store=&category=` | in-stock-only slot options |

**Auth (every call except `/health/`):** `Authorization: Bearer <HHT_BACKEND_TOKEN>` (constant-time checked in budtender `auth.py:15-23`, fails CLOSED). **Store slugs:** `yakima`, `mount-vernon`, `pullman` (budtender `models.STORES`; default `yakima`). **Latency property:** budtender ranks over a pre-synced per-store `Product` table (inventory sync every 10 min), not a live Dutchie call (`_research-suggestion-engine.md` §7) → fast enough for a voice turn.

---

## 5. Vapi deploy steps (what this phase actually provisions/patches)

P1 **creates three tools** and **fills the budtender member**; it reuses P0's idempotent `core/services/vapi.py` + `tools/provision_vapi.py` (ADR-003 — GET-then-PATCH, never blind POST). Steps:

1. **Define the three tools** as code (the §4.1–4.3 schemas) in the provisioning catalog (`tools/provision_vapi.py`'s tool list, extended by P1). Each tool's `server.url = ${PUBLIC_BASE_URL}/api/voice/vapi`, `server.secret = ${VAPI_WEBHOOK_SECRET}`.
2. **Provision (idempotent):** `python tools/provision_vapi.py` → `ensure_tool("suggest_products")`, `ensure_tool("check_inventory")`, `ensure_tool("pair_upsell")` (GET-by-name → POST-once if absent → else PATCH). Each returned `toolId` is written back onto the local tool row (`vapi_tool_id`). A re-run produces **zero** new Vapi objects (drift-free — an acceptance criterion).
3. **Attach to the budtender assistant:** the budtender `AgentPrompt.tool_names = ["suggest_products","check_inventory","pair_upsell"]` resolves to `toolIds`; provision/Publish PATCHes the budtender assistant's `model.toolIds` with the three resolved ids. **Voice/transcriber/model are set ONCE on the member** (Cartesia sonic-3 Koptza, Deepgram nova-3 + keyterms, gpt-4.1-mini — ADR-010/011), never per node.
4. **Wire the Squad transition:** `entry_router →(retail intent)→ budtender` is part of the code-defined Squad shape (`01-ARCHITECTURE.md` §1.6); provision PATCHes `/squad/{id}` so `entry_router`'s `assistantDestinations` includes `budtender`. (The full classifier taxonomy that *fires* this transition is P3/P5 prompt work; P0 scaffolded the destination.)
5. **Never call `/workflow`** (ADR-002). Only `/tool`, `/assistant`, `/squad` CRUD.
6. **Verify drift-free:** a second `provision` run after P1 → only PATCHes (or no-ops), zero new objects (§7 G-criteria; ADR-003).

> The owner-editable version of all this (edit the budtender prompt + tool selection in the dashboard → Publish) is **P4**. P1 ships the code-defined provisioning; P4 adds the UI to PATCH it.

---

## 6. Acceptance criteria (testable, concrete)

Each is a pass/fail gate with explicit assertions (mirrors roadmap §5 P1).

**A. Client + connectivity**
- A1. budtender reachable over HTTP with `Authorization: Bearer <HHT_BACKEND_TOKEN>`; `BudtenderClient.health()` returns `True` against `/api/v1/health/` 200. With budtender unreachable, `health()` returns `False` and `healthz` reports it — the app still boots (degrade rule §2).
- A2. Every `BudtenderClient` method attaches the Bearer header and the `Accept`/`User-Agent` headers; a timeout/connection error returns the typed graceful-empty (`{"results": []}` / `{"pairing": null}`) and NEVER raises into the turn; the token never appears in any log line.

**B. `suggest_products`**
- B1. A valid tool-call (`store, category` required + optional slots) returns **≤3** picks, **each in-stock** (budtender's purchasability gate), **each with a non-empty `why_this`**, **each with a `price_otd`** (OTD). The required-field validator rejects a call missing `store`/`category` with a clear tool error (not a 500).
- B2. **Leak-safe (mandatory):** **no response field ever contains `"cost"` or `"margin"`** — `tests/test_leak_guard.py` asserts the substring is absent in every `suggest_products` result (ADR-008). The `_speakable_pick` mapper drops every field outside the §4.5 allowlist.
- B3. **Margin-vs-taste switch (ADR-005):** an **anonymous** caller (recognition MISS → no `session_token`) produces budtender's `W_ANON` order (margin-first — slot #1 highest gross-margin $); a **recognized** caller (recognition HIT → `session_token` from `resume-by-phone`) produces the `W_KNOWN` order (taste-first, affinity 0.34, drawn from the caller's real purchase history). Asserted against a recorded budtender response for each path (the voice repo asserts it sent/omitted the `session_token`, not that it re-ranked — budtender owns ranking).
- B4. Honest-empty: when budtender returns `results:[]`, the handler returns `{"picks": [], "spoken_summary": "I'm not finding that in stock right now."}` and the agent does NOT fabricate a product (Numbers-Guard).

**C. `check_inventory`**
- C1. A valid `{store, sku}` returns `{in_stock, qty_band, price_otd}`; an out-of-stock/zombie SKU returns `in_stock:false` (budtender's freshness+floor gate, `MIN_STOCK=5`). No cost/margin in the response (Leak-Guard).

**D. `pair_upsell` (ADR-007)**
- D1. Returns at most ONE complement; `offer:true` ONLY when `strength >= 0.40` AND `pairing` non-null; otherwise `offer:false` (silent). The complement is ≤50% of the anchor price (budtender's hard gate) — asserted present in the offered shape.
- D2. A weak/expensive/no-complement case → `offer:false`, and the agent stays silent (no upsell spoken). This is correct, not a failure.

**E. Returning-caller recognition (ADR-006)**
- E1. `voice/recognition.phone_hash(number)` equals `crm.models.phone_hash(number)` (peppered SHA-256, `PHONE_HASH_PEPPER` ≠ `SECRET_KEY`); the same number → the same hash; a different pepper → a different hash.
- E2. A recognition HIT sets `ctx.known=True`, carries the `session_token` into `suggest_products`, and surfaces a `profile_summary` (non-PII: `has_history`/`top_categories`/`price_tier`). A MISS sets `ctx.known=False`, no `session_token`.
- E3. **PII discipline:** the `VoiceCall` row stores ONLY the peppered hash — a test asserts the raw caller number is absent from every persisted voice-repo field (ADR-006/019).

**F. Speakable + OTD (ADR-009)**
- F1. The budtender member speaks the pick's `why_this` (built from real signals only — `_research-suggestion-engine.md` §2.5) and quotes the OTD price when it quotes a price at all; a unit test on the prompt-contract + a manual call confirm no pre-tax/cost figure is ever spoken.

**G. Vapi provisioning (ADR-003)**
- G1. `tools/provision_vapi.py` creates the three tools (POST-once) and attaches their `toolIds` to the budtender assistant; ids are written back onto local rows.
- G2. **Idempotency / zero-drift:** a second `provision` run with no edits issues **zero** new Vapi objects (only GET/PATCH or no-op) — assert the create-call count is 0 on the second run (mocked `vapi.py`).
- G3. **No per-node duplication (ADR-011):** the budtender assistant payload sets voice/transcriber/model ONCE; a test asserts the keyterm list + voiceId + model appear exactly once in the assistant payload.

**H. Security / fail-closed (ADR-019)**
- H1. A tool-call webhook with a missing/bad HMAC signature → 401 before any P1 handler runs (`tests/test_hmac_fail_closed.py`, re-asserted on the tool path).
- H2. The `HHT_BACKEND_TOKEN` and `VAPI_WEBHOOK_SECRET` never appear in any rendered output, tool result, or log line.

---

## 7. Test plan

Mirrors the four planes in `03-CONVENTIONS.md` §5 (Unit · Contract · Provisioning · Manual call). P1 touches a tool path → the **Leak-Guard** and **HMAC-fail-closed** tests are non-negotiable gates.

### 7.1 Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network)
- `tests/test_budtender_client.py` — each method's URL/headers/body; timeout/connection-error → graceful-empty (never raised); the token is not in logs (capture logs, assert absence). Stub the HTTP layer (`responses`/`respx`).
- `tests/test_suggest_tools.py` — `suggest_products` arg validation (required `store`/`category`; bad enums rejected); `_speakable_pick` allowlist (drops everything outside §4.5; relabels `price`→`price_otd`); honest-empty path (B4); the pairing strength gate at the threshold (D1/D2 — `0.39`→silent, `0.40`→offer).
- `tests/test_recognition.py` — `phone_hash` peppered/deterministic (E1); HIT vs MISS sets the ctx correctly (E2); raw number never persisted (E3) — assert the `VoiceCall` field set contains only the hash.

### 7.2 Contract (`pytest -m integration`, budtender stubbed/recorded, Vapi client mocked)
- `tests/test_leak_guard.py` (**mandatory**) — no `"cost"`/`"margin"`/`"velocity"`/`"bucket"` substring in any `suggest_products`/`check_inventory`/`pair_upsell` response (ADR-008 / B2). Reuse a recorded budtender response fixture that *deliberately* would have contained cost/margin server-side, proving the allowlist strips them.
- `tests/test_suggest_contract.py` — the tool→budtender round-trip against recorded responses (`_research-suggestion-engine.md` §5.2/§5.3): ≤3 picks, non-empty `why_this`, in-stock, `price_otd` present (B1); the anonymous path sends NO `session_token` and the known path sends one (B3 — assert the outgoing request body, since budtender owns the actual re-ranking).
- `tests/test_pairing_gate.py` — recorded pairing responses at `strength` 0.30/0.40/0.62 → silent/offer/offer; the offered pair's price ≤50% of the anchor (D1).
- `tests/test_recognition_contract.py` — `resume_by_phone` HIT returns a `session_token` carried into the next `search_products` call; MISS → none (E2/B3); Option A sends the normalized phone over Bearer and the voice DB stores only the hash (E3).
- `tests/test_hmac_fail_closed.py` (**mandatory**) — bad/missing signature → 401 before the tool handler (H1); valid signature passes.
- `tests/test_provision_tools.py` — mocked `core/services/vapi.py`: first run POSTs three tools + PATCHes the budtender assistant's toolIds; **second run = 0 POSTs** (G2); voice/model appear once in the assistant payload (G3).

### 7.3 Provisioning (`python tools/provision_vapi.py --dry-run` then live against a sandbox key)
- Dry-run prints the three tool upserts + the budtender assistant toolIds PATCH. Live run against a sandbox key creates them; a re-run is drift-free (G1/G2). Paste the dry-run diff (POST-once then PATCH-only).

### 7.4 Manual call script (the per-phase definition of done — `03-CONVENTIONS.md` §5)
Dial `VAPI_PHONE_NUMBER_ID` (O-4 placeholder — use the provisioned test number) and run, pasting the **transcript + the resulting `VoiceCall` row + the `suggest_products` tool args/response** for each:
1. **Anonymous high-margin path:** from a number with NO budtender history — *"recommend an indica for sleep under $40."* → agent confirms 21+ (entry_router), hands to budtender, calls `suggest_products{store, category:"flower", effect_desired:"relaxed", price_max:40}`, speaks ≤3 in-stock picks with `why_this` + **OTD** prices; the order is margin-first (`W_anon`). Confirm **no cost/margin spoken**.
2. **Returning-caller taste-first path:** from a number that resolves to a budtender profile (seed one via `customer/profile-upsert`) — same ask → the recognition HIT carries the `session_token`; the picks reflect the caller's real brands/categories (taste-first `W_KNOWN`) and the `why_this` gains a personal hook ("your go-to Phat Panda"). Paste the `profile_summary` + the picks.
3. **Gated upsell:** after a selection, the agent calls `pair_upsell{anchor_sku}`; if `strength >= 0.40` it offers ONE complement ≤50% of the anchor ("want to add a low-dose sleep gummy for twelve out the door?"); if not, it stays silent. Paste both a `offer:true` and a `offer:false` example if reproducible.
4. **Honest miss:** ask for an out-of-stock/absurd combo → the agent says it's not in stock and does NOT invent a product (Numbers-Guard).

**Test-data discipline:** deterministic fixtures; expected values hand-authored (not generated by the code under test). The **Leak-Guard** (`tests/test_leak_guard.py`) and **HMAC-fail-closed** (`tests/test_hmac_fail_closed.py`) tests are **non-negotiable gates** on this phase. Coverage: ~90% diff coverage on `voice/budtender_client.py`, `voice/tools/suggest.py`, `voice/recognition.py`; never lower an existing ratchet.

**Hygiene gate (paste all outputs — `03-CONVENTIONS.md` §1.3):** `ruff check` + `ruff format --check` clean; targeted `pytest` green; `python manage.py check`; `makemigrations --check` exit 0 (the only schema touch is the `VoiceCall` outcome/SKU stamp + the phone-hash field, if P0 didn't already ship them — commit the migration). Never claim passing without the pasted output.

---

## 8. Risks / open questions

| Risk / open item | Impact | Mitigation / disposition |
|---|---|---|
| **O-1 — budtender deploy location + current per-store Dutchie keys unknown** | P1's live calls fail until budtender is reachable | Ship the client against the documented contract (`_research-suggestion-engine.md` §5); the contract test uses a **recorded/stub** budtender response; the graceful-empty path keeps the agent honest meanwhile. Flip live when the owner confirms `HHT_BUDTENDER_BASE_URL` + a matching `HHT_BACKEND_TOKEN`. **Not blocked.** |
| **Phone-hash vs phone keying mismatch** (swedish-bot peppered hash ≠ budtender's normalized-phone index) | Recognition can't resolve a profile if we send the wrong key | Resolve via **ADR-022 (Option A, §3.4):** send the normalized phone to budtender over the Bearer/TLS channel (the existing website pattern); persist ONLY the peppered hash in the voice DB (ADR-006's real goal — no reversible index at rest). Record the ADR in the same change. Option B (budtender stores the peppered hash) is the fallback if the owner forbids voice→budtender raw numbers. |
| **Cost/margin leak into spoken output** (owner rule + ADR-008) | Trust + rule violation | Two layers: budtender's `serializers.public_product` allowlist (`serializers.py:13`) never serializes cost/margin; the voice repo's `_speakable_pick` re-allowlists; `tests/test_leak_guard.py` asserts no substring. The agent is structurally incapable of speaking them. |
| **budtender cold / returns `[]`** (no Dutchie creds or stale sync) | Empty recommendations | budtender boots and returns `[]` safely (`_research-suggestion-engine.md` §7); the handler's honest-empty path makes the agent say "not finding that in stock" instead of hallucinating. The 10-min inventory sync + on-request self-heal keep it fresh once creds are wired. |
| **Latency: a slow budtender hop stalls the voice turn** | Caller hears dead air | Pooled `requests.Session` (keep-alive) + a per-call timeout (`HHT_BUDTENDER_TIMEOUT`, tightened to 3–4s in P5) + a fast graceful-empty on timeout (never block the turn). budtender ranks over a pre-synced table (not a live Dutchie call) so the happy path is fast. Full latency tuning + load test = **P5** (`15-P5` §3.4). |
| **Pairing strength gate value (0.40) is a judgment call** | Too low → annoying upsells; too high → never offered | Ship `PAIR_STRENGTH_GATE=0.40` as a single module constant (easy to tune); the research suggests "only voice when strength >= ~0.4" (`_research-suggestion-engine.md` §8.3). Make it a `RankingWeights`-adjacent knob in P4 if the owner wants to tune it from the dashboard. |
| **Parallel-worktree collision on `voice/tools/__init__.py` / `voice/webhooks.py`** (P1 ∥ P2 ∥ P3) | Merge pain | P1 adds its OWN module `voice/tools/suggest.py` (ADR-020); the only shared-file edit is a **one-line** `from . import suggest` in `tools/__init__.py` (P3 adds `from . import vendor` — two one-line additions, not a conflict). Recognition resolved **lazily inside `suggest.py`** to avoid editing `webhooks.py` (P2's file). Documented in §3.3/§3.4. |
| **budtender contract drift** (an endpoint shape changes vs `_research-suggestion-engine.md`) | Tool returns wrong shape | The client is the single seam; the contract test pins the request/response shape against a recorded response; `_research-suggestion-engine.md` §5 is the canonical reference cited in the client. A drift fails the contract test loudly. |
| **`check_inventory` endpoint ambiguity** (search-sku-scoped vs `products/in-stock/`) | Two possible implementations | Pick the one budtender exposes cleanly at build time (both are documented — `_research-suggestion-engine.md` §5.1); the client method's signature/return shape is stable either way (`{in_stock, qty_band, price_otd}`), so the choice is an internal detail behind the tool contract. |
| **Open: should the agent ever speak `thc_percent` / `stock_on_hand`?** | Over-promising or revealing exact counts | Speak `thc_percent` only when budtender already surfaced it in `why_this` (≥25% threshold — `_research-suggestion-engine.md` §2.5); map `stock_on_hand` to a coarse `qty_band` ("plenty"/"a few left") rather than an exact count. Confirm tone with owner; defaults are conservative. |

---

## 9. Documentation protocol (close-out — binding, `03-CONVENTIONS.md` §6)

On completing P1, **in the same change:**
- Bump this doc's status to `DONE` with a live-verified note (paste the manual-call transcript anchor).
- Check off `11-P1-DUTCHIE-SUGGESTIONS.md` in `00-MASTER-ROADMAP.md` §7 (the "Phase specs" + the P1 build items).
- Append **ADR-022** to `02-DECISIONS.md` recording the phone-hash-vs-phone recognition decision (§3.4 Option A: send normalized phone over Bearer/TLS, persist only the peppered hash; budtender unchanged).
- Note the `voice/budtender_client.py` + `voice/tools/suggest.py` + `voice/recognition.py` net-new files and the `PAIR_STRENGTH_GATE` constant in `01-ARCHITECTURE.md` §8 (the net-new table).
- Record any new env confirmation for O-1 (`HHT_BUDTENDER_BASE_URL`/`HHT_BACKEND_TOKEN` live values) if the owner supplied them; otherwise leave them flagged as placeholders in `03-CONVENTIONS.md` §3.4.
- Append a line to `brain/`-equivalent daily notes if the repo carries them.

**No task starts without context loaded; none completes without docs updated.** Sub-agents inherit this.

---

## 10. Source-file anchors (for the executor)

**happytime-budtender (CONSUMED OVER HTTP — never copied; ADR-004):** `C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender\budtender\`
- `ranking.py` — `W_ANON` (`:14`), `W_KNOWN` (`:15`), `BUCKET_NUDGE` (`:18`), `CATEGORY_BY_SLOTKEY` (`:32`), `_affinity_score` (`:234`), `_quality_fit` (`:248`), `_novelty_bias` (`:257`), `_recency_boost` (`:282`), `rank_products` (`:465`), `premium_intent` (`:482`), `_why` (`:676`).
- `pairing.py` — `LADDER_COMPLEMENTS` (`:19`), `MAX_PAIR_PRICE_RATIO=0.50` (`:35`), `IDEAL_PAIR_PRICE_RATIO=0.25` (`:36`), `pair_for` (`:122`), `strength` (`:195-198`).
- `serializers.py` — `PUBLIC_PRODUCT_FIELDS` (`:13`), `public_product` (`:24`) — **the leak-safe allowlist; cost/margin never referenced**.
- `auth.py` — `ServiceTokenPermission` (`:8`), `HHT_BACKEND_TOKEN` (`:15`), `hmac.compare_digest` + Bearer (`:19-23`) — the matching auth side for the client.
- `urls.py` — endpoint map: `health/` (`:6`), `products/search/` (`:8`), `products/in-stock/` (`:9`), facets (`:10-13`), `pairing/for-sku` (`:14`), `chat/resume-by-phone` (`:15`), `chat/persist/` (`:16`), `customer/profile-upsert` (`:17`), `analytics/summary` (`:19`).
- `views.py` — `_hash_phone` (`:28`), `_profile_for_phone` (`:85`), `ProductSearchView` (`:109`), `PairingView` (`:377`).

**swedish-bot (port the phone-hash pattern):** `C:\Users\vladi\OneDrive\Desktop\swedish-bot\crm\models.py`
- `phone_hash` (`:17-29`) — peppered SHA-256 (`PHONE_HASH_PEPPER`, normalize-then-hash); `EmailSink`/`dispatch` in `crm/sinks.py` (`:40`/`:119`) are P2's, not P1's.

**Research (canonical contracts):** `C:\happytime-voice\docs\plans\_research-suggestion-engine.md` (§2 high-margin/`W_ANON`, §3 recognition/`W_KNOWN`, §4 pairing, §5 the HTTP contract, §6 facets, §7 freshness, §8 what P1 must adopt); `_research-education-blogs.md` (§8 house style for the budtender prompt, §5 strain-type caution, §9-10 taxonomy/WA-limits for slot vocabulary).

**Foundation:** `C:\happytime-voice\docs\plans\{00-MASTER-ROADMAP,01-ARCHITECTURE,02-DECISIONS,03-CONVENTIONS}.md`.

**Dependencies authored by other phases:** P0 (`voice/tools/__init__.py` registry, `voice/webhooks.py` tool dispatch + HMAC, `core/middleware.py`, `voice/models.VoiceCall/VoiceTurn/Outcome`, ported `crm/models.phone_hash`, `core/services/vapi.py`, `tools/provision_vapi.py`, the scaffolded `budtender` member + `voice/constants.py` member-level voice/model/keyterm constants). P4 (the dashboard weights tuner + Publish-to-Vapi over the same tools). P5 (cartridge entry from router, correction-reset, latency tuning of this exact budtender hop).
