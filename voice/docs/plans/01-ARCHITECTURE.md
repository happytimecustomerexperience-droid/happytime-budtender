# 01 — ARCHITECTURE — Happy Time Voice Agent

> **Status:** FOUNDATION (authoritative). Written 2026-06-22.
> **Reads from:** `00-MASTER-ROADMAP.md`. **Binds:** every phase doc (`10`–`16`).
> **Locked decisions it implements:** see `02-DECISIONS.md`. This doc is the target *shape*; phase docs are the *build steps*.

---

## 0. The four planes (one-screen mental model)

```
   CALLER (PSTN)
        │  inbound number(s)  [VAPI_PHONE_NUMBER_ID — O-4 placeholder]
        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  VAPI SURFACE  — one Squad of 5 Assistants (entry_router/budtender/faq/     │
│  vendor/escalation). Voice=Cartesia sonic-3 "Koptza"; STT=Deepgram nova-3;  │
│  LLM=gpt-4.1-mini. Set ONCE at member level. Tools call our webhook.        │
└───────┬───────────────────────────────────────────────────────────────────┘
        │  HTTPS POST /api/voice/vapi   (HMAC-verified, fail-closed)
        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  CONTROL + DATA PLANE  — happytime-voice (Django, this repo)                │
│  • voice/webhooks.py   assistant-request | tool-calls | status | eocr       │
│  • voice/tools/*       suggest | check_inventory | pair_upsell | faq_lookup  │
│  •                     | notify_vendor_callback                             │
│  • voice/guardrails.py code-owned safety (no leak / age / scope)            │
│  • voice/models.py     VoiceCall / VoiceTurn / Outcome (durable log)        │
│  • core/services/vapi.py   Vapi REST client (provision + publish)           │
│  • core/services/gemini.py KB grounding + call summaries (Vertex/API key)   │
│  • kb/                  FAQ/policy/store-facts/taxonomy (dashboard-editable) │
│  • crm/                 Caller(phone-hash) / VendorCallback / sinks(email)   │
│  • dashboard/           editor + flow canvas + Publish-to-Vapi + call log    │
└───────┬───────────────────────────────┬───────────────────────────────────┘
        │  Bearer HHT_BACKEND_TOKEN      │  Bearer Vapi private key
        ▼  (suggest/inventory/pairing)   │  (PATCH /assistant /squad)
┌──────────────────────────────┐         ▼
│  BUDTENDER MICROSERVICE       │   ┌─────────────────────────┐
│  (happytime-budtender, sep.)  │   │  api.vapi.ai REST        │
│  Dutchie POS + ranking +      │   │  assistant/squad/tool/   │
│  pairing + LEAK-SAFE serializ.│   │  phone-number CRUD       │
│  per-store Dutchie keys HERE  │   └─────────────────────────┘
└──────────┬───────────────────┘
           │  HTTP Basic (per-store key as username)
           ▼
   DUTCHIE POS REST  (api.pos.dutchie.com)
```

- **Voice/Vapi surface** = the conversation runtime (assistants + tools + phone).
- **Control plane** = the dashboard → Vapi REST publish (`core/services/vapi.py`).
- **Data plane** = the voice repo ⇄ budtender HTTP contract (suggestions/inventory).
- **KB plane** = Django `kb/` models (canonical) + a Vapi Files/Query-tool mirror.

---

## 1. The Squad of assistants

**One Squad** ("Happy Time Voice"), five saved (permanent, dashboard-visible, PATCHable-by-ID) assistant members. Voice/transcriber/model set **once per member** (NOT per node — the export duplicated it 51×). Each member has a small, focused system prompt → less hallucination, lower latency, fewer tokens.

### 1.1 `entry_router` — answers the call, greets as Koptza, classifies intent

