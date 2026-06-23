# 00 — MASTER ROADMAP — Happy Time Weed Voice-Agent Stack Rebuild

> **Status:** FOUNDATION (authoritative). Written 2026-06-22.
> **Repo:** `C:\happytime-voice` (off OneDrive, new Django service).
> **Read order for every session:** this file → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → the relevant phase doc (`10`–`16`) → its referenced cross-cutting spec (`20`–`24`).
> **★ Doc status (2026-06-22 — ALL authored):** the foundation (`00`–`03`), every phase doc (`10-P0` · `11-P1` · `12-P2` · `13-P3` · `14-P4` · `15-P5` · `16-CAPABILITY-EXPANSIONS`), and every cross-cutting spec (`20-SPEC-vapi-deploy` · `21-SPEC-budtender-contract` · `22-SPEC-kb-seed` · `23-SPEC-security-guardrails` · `24-SPEC-testing`) now EXIST on disk. The plan-review (`99-PLAN-REVIEW.md`) gap list G-1…G-12 is resolved. A fresh session may follow the read-order without hitting a missing file.
> **Source-of-truth brief (merged research):** the synthesis brief consolidated from the Vapi export, swedish-bot, happytime-budtender, and web/brand research. Every claim below traces to a real file path (see §10).

---

## 1. Program in one paragraph

Rebuild the Happy Time Weed inbound phone agent. The legacy artifact is a **Vapi Workflow export** (`C:\Users\vladi\Downloads\happy-time-voice-agent-(full-script)-(uploaded-via-json).json`, 3,960 lines, 53 nodes) whose *conversational design is excellent* but whose *wiring is broken*: tools are named in prose but never bound (no `toolIds`, no `server.url` anywhere), escalation is an orphan node with empty transfer destinations, there is no FAQ/return-policy knowledge, no Dutchie inventory source, no vendor path, the config is duplicated ~51× per node, and two conflicting models are declared. Vapi itself now deprecates Workflows ("Prefer Assistants or Squads"). We rebuild as **Assistants + ONE Squad**, code-defined and idempotently provisionable via the documented Vapi REST API, forking the **swedish-bot** Django chassis (lean settings, Docker+Caddy, uv, `core/services/gemini.py` lifted verbatim) and reusing **happytime-budtender** as a separate HTTP microservice for all Dutchie/ranking work. Product suggestions run through budtender's margin-first / taste-first ranking engine, personalized for returning callers via a peppered phone-hash. A full operator dashboard (ported from swedish-bot and expanded) edits prompts/KB/weights and publishes to Vapi.

---

## 2. The 7 subsystems

| # | Subsystem | What it is | Primary new code | Reuses |
|---|---|---|---|---|
| **S1** | **Chassis + FAQ** | Django service (settings, Docker, Caddy, healthz, Vapi REST client), KB seeded with FAQ/return-policy/store-facts/WA-law, one entry+FAQ assistant grounded via `faq_lookup` tool, durable call log. | `config/`, `core/services/vapi.py`, `voice/webhooks.py`, `voice/tools.py` (faq), `kb/seed.py`, `voice/models.py` | swedish-bot `config/settings.py`, `core/services/gemini.py`, `kb/`, `crm/sinks.py` |
| **S2** | **Dutchie suggestions** | `suggest_products` / `check_inventory` / `pair_upsell` Vapi tools → budtender over HTTP; margin-first (anon) / taste-first (known) ranking; returning-caller personalization via phone-hash; leak-safe (cost/margin never spoken); one gated upsell. | `voice/budtender_client.py`, `voice/tools.py` (3 handlers), `budtender` Squad member | budtender `ranking.py`, `pairing.py`, `serializers.py`, `dutchie.py`, `facets.py`, `auth.py`; swedish-bot `crm` phone-hash |
| **S3** | **Escalation + transfer + email** | `escalation` Squad member with REAL inbound transitions (fix the orphan), warm `transferCall` + `summaryPlan({{transcript}})` to per-location numbers; `end-of-call-report` webhook → durable `VoiceCall` → email sink. | `voice/webhooks.py` (eocr handler), `crm/sinks.py` (email), escalation assistant | swedish-bot fail-closed-to-escalate, `crm/sinks.py` EmailSink |
| **S4** | **Vendor routing** | `entry_router` intent classifier detects vendor/wholesale/delivery/manifest; `vendor` member; warm transfer to store human → on NO ANSWER return to AI → ask reason → `notify_vendor_callback` async tool logs `VendorCallback` + alerts staff + states callback window. Never enters retail. | `voice/tools.py` (`notify_vendor_callback`), `vendor` assistant, `crm/models.VendorCallback` | swedish-bot `crm` models/sinks |
| **S5** | **Dashboard + publish** | Full swedish-bot dashboard (agents editor, flow canvas, KB manager, prompt-assist, CRM) **expanded**: ranking-weights tuner, live call monitor + log, vendor-callback queue, escalation review, KB-source manager + reindex button, specials/hours editor, analytics, **"Publish to Vapi"** (PATCH assistant/squad). Canvas is config+docs only; guardrails stay in Python (`_clean_graph` fail-closed). | `dashboard/views.py` (expanded), `templates/dashboard/flow.html` | swedish-bot `dashboard/`, `templates/dashboard/`, `core/services/vapi.py` |
| **S6** | **Polish + brand** | Cartridge entry from router, back-edge/correction handling, persona "Koptza" final pass, brand hex/fonts/logo (manual browser capture), latency tuning, optional move of post-call work to a queue. | theming, prompt polish | Vapi export voice/persona blocks |
| **S7** | **Capability expansions** | Post-P5 backlog: pgvector swap for KB at scale, SMS follow-up, web-chat fallback (reuse swedish-bot widget), multi-number per-store routing, analytics deepening. | TBD per expansion | swedish-bot `static/widget/`, `kb/semantic.py` pgvector seam |

---

## 3. Phased build order (smallest valuable slice first)

Each phase = one `1X-*.md` executable spec doc keyed off this roadmap. **Every phase ships a real, callable deliverable.**

| Phase | Subsystem(s) | Deliverable (a real inbound call does…) | Fixes export weaknesses |
|---|---|---|---|
| **P0 — Chassis + grounded FAQ** | S1 | Caller asks hours/specials/returns/payment/pickup → answered, grounded in KB, durable call log written. Proves the webhook + HMAC contract. | #1 (tools bound), #5 (FAQ exists), #9 (durable log) for FAQ surface |
| **P1 — Dutchie suggestion path** | S2 | Caller asks "recommend an indica for sleep under $40" → ≤3 real, in-stock, leak-safe picks spoken with `why_this` + one gated upsell. Returning caller (recognized by phone-hash) gets personalized + high-margin recs. | #1, #2 (the core blocker), product expertise made real |
| **P2 — Escalation + transfer + email** | S3 | "Let me talk to a human" (×2) / return dispute / defective return → warm transfer to a person with summary; staff get an email per call, immediate alert on escalation/vendor/defective. | #3 (dead escalation), #9 (durable record), #10 (age/ID, defective path) |
| **P3 — Vendor routing** | S4 | Vendor/delivery/manifest caller detected at entry → warm transfer to store → on no-answer back to AI → reason captured → `VendorCallback` logged + staff alerted + callback window stated. Never dropped into retail. | #6 (no vendor path) |
| **P4 — Dashboard + Publish to Vapi** | S5 | Owner edits prompts/model/voice/transfer-numbers/KB/weights in the dashboard, clicks "Publish to Vapi", reviews calls + vendor-callback queue + escalations. | #7 (config dup), #8 (single model), #11 (hydrated vars) by construction |
| **P5 — Polish + brand** | S6 | Cartridge entry from router; "actually make it edibles" mid-flow works; persona/brand applied; latency acceptable under load. | #4 (cartridge entry), #12 (back-edges) |
| **EXP — Expansions** | S7 | Backlog, scheduled per owner priority. | — |

---