- **Role:** greet warmly ("Koptza" persona, family/community tone); spoken **"are you 21 or older?"** age confirm (NO "peek at your ID" — a phone agent can't see ID); classify the caller's intent in one slot-filling turn (model gpt-4.1-mini).
- **Tools:** none required for routing (classification is a single LLM turn). May call `faq_lookup` for a trivial one-liner before handing off.
- **Transitions (Squad `assistantDestinations`):**
  - retail buyer ("looking for / recommend / what's good for…") → **budtender**
  - hours / specials / returns / payment / pickup / location → **faq**
  - vendor / wholesale / delivery / manifest / "I'm dropping off" → **vendor**
  - (≥2 explicit human requests) OR (return dispute) OR (defective product) → **escalation**
- **Hydrated vars:** `{{store_name}}`, store hours, transfer number — via per-phone-number assistant overrides (fixes export #11 unhydrated `{{store_name}}`).

### 1.2 `budtender` — slot-fill + product suggestions (the retail brain)

- **Role:** category-aware slot-filling (effect → activity → preferences → past-wins → explore → budget → select → quantity/upsell). Speaks picks + `why_this`; offers one gated upsell. Speaks **out-the-door (tax-included)** prices only.
- **Tools (server.url → `/api/voice/vapi`):**
  - `suggest_products(store, category, subcategory, size, price_tier, effect_desired, doh_only)` → budtender `POST /api/v1/products/search/` → ≤3 leak-safe picks.
  - `check_inventory(store, sku)` → budtender purchasability gate.
  - `pair_upsell(store, anchor_sku)` → budtender `pairing/for-sku`, surfaced only if `strength` clears the gate.
- **Personalization:** caller number → peppered phone-hash → budtender `chat/resume-by-phone` → their Dutchie purchase history/profile → ranking uses **taste-first `W_KNOWN`** (affinity 0.34); UNKNOWN caller → **margin-first `W_ANON`** (margin 0.55, owner emphasis: high margin first).
- **Leak-safe:** every pick comes through budtender's `public_product` allowlist serializer — `cost`/`margin` are never present.
- **Cartridge fix (#4):** an opener "I want a cart/510/vape" reaches a cartridge sub-flow (router classifies category up front; budtender does not bury cartridge under a concentrate sub-branch).

### 1.3 `faq` — grounded answers from the KB

- **Role:** answer hours / specials / returns / payment / pickup / limits / store facts / weights+types education, grounded — never hand-waved.
- **Tools:** `faq_lookup(query, store?)` → `/api/voice/vapi` → reads `kb/` live (canonical; edits are instant). A Vapi **Query Tool** over mirrored Files is the low-latency fallback.
- **Knowledge it must speak fluently:** WA purchase limits (1oz flower / 7g concentrate / 16oz solid edibles); pickup-only, no delivery; cash/debit + on-site ATM; 21+; weekly specials; the FULL weights+types taxonomy (flower gram/eighth-3.5g/quarter-7g/half-14g/oz-28g, pre-rolls, concentrates 0.5g/1g, carts 0.5g/1g, edibles mg / 10-serving-100mg WA packs, tinctures, THC:CBD ratios, solventless vs BHO, distillate vs live resin/rosin); return policy incl. WAC 314-55-079 defective exception.

### 1.4 `vendor` — wholesale/delivery/manifest handler

- **Role:** never enters retail. Owner's exact flow: warm `transferCall` to the store human → if NO ANSWER, control returns to this AI member → ask them to explain what they're calling about → log + alert + state a callback window.
- **Tools:**
  - `transferCall` (warm) to the per-location store number.
  - `notify_vendor_callback(store, caller_phone_hash, reason, summary)` (async tool) → logs a `VendorCallback`, fires a staff email/alert, returns a stated callback window.

### 1.5 `escalation` — de-escalation + warm human transfer

- **Role:** the export's dead orphan, fixed. Real inbound transitions from `entry_router`/`budtender`/`faq`. De-escalation script + WA defective-product path.
- **Trigger conditions:** (≥2 explicit human requests) OR (return dispute) OR (defective product return).
- **Tool:** warm `transferCall` with `transferPlan.mode = "warm-transfer-wait-for-operator"` + a `summaryPlan` that injects `{{transcript}}`, destination = the **populated** per-location number (env placeholder, O-4).

### 1.6 Squad shape (provisioned as code)

```
Squad "Happy Time Voice"
  members:
    - entry_router  → destinations: [budtender, faq, vendor, escalation]
    - budtender     → destinations: [escalation]            (human request mid-flow)
    - faq           → destinations: [budtender, escalation]  (cross-sell / dispute)
    - vendor        → destinations: [escalation]
    - escalation    → (terminal; warm transferCall out)
  shared (set once at each member): voice, transcriber, model  (NO per-node dup)
```

---

## 2. New repo folder layout (fork swedish-bot)

```
happytime-voice/                       (C:\happytime-voice — off OneDrive)
├── manage.py  pyproject.toml  uv.lock  Makefile  DEPLOY.md
├── Dockerfile  docker-compose.yaml  docker-compose.prod.yaml
├── Caddyfile  Caddyfile.prod
├── .env.example                       # full env catalog (see 03-CONVENTIONS.md)
├── config/
│   ├── settings.py                    # ← swedish-bot: lean, env-driven, prod-fail-closed
│   ├── urls.py                        # admin + /api/voice + /dashboard + /healthz
│   ├── wsgi.py  asgi.py
├── core/
│   ├── services/gemini.py             # ← LIFT VERBATIM from swedish-bot/core/services/gemini.py
│   ├── services/vapi.py               # ★ NEW: Vapi REST client (provision + publish; full CRUD per 20-SPEC)
│   ├── services/redact.py             # ★ NEW (P-sec/23): secret redaction for logs/reports
│   ├── constants.py                   # ← swedish-bot: model/pricing single source of truth
│   ├── celery.py                      # ★ NEW (P5, gated HHT_USE_CELERY): post-call work queue
│   ├── middleware.py                  # ← swedish-bot CORS + ★ Vapi webhook HMAC verify
│   ├── views.py                       # healthz (DB + Gemini + Vapi-auth status)
│   └── urls.py
├── voice/                             # ★ NEW app: telephony adapter + tool webhooks
│   ├── webhooks.py                    # POST /api/voice/vapi: assistant-request | tool-calls
│   │                                  #   | status-update | end-of-call-report  (HMAC-verified)
│   ├── signing.py                     # ★ Vapi webhook HMAC/secret verify (fail-closed) — P0
│   ├── tools/                         # one module per tool (parallel-safe; registry in __init__)
│   │   ├── __init__.py                #   TOOL_REGISTRY dispatch (P0 ships this)
│   │   ├── faq.py                     #   faq_lookup           (P0)
│   │   ├── suggest.py                 #   suggest/check_inventory/pair_upsell (P1)
│   │   └── vendor.py                  #   notify_vendor_callback (P3)
│   ├── budtender_client.py            # ★ Bearer HTTP client → happytime-budtender (P1)
│   ├── recognition.py                # ★ returning-caller phone-hash → budtender profile (P1)
│   ├── pricing.py                     # ★ OTD price helpers for spoken picks (P1)
│   ├── outcomes.py                    # ★ classify VoiceCall.outcome from transcript (P2/P3)
│   ├── routing.py                     # ★ entry_router intent classifier helpers (P5)
│   ├── corrections.py                # ★ mid-flow back-edge / correction handling (P5)
│   ├── vendor_flow.py                 # ★ vendor transfer→no-answer→capture flow (P3)
│   ├── provision.py                   # ★ everything-as-code Vapi reconcile engine (20-SPEC)
│   ├── analytics.py                   # ★ call_summary aggregates over durable rows (P5)
│   ├── tasks.py                       # ★ Celery tasks: summarize/dispatch/rollup (P5, gated)
│   ├── constants.py                   # ★ member-level voice/model/keyterms (set ONCE — ADR-011)
│   ├── orchestrator.py                # ← adapt swedish-bot FSM IF server-side turn logic needed
│   ├── guardrails.py                  # ← code-owned safety (no cost/margin leak, age, scope)
│   ├── summarize.py                   # call summary via core/services/gemini.py
│   ├── models.py                      # VoiceCall / VoiceTurn / Outcome (durable log) + VapiObject
│   ├── urls.py                        # /api/voice/vapi route
│   └── management/commands/
│       └── provision_vapi.py          # ★ manage.py provision_vapi (idempotent — 20-SPEC)
├── kb/                                # ← swedish-bot, slimmed
│   ├── models.py                      # FAQEntry / PolicyDocument / StoreFact / EducationDoc / BlogDoc
│   │                                  #   / WeightTypeTaxonomy / AgentPrompt / FlowConfig
│   ├── ingest.py                      # idempotent PDF/text ingest (sha256, magic-byte, size cap)
│   ├── semantic.py                    # ← embeddings cosine; pgvector swap-seam documented
│   ├── seed.py                        # ★ seed FAQ/return-policy/store-facts/WA-law/taxonomy
│   ├── vapi_files.py                  # ★ mirror KB → Vapi Files + Query Tool
│   └── management/commands/
│       └── seed_kb.py                 # ★ manage.py seed_kb (idempotent; --reindex)
├── crm/                               # ← swedish-bot
│   ├── models.py                      # Caller (peppered phone-hash) / CallSession / VendorCallback (P3)
│   ├── sinks.py                       # ← EmailSink (happytimeyak509@gmail.com) + Slack + webhook
│   └── profile.py                     # ← returning-caller profile shell (wired P1)
├── dashboard/                         # ← swedish-bot, expanded
│   ├── views.py                       # agents / flow / KB mgr / call log / weights / Publish-to-Vapi
│   ├── models.py                      # ★ RankingWeights singleton (+ dashboard-local rows)
│   ├── forms.py                       # ★ ModelForms: KB rows / RankingWeights / VendorCallback
│   ├── publish.py                     # ★ Publish-to-Vapi mapping (shares payload builders w/ provision)
│   ├── weights.py                     # ★ read/write RankingWeights + push to budtender
│   ├── monitor.py                     # ★ live/recent call-monitor query helpers
│   ├── branding.py                    # ★ brand tokens (logo/hex/fonts) loader (P5)
│   └── urls.py
├── templates/dashboard/flow.html      # ← Alpine/SVG canvas, fork
├── templates/dashboard/analytics.html # ★ call-summary page (P5)
├── tools/
│   ├── provision_vapi.py              # ★ thin shim → voice.provision.provision_all (20-SPEC)
│   └── loadtest_voice.py              # ★ concurrent-call load test, signs like Vapi (P5)
├── static/  locale/  tests/
```

**Apps:** `core`, `voice` (new), `kb`, `crm`, `dashboard`. (`chat` from swedish-bot is folded into `voice` — telephony replaces the SSE web-chat channel; the web-chat widget is an EXP item.)

---

## 3. Data plane — voice repo ⇄ budtender HTTP contract

The voice repo NEVER re-implements Dutchie or ranking. `voice/budtender_client.py` is a thin Bearer client to the happytime-budtender service.

- **Base URL:** `HHT_BUDTENDER_BASE_URL` (e.g. `https://budtender.internal`).
- **Auth:** `Authorization: Bearer <HHT_BACKEND_TOKEN>` (constant-time check in budtender `auth.py`; fail-closed).
- **Latency property:** budtender ranks over a small **pre-synced per-store `Product` table** (inventory sync every 10 min via Celery), NOT a live Dutchie call → fast enough for a voice turn.

| Tool (Vapi) | budtender endpoint | Request (key fields) | Response (leak-safe) |
|---|---|---|---|
| `suggest_products` | `POST /api/v1/products/search/` | `{slots:{store,category,subcategory,size,price_tier,effect_desired,doh_only}, limit, exclude_skus, session_token}` | `{results:[{rank, name, brand, category, size, price_otd, image_url, why_this, sku}…≤3]}` — **no cost/margin** |
| `check_inventory` | `POST /api/v1/products/search/` (sku-scoped) or `products/in-stock/` | `{store, sku}` | `{in_stock:bool, qty_band, price_otd}` |
| `pair_upsell` | `POST /api/v1/pairing/for-sku` | `{store, anchor_sku, session_token}` | `{pair:{…public_product}|null, strength, reason_text}` — surface only if `strength` clears gate |
| returning-caller | `POST /api/v1/chat/resume-by-phone` | `{phone_hash}` | `{profile_summary, recent_brands, recent_cats}` (drives `W_KNOWN`) |
| facets (slot options) | `GET /api/v1/products/{subtypes,sizes,price-bands,doh-options}` | `?store=` | data-driven slot choices |

**Ranking contract (budtender owns it; the voice repo only consumes order):**
- Anonymous → `W_ANON = {margin 0.55, affinity 0.0, effect 0.18, bucket 0.12, budget 0.10, category 0.05}` → **high margin first**.
- Known → `W_KNOWN = {margin 0.22, affinity 0.34, effect 0.10, quality 0.14, bucket 0.12, …}` → **taste-first** from real history.
- Final order is intentional: #1 highest gross-margin $, #2 highest velocity, #3+ real demand with a brand-variety penalty. `_why()` builds the speakable line from real signals only.
- Pairing: ONE complement, hard price gate ≤50% of anchor (`MAX_PAIR_PRICE_RATIO=0.50`), `strength∈[0,1]` gates whether the upsell is offered at all.

**Leak safety (binding):** budtender `serializers.public_product` builds the dict from an explicit `PUBLIC_PRODUCT_FIELDS` allowlist; `cost`/`margin` are never referenced. The voice repo adds a defensive contract test asserting no "cost"/"margin" substring in any tool response → the agent **physically cannot speak cost/margin**.

---

## 4. KB plane — Django `kb/` canonical + Vapi Files mirror

- **Canonical store = Django `kb/` models** (`FAQEntry`, `PolicyDocument`, `StoreFact`) — dashboard-editable, edits live on the next call (no redeploy). This is path (2), the canonical one.
- **Retrieval:** swedish-bot's **embeddings engine** — Gemini `embed()` (768-dim Matryoshka, `RETRIEVAL_DOCUMENT` for stored chunks / `RETRIEVAL_QUERY` for the caller's query) + `kb/semantic.py` cached in-memory cosine (content-hash keyed cache, self-invalidating on edit). **pgvector swap-seam documented** in `kb/semantic.py` ("swap past a few thousand rows") — not needed at this scale.
- **Mirror to Vapi (path 1, fast fallback):** `kb/vapi_files.py` pushes curated content to **Vapi Files → a Query Tool** (Gemini retrieval, <300KB/file) attached to the `faq` assistant. The dashboard "reindex" button re-mirrors.
- **Seed set (ALL):** FAQ; return policy incl WAC 314-55-079 defective-product exception; store-facts (3 stores, hours, phones, email); WA purchase limits; the FULL weights+types taxonomy; `happytimeweed.com/education`; blog posts.
- **Numbers-Guard:** the LLM never originates a figure — limits/prices/hours come from KB rows; the model only phrases them.

---

## 5. Control plane — dashboard → Vapi REST publish

- **`core/services/vapi.py`** wraps the documented CRUD: base `https://api.vapi.ai`, `Authorization: Bearer <VAPI_PRIVATE_KEY>`, methods for `POST/GET/PATCH/DELETE` on `/assistant`, `/squad`, `/tool`, `/phone-number`. **Never** touches `/workflow` (undocumented/beta).
- **`tools/provision_vapi.py`** — idempotent, re-runnable: ensures the Squad, the 5 assistants, the tool set, and the phone number exist; stores each Vapi `assistantId`/`squadId`/`toolId` on the corresponding local row; re-running produces zero drift (GET-then-PATCH, never blind POST).
- **Dashboard "Publish to Vapi"** — maps each edited `AgentPrompt` → `PATCH /assistant/{id}` (system prompt, model, voice, attached `toolIds`, `transferPlan` number) and the squad shape → `PATCH /squad/{id}` (`members`, `assistantDestinations`).
- **`_clean_graph` fail-closed boundary** (ported from swedish-bot `dashboard/views.py`): MAX_NODES=80, role allowlist, coord clamp, char caps. The flow canvas is **config + docs only**; safety guardrails live in version-controlled `voice/guardrails.py` and **cannot be deleted from the UI**.

---

## 6. Sequence diagrams (prose/ASCII)

### 6.1 Inbound FAQ call

```
Caller dials inbound number
  → Vapi routes to Squad → entry_router assistant
  → "Happy Time, this is Koptza! Are you 21 or older?"  → caller: "yes, what time do you close?"
  → entry_router classifies intent = FAQ → handoff to faq member
  → faq calls tool faq_lookup{query:"closing hours", store:?}
      → POST /api/voice/vapi  (HMAC-verified, fail-closed)
      → voice/tools/faq.py reads kb/ (StoreFact + embeddings cosine) → returns grounded answer
  → faq speaks: "Our Yakima store is open till 10 tonight."  (from KB, not hallucinated)
  → caller: "thanks, bye"
  → status-update / end-of-call-report → voice/webhooks.py
      → VoiceCall row written (transcript, outcome=faq_answered)
      → crm/sinks.py EmailSink → happytimeyak509@gmail.com  (per-call digest)
```

### 6.2 Product-suggestion call (incl. returning-caller personalization)

```
Caller dials → entry_router → "are you 21+?" yes → "I'm looking for something to help me sleep, under $40"
  → intent = retail → handoff to budtender member
  → [personalization] entry_router/budtender has caller number
      → voice computes peppered phone-hash (PHONE_HASH_PEPPER)
      → tool resume_by_phone → budtender /chat/resume-by-phone{phone_hash}
          → KNOWN caller: profile + recent_brands/cats → ranking will use W_KNOWN (taste-first)
          → UNKNOWN caller: no profile → ranking uses W_ANON (margin-first, HIGH MARGIN priority)
  → budtender slot-fills: effect=sleep, budget<=40, category inferred (indica flower / edible)
  → tool suggest_products{store, effect_desired:"sleep", price_tier:"<=40", ...}
      → POST /api/voice/vapi → voice/tools/suggest.py → budtender_client → /api/v1/products/search/
      → budtender ranks (W_KNOWN if known else W_ANON) → ≤3 in-stock, leak-safe picks + why_this
      → response carries price_otd (out-the-door); NO cost/margin
  → budtender speaks pick #1 + why_this ("Indica-dominant, customers love it for sleep, $38 out the door")
  → caller: "I'll take it"  → tool pair_upsell{anchor_sku}
      → budtender /pairing/for-sku → ONE complement, strength gate, <=50% anchor price
      → if strength clears gate: "Want to add a 10-pack of sleep gummies for $12 out the door?"
  → checkout summary → end-of-call-report → VoiceCall(outcome=suggested, SKUs) + email
```

### 6.3 Vendor call (transfer → no answer → callback)

```
Caller dials → entry_router → "Hi, I'm dropping off a delivery / I'm a vendor / here's a manifest"
  → entry_router classifies intent = VENDOR (never retail) → handoff to vendor member
  → vendor: warm transferCall to the store human (per-location env number)
      → IF a human answers: warm transfer completes (with summaryPlan {{transcript}}). Done.
      → IF NO ANSWER (transfer fails/times out): control RETURNS to the vendor AI member
  → vendor AI: "I couldn't reach the team right now — can you tell me what you're calling about?"
      → caller explains (delivery / wholesale order / manifest correction)
  → tool notify_vendor_callback{store, caller_phone_hash, reason, summary}  (async)
      → POST /api/voice/vapi → voice/tools/vendor.py
          → crm/models.VendorCallback row created (idempotent)
          → crm/sinks.py email/alert to staff (immediate, outcome=vendor)
          → optional n8n/Slack secondary sink
      → returns a callback window string
  → vendor AI: "Got it — someone will call you back within [window]. Thanks!"
  → end-of-call-report → VoiceCall(outcome=vendor_callback) + immediate staff alert
```

### 6.4 Escalation call (defective return / repeated human request / dispute)

```
Caller dials → entry_router → "my vape cart is defective and I want a refund"  (OR asks for a human 2x)
  → trigger met: defective product (OR >=2 human requests OR return dispute)
  → handoff to escalation member  (REAL inbound transition — the export's orphan, fixed)
  → escalation: de-escalation script + WA defective-product path from KB
      "I'm sorry that happened. Under WAC 314-55-079, a defective product can be exchanged —
       bring the original packaging with a legible lot ID and your receipt. Let me get a manager on."
  → warm transferCall:
      transferPlan.mode = "warm-transfer-wait-for-operator"
      summaryPlan injects {{transcript}}  → the operator hears the context before connecting
      destination = per-location number (env placeholder, O-4)
  → IF operator connects: warm transfer completes.
  → end-of-call-report → VoiceCall(outcome=escalation, reason=defective_return)
      → crm/sinks.py IMMEDIATE alert email to happytimeyak509@gmail.com (+ per-store)
```

---

## 7. Security architecture (cross-cutting)

- **Every Vapi webhook** to `/api/voice/vapi` is HMAC/secret-verified in `core/middleware.py` (constant-time `hmac.compare_digest`) and **fails closed** on a missing/bad signature.
- **budtender Bearer token** (`HHT_BACKEND_TOKEN`) is constant-time checked in budtender `auth.py`, fail-closed.
- **Per-store Dutchie keys live ONLY in budtender** — never in the voice repo's env or code.
- **Prod-fail-closed settings** (ported from swedish-bot): when `DEBUG=0`, the app refuses to boot on a default secret or an unsafe config.
- **PII discipline:** caller numbers are stored only as peppered hashes (`PHONE_HASH_PEPPER` ≠ `SECRET_KEY`); raw numbers are not persisted.
- **Leak-safety** (cost/margin) enforced at budtender's serializer AND re-asserted by a voice-repo contract test.

---

## 8. What is net-new vs ported (so agents don't re-invent)

| Net-new (write) | Ported verbatim/near-verbatim |
|---|---|
| `core/services/vapi.py` (REST client) | `core/services/gemini.py`, `core/constants.py` |
| `voice/` app entirely (webhooks, tools, budtender_client, models, summarize) | `config/settings.py`, Docker/Caddy/Makefile, prod-fail-closed |
| `kb/seed.py`, `kb/vapi_files.py` | `kb/models.py` (AgentPrompt/FlowConfig/FAQEntry/PolicyDocument), `kb/semantic.py`, `kb/ingest.py` |
| `crm/models.VendorCallback` | `crm/sinks.py` (EmailSink/dispatch), `crm/models.Caller` (phone-hash), `crm/profile.py` |
| dashboard expansions (weights tuner, call monitor, callback queue, Publish-to-Vapi) | `dashboard/views.py` (flow_canvas/_clean_graph/agent_prompt_assist), `templates/dashboard/flow.html` |
| `voice/guardrails.py` (voice-specific) | `chat/guardrails.py` pattern |
| `tools/provision_vapi.py` | — |
| budtender HTTP integration | **budtender itself** reused unchanged as a separate service |

**P1 net-new (built 2026-06-22):** `voice/budtender_client.py` (the thin pooled Bearer client — `search`/`check_sku`/`pair_for_sku`/`resume_by_phone`/`persist_session`/`facets_*`/`health`, graceful-empty on every method), `voice/recognition.py` (`resolve_caller` + the peppered phone-hash re-export; ADR-022 Option A — sends normalized phone to budtender, persists only the hash), `voice/pricing.py` (`otd(price, store)` per-store OTD uplift; TODO-B1 fallback), `voice/tools/suggest.py` (the three handlers + `_speakable_pick` leak wall + `PAIR_STRENGTH_GATE = 0.40`). `provision.py` extended to reconcile the seeded P1 members (`entry_router`/`budtender`) + their tools + the `entry_router →(retail)→ budtender` squad edge; `kb/seed.py` seeds the `entry_router` + `budtender` `AgentPrompt` rows. The only P0-shared edits: a one-line `from . import suggest` in `voice/tools/__init__.py` and a transient `caller_number` on the `tool-calls` ctx in `voice/webhooks.py` (never persisted — only the hash is).

**P4-EXPANSIONS net-new (built 2026-06-22):** the ranking-weights lever reaches budtender PER REQUEST — `voice/budtender_client.search()` forwards the owner-tuned `RankingWeights` singleton as a `ranking_weights={w_anon,w_known,margin_emphasis}` config on every `products/search/` call (`dashboard/models.RankingWeights.as_request_config()`), OMITTED while `is_default()` (zero behavior change until a lever is tuned; P1 goldens byte-identical) and fail-safe (a weights-read error never crashes a turn). budtender owns the ranking — TODO-BUDTENDER: it must read a `ranking_weights` request param (today `ranking.score` reads its module-level `W_ANON`/`W_KNOWN`). Plus a dedicated specials/hours editor (`dash-specials-hours`, the `faq`-spoken `StoreFact` subset with the O-8 confirm gate; CRUD reuses the shared kb-row editor) and an analytics top-product-asks rollup (a real count over `VoiceCall.suggested_skus`, leak-safe). No model migration — pure methods on the P4-core `RankingWeights`.

**P3 net-new (built 2026-06-23):** `voice/tools/vendor.py` (`notify_vendor_callback` — idempotent `VendorCallback` write on `vapi_call_id`, the `VoiceCall.outcome=vendor_callback`/`reason=vendor` stamp, the immediate `crm.sinks.dispatch` staff alert, and the §4.2 envelope with the config callback window), `voice/vendor_flow.py` (the pure `is_no_answer`/`normalize_reason`/`callback_window_text` helpers), `voice/routing.py` (the code-owned `entry_router` intent classifier matrix — VENDOR-before-RETAIL precedence, the contract the `AgentPrompt` few-shots teach), and `crm/models.VendorCallback` + `VendorCallbackStatus` (durable B2B record, migration `crm/0003`; PII = peppered hash only, no product/cost field — Leak-safe). `kb/seed.py` seeds the `vendor` `AgentPrompt` (ADR-015 flow) + the `kind="vendor"` StoreFact rows; `voice/constants.py` already carried the `vendor` member tools / transfer key / squad edges (`entry_router→vendor`, `vendor→escalation`) — P3 added `P3_MEMBER_ROLES=("vendor",)` to `provision.EXTRA_MEMBER_ROLES` so the `vendor` member provisions. The only P0-shared edit: a one-line `from . import vendor` in `voice/tools/__init__.py` (ADR-020 parallel-safety). New env (settings + §3.5): `HHT_VENDOR_CALLBACK_WINDOW`, `VENDOR_CALLBACK_WEBHOOK_URL`.

---

## 9. Open architectural placeholders (owner-supplied; do NOT block)

These are **env placeholders** (see `02-DECISIONS.md` + `03-CONVENTIONS.md`), not blockers:
- `VAPI_PHONE_NUMBER_ID` / inbound number(s) — one fronting 3 stores (intent-route) or one per store (O-4).
- `HHT_TRANSFER_NUMBER_{YAKIMA,MTVERNON,PULLMAN}` — per-location transfer destinations (O-4).
- `HHT_BUDTENDER_BASE_URL` + confirmation budtender is deployed with current per-store Dutchie keys (O-1).
- Brand hex/fonts/logo (P5, O-10).
- Mt Vernon hours conflict (O-8) — seed KB only after owner confirms.
- `SLACK_WEBHOOK_URL` (optional secondary sink, O-9).