## 4. Dependency graph

```
                         ┌─────────────────────────────────────────────┐
                         │  P0  Chassis + FAQ                           │
                         │  (config, gemini, vapi REST client, kb,      │
                         │   voice webhook+HMAC, faq_lookup tool,       │
                         │   VoiceCall log, entry+FAQ assistant)        │
                         └───────────────┬─────────────────────────────┘
                                         │  (webhook contract + Squad scaffold + vapi.py)
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
  ┌────────────────────┐    ┌────────────────────────┐   ┌──────────────────────┐
  │ P1 Dutchie suggest │    │ P2 Escalation+transfer │   │ P3 Vendor routing    │
  │ (budtender client, │    │ (escalation member,    │   │ (entry_router intent,│
  │  3 tools, budtender│    │  warm transfer+summary,│   │  vendor member,      │
  │  Squad member)     │    │  eocr→VoiceCall→email) │   │  notify_vendor_cb)   │
  └─────────┬──────────┘    └───────────┬────────────┘   └──────────┬───────────┘
            │                           │                            │
            │  needs budtender service  │ needs VoiceCall + sinks    │ needs entry_router classifier
            │  (O-1) up; phone-hash     │ (P0) + transfer #s (O-4)   │ + transfer #s (O-4)
            └───────────────┬───────────┴────────────────────────────┘
                            ▼
                ┌────────────────────────────────────────────────┐
                │  P4  Dashboard + Publish to Vapi                │
                │  (depends on ALL assistants/tools existing as   │
                │   code so the editor has rows to PATCH)         │
                └───────────────────────┬────────────────────────┘
                                        ▼
                ┌────────────────────────────────────────────────┐
                │  P5  Polish + brand   →   EXP  Expansions       │
                └────────────────────────────────────────────────┘
```

**Hard dependencies (must exist first):**
- P1, P2, P3 **all** depend on P0 (the webhook contract, HMAC middleware, the Vapi REST client `core/services/vapi.py`, the Squad scaffold, and the `VoiceCall` durable-log model).
- P1 additionally depends on the **happytime-budtender** service being reachable (Open Decision O-1: deploy location + current per-store keys) and on swedish-bot's **peppered phone-hash** being ported (returning-caller recognition).
- P2 and P3 both depend on the **per-location transfer numbers** (O-4) — but they are **env placeholders**, so the code can ship and be tested with a stub number; do not block.
- P4 depends on **all** assistant/tool/squad definitions existing as code (so the editor has real rows to map to `PATCH /assistant/{id}` / `PATCH /squad/{id}`).
- P5 depends on P0–P4 being live.

**Soft / parallelizable:** P1, P2, P3 share only the P0 surface; they touch **different files** (P1=`budtender_client.py`+`tools.py` suggest handlers; P2=`webhooks.py` eocr + `crm/sinks.py` + escalation prompt; P3=`tools.py` vendor handler + entry_router classifier prompt). They are the prime candidates for parallel worktrees (§7).

---

## 5. Success criteria per phase

Concrete, testable. Each phase doc restates these as acceptance criteria with exact assertions.

**P0 — Chassis + FAQ**
- `make` builds; `docker compose up` starts; `GET /healthz` returns 200 with DB + Gemini + Vapi-auth status keys.
- A provisioning script run (idempotent) creates/updates exactly one Squad + the entry+FAQ assistant + the `faq_lookup` tool + (placeholder) phone number; re-running it produces **zero** drift (no duplicate Vapi objects).
- Every inbound Vapi webhook is HMAC/secret-verified and **fails closed** on a bad/missing signature (constant-time compare).
- A real test call: "what are your hours / do you take cards / can I return a vape that died?" → answered from KB content (Yakima/Mt Vernon/Pullman facts, cash/debit+ATM, WAC 314-55-079 defective exception). No hallucinated facts.
- A `VoiceCall` row is written for the call with transcript + outcome; `kb/` is dashboard-editable and the next call reflects an edit with no redeploy.

**P1 — Dutchie suggestions**
- budtender reachable over HTTP with Bearer `HHT_BACKEND_TOKEN`; `/health/` 200.
- `suggest_products` returns ≤3 picks, each in-stock (budtender purchasability gate), each with a non-empty speakable `why_this`. **No response field ever contains "cost" or "margin"** (contract test against the allowlist serializer).
- Anonymous caller → margin-first order (`W_ANON`, margin 0.55); recognized caller (phone-hash hit → budtender profile) → taste-first order (`W_KNOWN`, affinity 0.34) drawn from their real Dutchie purchase history.
- `pair_upsell` offers exactly one complement, only when `strength` clears the gate and price ≤50% of anchor; otherwise silent.
- Agent quotes **out-the-door (tax-included)** prices when it quotes a price at all.

**P2 — Escalation + transfer + email**
- `escalation` has real inbound transitions from `entry_router`, `budtender`, `faq`; triggers on (≥2 explicit human requests) OR (return dispute) OR (defective product).
- Warm `transferCall` uses `transferPlan` warm-transfer-wait-for-operator + `summaryPlan` that injects `{{transcript}}`; destination = the per-location env number.
- `end-of-call-report` → a durable `VoiceCall` row (never silently dropped); an email to `happytimeyak509@gmail.com` (+ per-store env) fires every call, with an **immediate** alert on escalation/vendor/defective-return outcomes.

**P3 — Vendor routing** — ✅ BUILT 2026-06-23 (245 tests green, +80 P3; ruff clean; `makemigrations --check` exit 0)
- A vendor/wholesale/delivery/manifest opener is classified at `entry_router` and routed to `vendor` (never the retail budtender flow). [`voice/routing.py` code-owned matrix + the `entry_router` `AgentPrompt` few-shots; `test_routing_intent.py`]
- Vendor flow: warm `transferCall` to the store human → on NO ANSWER, control returns to the AI → AI asks them to explain the reason → `notify_vendor_callback` logs a `VendorCallback` + alerts staff + the AI states a callback window. [`voice/tools/vendor.py` + `voice/vendor_flow.py` + `crm.models.VendorCallback` (migration `crm/0003`); the `vendor` Squad member + `entry_router→vendor`/`vendor→escalation` edges provisioned via `voice/provision.py`.]

**P4 — Dashboard + Publish (CORE)** — ✅ BUILT 2026-06-22 (297 tests green, +52 P4; ruff clean; `ruff format --check` clean; `makemigrations --check` exit 0; every dashboard page renders 200 as staff)
- Editing an `AgentPrompt` row + clicking "Publish to Vapi" issues a `PATCH /assistant/{id}` with the new system prompt/model/voice/toolIds/transferPlan and a `PATCH /squad/{id}` for shape; the live assistant reflects it. [`dashboard/publish.py` reuses `voice/provision.py`'s payload builders; GET-then-PATCH; zero-drift on `last_publish_hash`; per-object fail-loud; squad destinations re-asserted from `voice/constants.SQUAD_SHAPE` so the canvas cannot delete a required transition — `test_publish.py`.]
- `_clean_graph` rejects an over-cap / bad-role / out-of-bounds graph (fail-closed); a safety guardrail cannot be deleted from the UI (it lives in `voice/guardrails.py`, version-controlled).
- Operator can view the call log, vendor-callback queue, escalation review, and tune ranking weights (persisted, read by budtender).

**P4 — Dashboard EXPANSIONS** — ✅ BUILT 2026-06-22 (309 tests green, +12; ruff clean; `ruff format --check` clean; `makemigrations --check` exit 0; no model migration — pure methods)
- The ranking-weights lever now reaches budtender PER REQUEST, not only via the admin push: `voice/budtender_client.search()` forwards the owner-tuned singleton as a `ranking_weights={w_anon,w_known,margin_emphasis}` config on every `products/search/` call (`RankingWeights.as_request_config()`). OMITTED while the owner hasn't tuned anything off budtender's baseline (`RankingWeights.is_default()` → zero behavior change, P1 goldens byte-identical); fail-safe (a DB-read failure never crashes a turn). **TODO-BUDTENDER:** budtender must read a `ranking_weights` request param in `products/search/` (`ranking.score` reads its module-level `W_ANON`/`W_KNOWN` today) — until it does, the param is sent + ignored harmlessly. [`voice/budtender_client.py::_ranking_config` + `dashboard/models.RankingWeights`; `test_ranking_weights_wire.py`.]
- Dedicated **specials/hours editor** (`dash-specials-hours`) over the `StoreFact` rows the `faq` assistant speaks (kind∈{special,hours}); surfaces the O-8 Mt Vernon unconfirmed-hours gate ("call to confirm", never spoken as a fact); CRUD reuses the shared kb-row editor (kind=store-fact, one editor). [`dashboard/views.specials_hours` + `templates/dashboard/specials_hours.html`.]
- **Analytics** gains a real **top-product-asks** rollup (a count over the durable `VoiceCall.suggested_skus`, no LLM math, leak-safe — a SKU is an id) + a per-store call breakdown. [`dashboard/views._top_product_asks`; `test_views.py`.]

**P5 — Polish**
- "I want a cart/510/vape" from the router reaches the cartridge branch.
- A mid-flow "actually make it edibles" is handled (back-edge/correction).
- Persona/brand applied; p95 turn latency within target under a small concurrent-call load test.

---

## 6. Multi-agent execution strategy

**Orchestrator (Opus, "big brain")** owns this roadmap + the foundation docs and dispatches **domain agents**, each Opus, one per phase/subsystem. Each domain agent decomposes into **granular sub-agents** (sonnet=moderate logic, haiku=mechanical port). One task per sub-agent; dispatch parallel work in a single message; isolate file-mutating parallel work in **git worktrees**.

**Which phases parallelize:**
- **P0 is serial and first** — it lays the chassis + the webhook contract + `core/services/vapi.py` + the Squad scaffold every later phase imports. Nothing else starts until P0's webhook contract and Vapi REST client land.
- **P1 ∥ P2 ∥ P3 run in parallel** after P0. They touch disjoint files (see §4 "Soft"). Give each its own worktree:
  - `wt-p1-suggest` → `voice/budtender_client.py`, `voice/tools.py::suggest_products/check_inventory/pair_upsell`, `budtender` member.
  - `wt-p2-escalation` → `voice/webhooks.py::end_of_call_report`, `crm/sinks.py`, escalation assistant prompt.
  - `wt-p3-vendor` → `voice/tools.py::notify_vendor_callback`, entry_router classifier prompt, `crm/models.VendorCallback`.
  - **Shared-file hazard:** all three may add a handler to `voice/tools.py`. Mitigate by giving each phase its **own handler module** under `voice/tools/` (e.g. `tools/suggest.py`, `tools/vendor.py`) and a thin dispatch registry in `voice/tools/__init__.py` that P0 ships — so parallel agents append to different files, not the same one.
- **P4 is serial after P1–P3** — the publish editor needs all assistant/tool rows to exist. (You *can* start the dashboard *port* — the read-only views — in parallel during P1–P3, but the "Publish to Vapi" mapping must wait for the final assistant/tool shapes.)
- **P5 is serial last.** EXP items parallelize freely (independent backlog).

**Worktree isolation rule (from the rules):** every file-mutating parallel agent works in its own `git worktree`; agents report structured results up; context handoff via these foundation docs + each phase's `1X-*.md`. **Verify before "done"** (ruff + pytest + a manual call script) with pasted output. Commit only your own files.

---

## 7. Top-level checklist

**Foundation (this pass)**
- [x] `00-MASTER-ROADMAP.md`
- [x] `01-ARCHITECTURE.md`
- [x] `02-DECISIONS.md`
- [x] `03-CONVENTIONS.md`

**Phase specs (one executable doc each — ALL AUTHORED 2026-06-22)**
- [x] `10-P0-CHASSIS-FAQ.md`
- [x] `11-P1-DUTCHIE-SUGGESTIONS.md`
- [x] `12-P2-ESCALATION-TRANSFER-EMAIL.md`
- [x] `13-P3-VENDOR-ROUTING.md`
- [x] `14-P4-dashboard-publish.md`
- [x] `15-P5-polish-brand.md`
- [x] `16-CAPABILITY-EXPANSIONS.md`

**Cross-cutting spec docs (ALL AUTHORED 2026-06-22)**
- [x] `20-SPEC-vapi-deploy.md`
- [x] `21-SPEC-budtender-contract.md`
- [x] `22-SPEC-kb-seed.md`
- [x] `23-SPEC-security-guardrails.md`
- [x] `24-SPEC-testing.md`

**P0 build**
- [ ] Fork swedish-bot `config/` + Docker + Caddy + `Makefile` + `pyproject.toml` into `C:\happytime-voice`.
- [ ] Lift `core/services/gemini.py` + `core/constants.py` verbatim.
- [ ] Write `core/services/vapi.py` (idempotent CRUD: assistant/squad/tool/phone-number).
- [x] Write `voice/webhooks.py` (assistant-request / tool-calls / status-update / end-of-call-report) + HMAC verify (`voice/signing.py`, fail-closed) + `voice/tools/` registry + `faq_lookup` + `voice/guardrails.py` leak wall + `crm/phone_hash`/`sinks`. (2026-06-22)
- [ ] Port `kb/` (slimmed) + seed FAQ / return-policy (WAC 314-55-079) / store-facts (3 stores) / WA-limits / weights-types taxonomy.
- [x] Write `voice/models.py` (`VoiceCall`, `VoiceTurn`, `Outcome`, `VapiObject`); migrations applied; `voice/tests/test_voice.py` green (20). (2026-06-22)
- [ ] Provisioning script (`tools/provision_vapi.py`) — idempotent, re-runnable.
- [ ] entry+FAQ assistant + `faq_lookup` tool live; one real grounded test call passes.

**P1–P5 / EXP** — tracked in their phase docs.

**Cross-cutting (owner-supplied env placeholders — do NOT block on these):**
- [ ] Per-location transfer phone numbers (O-4).
- [ ] Vapi inbound number(s) — one fronting all 3 stores or one per store (O-4).
- [ ] Dutchie POS keys live in budtender, not the voice repo (O-1).
- [ ] Brand hex/fonts/logo (manual capture, P5, O-10).
- [ ] Mt Vernon hours conflict resolved (O-8).
- [ ] Slack webhook (optional secondary sink, O-9).

---

## 8. Risks (program-level)

| Risk | Impact | Mitigation |
|---|---|---|
| budtender service location/keys stale (O-1) | P1 blocked | Treat as env placeholder; ship `budtender_client.py` against a contract + stub; flip when owner confirms. |
| Vapi `/workflow` endpoint is undocumented/beta | dashboard publish could target a moving surface | We **never** touch `/workflow`; only the documented Assistants/Squads/Tools/Phone CRUD. |
| Voice inverts swedish-bot's long-lived SSE into **stateless turn webhooks** | orchestrator assumptions break | Re-use `process_turn()` per-turn but treat each tool-call webhook as stateless; persist state in `VoiceCall`/budtender, not in memory. |
| Cost/margin leak into spoken output | trust + owner rule violation | budtender allowlist serializer (`PUBLIC_PRODUCT_FIELDS`, no cost/margin) + a contract test asserting no "cost"/"margin" substring. |
| Parallel agents collide on `voice/tools.py` | merge pain | P0 ships a `voice/tools/` package + registry; each phase adds its own module (§6). |
| Per-node config duplication creeps back | token bloat + drift | Voice/transcriber/model set **once at assistant level**; a test asserts no per-node duplication. |

---

## 9. Glossary (binding terms)

- **Squad** — one Vapi container of saved Assistants with handoff transitions. Our single runtime surface.
- **Assistant member** — `entry_router`, `budtender`, `faq`, `vendor`, `escalation` (see `01-ARCHITECTURE.md` §1).
- **Tool** — a Vapi custom/function tool with a `server.url` pointing at `POST /api/voice/vapi`; handled in `voice/tools/`.
- **budtender** — the separate happytime-budtender microservice (Dutchie + ranking + leak-safe serializer). The voice repo NEVER re-implements it.
- **Leak-safe** — cost/margin physically cannot appear in any spoken/serialized output (allowlist serializer).
- **OTD** — out-the-door (tax-included) price; the only price the agent speaks.
- **Phone-hash** — peppered SHA-256 of the caller number (`PHONE_HASH_PEPPER` ≠ `SECRET_KEY`) for returning-caller recognition.
- **Publish to Vapi** — dashboard action mapping local `AgentPrompt`/`FlowConfig` rows → `PATCH /assistant/{id}` + `PATCH /squad/{id}`.

---

## 10. Verified source-file anchors

**Vapi export (legacy artifact to fix):** `C:\Users\vladi\Downloads\happy-time-voice-agent-(full-script)-(uploaded-via-json).json`

**swedish-bot chassis (fork this):** `C:\Users\vladi\OneDrive\Desktop\swedish-bot`
- `config/settings.py`, `config/urls.py`, `config/asgi.py`, `config/wsgi.py`
- `core/services/gemini.py` (lift verbatim), `core/constants.py`, `core/middleware.py`, `core/views.py`, `core/urls.py`
- `chat/orchestrator.py`, `chat/views.py`, `chat/guardrails.py`, `chat/context.py`, `chat/prompts.py`
- `kb/models.py` (`AgentPrompt`~L226, `FlowConfig`~L255, `FAQEntry`~L150, `PolicyDocument`~L291, `SiteFAQ`~L347), `kb/ingest.py`, `kb/semantic.py`, `kb/seed_prompts.py`
- `crm/models.py`, `crm/sinks.py` (DBSink/EmailSink/WebhookSink + `dispatch`~L119), `crm/leads.py`, `crm/profile.py`
- `dashboard/views.py` (`flow_canvas`~L492, `flow_save`~L572, `_clean_graph`~L511, `default_flow_graph`~L430, `agent_prompt_assist`~L385, `MAX_NODES=80`~L426), `dashboard/urls.py`, `templates/dashboard/flow.html`
- `docker-compose.prod.yaml`, `Caddyfile.prod`, `DEPLOY.md`, `Makefile`, `pyproject.toml`, `.env.example`, `static/widget/nordland-widget.js`

**happytime-budtender brain (separate HTTP microservice, reuse unchanged):** `C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender`
- `budtender/dutchie.py` (POS REST client, Basic auth, `_is_purchasable`)
- `budtender/ranking.py` (`rank_products`~L465, `W_ANON`~L14, `W_KNOWN`~L15, `_why`~L676, `_affinity_score`~L234)
- `budtender/pairing.py` (`pair_for`~L122, `MAX_PAIR_PRICE_RATIO=0.50`~L35, strength gate)
- `budtender/serializers.py` (`PUBLIC_PRODUCT_FIELDS`~L13, `public_product`~L24 — no cost/margin)
- `budtender/facets.py`, `budtender/views.py`, `budtender/urls.py`, `budtender/auth.py` (Bearer `HHT_BACKEND_TOKEN`), `budtender/tasks.py`, `core/celery.py`
- `budtender/urls.py` endpoints: `products/search/`, `products/in-stock/`, `products/{price-bands,subtypes,sizes,doh-options}`, `pairing/for-sku`, `chat/resume-by-phone`, `customer/profile-upsert`, `analytics/summary`, `health/`
- `.env.example` (Dutchie per-store keys + `HHT_BACKEND_TOKEN`)

**3 stores (binding facts):** Yakima 1315 N 1st St (509) 571-1106 · Mt Vernon 200 Suzanne Ln (360) 488-2923 · Pullman 5602 WA-270 (509) 334-2788 · shared email `happytimeyak509@gmail.com` · Dutchie slug `happytime`.
