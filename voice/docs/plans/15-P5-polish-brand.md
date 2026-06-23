# 15 — P5 — POLISH + BRAND — Happy Time Voice Agent

> **Status:** CODEABLE PARTS DONE (2026-06-22) — cartridge entry (#4), mid-call corrections (#12),
> the gated Celery scaffold (sync fallback), and the brand-theming scaffold (CSS custom properties +
> Koptza tone copy) are built + green (363 passing, +54 P5 tests). **DEFERRED (owner):** the real
> Happy Time hex/fonts/logo capture past the Vercel wall (O-10 — `brand/CAPTURE.md` is the runbook;
> `brand/tokens.json` ships `provisional:true` with the neutral fallback). Latency item #4 is
> already largely satisfied by P1's pooled budtender client + graceful-empty; the load-test driver +
> the analytics-summary surface (P4 already shipped `analytics_dashboard`) are not re-built here.
> Originally: EXECUTABLE PHASE SPEC, written 2026-06-22.
> **Reads from (in order, mandatory):** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → this doc.
> **Implements roadmap:** P5 / subsystem **S6** (roadmap §2, §3, §5).
> **Honors ADRs (binding, never contradicted here):** ADR-002 (Squad, not Workflow), ADR-003 (idempotent code-provisioned), ADR-004 (budtender is a separate HTTP service), ADR-005 (margin-first/taste-first ranking owned by budtender), ADR-008 (leak-safe), ADR-009 (speak OTD), ADR-010 (gpt-4.1-mini assistants), ADR-011 (voice/persona set ONCE per member), ADR-014 (dashboard config + canvas docs-only), ADR-019 (HMAC fail-closed, per-store Dutchie keys only in budtender), ADR-020 (`voice/tools/` package + registry).
> **Open items it consumes as env placeholders (do NOT block):** O-10 brand visuals, O-4 transfer/inbound numbers, O-1 budtender base URL.

---

## 1. Goal & scope

P5 is the **final polish pass** over a fully-live P0–P4 stack. It does **not** add new conversational surfaces or new external integrations; it **sharpens, themes, and hardens** what already ships. Six concrete workstreams, each tracing to a roadmap/export item:

1. **Branding pass (O-10 / roadmap §3 G):** manually capture the Happy Time brand (logo, hex palette, fonts) past the Vercel security checkpoint (which blocks `WebFetch`/`fetch()` — see `_research-education-blogs.md` §Provenance), then (a) theme the **operator dashboard** with those tokens and (b) set the **assistant tone/persona** ("Koptza") final pass at the member level. Brand tokens become a small, version-controlled config the dashboard reads; nothing about brand visuals touches the Vapi runtime except the persona/tone copy.
2. **Cartridge entry directly from `entry_router` (export weakness #4):** a caller who opens with "I want a cart / a 510 / a vape pen" is classified to the **cartridge category up front** and lands in the budtender cartridge slot-flow — never buried under a concentrate sub-branch (the export's only path to `cart_battery`).
3. **Back-edge / correction handling (export weakness #12):** a mid-call "actually, make it edibles" (or any category/slot correction) is honored — the budtender member resets the affected slots and re-routes the category instead of marching forward on the stale graph. The legacy export was strictly-forward; this makes the conversation editable in-flight.
4. **Latency / load tuning:** measure and tune p95 turn latency under a small concurrent-call load; pin the cheap config wins (member-level config dedup already done in P4; here we tune tool timeouts, budtender pre-warm, KB retrieval cache, and Gemini call budgets) and document a target.
5. **Optional move of post-call work to a real queue (Celery), mirroring budtender:** swedish-bot has **no Celery** (post-call work is best-effort inline / cron'd `manage.py` — roadmap §2 caveat). The `end-of-call-report` handler (P2) does durable-write + email + summary inline. P5 optionally moves the **non-critical** post-call work (Gemini call summary, email/Slack dispatch, analytics roll-up) onto a Celery queue so the webhook returns fast and a slow Gemini/SMTP call never stalls the Vapi callback. Mirrors `happytime-budtender/core/celery.py` + `budtender/tasks.py`. **The durable `VoiceCall` write stays synchronous** (never lose the record — ADR-017).
6. **Analytics summary:** a per-period operations summary (calls, outcomes, escalations, vendor-callbacks, suggestion accept-rate, p95 latency) surfaced on the dashboard and emailable, reading the durable `VoiceCall`/`Outcome` rows plus budtender's `/analytics/summary`.

**Out of scope (explicitly):** any new Vapi assistant/tool; any change to budtender (it is reused unchanged — ADR-004); pgvector swap, SMS, web-chat (those are EXP / roadmap S7); resolving the Mt Vernon hours conflict (O-8 — KB content, owner-gated). P5 ships against env placeholders for every owner-unknown.

**Definition of done (the P5 acceptance gate — restated concretely in §7):**
- "I want a cart" from the router reaches the cartridge branch (manual call + unit test on the classifier mapping).
- A mid-flow "actually make it edibles" is handled (manual call + unit test on the correction-reset logic).
- Persona/brand applied: dashboard themed with captured brand tokens; the `entry_router`/budtender persona copy finalized and published to Vapi.
- p95 turn latency within the documented target under a small concurrent-call load test, with pasted numbers.
- (If the Celery option is taken) post-call work runs on the queue; the webhook still returns ≤ the latency budget; the `VoiceCall` row is written even if the queue is down.
- Analytics summary renders on the dashboard and matches a hand-recomputed fixture.

---

## 2. Dependencies (what must exist first)

P5 is **serial last** (roadmap §4: "P5 depends on P0–P4 being live"). Every item below is a hard prerequisite; cite the doc/path that establishes it.

| Needs | Established by | Used in P5 for |
|---|---|---|
| Chassis + webhook contract + HMAC + `VoiceCall`/`VoiceTurn`/`Outcome` durable log | **P0** (`10-P0-CHASSIS-FAQ.md`); `voice/webhooks.py`, `voice/models.py`, `core/middleware.py` | Latency tuning, queue move, analytics all read/extend these. |
| `core/services/vapi.py` REST client + `tools/provision_vapi.py` idempotent provisioner | **P0**; `core/services/vapi.py` (`01-ARCHITECTURE.md` §5) | Re-publishing the persona/tone copy + cartridge-route prompt changes via `PATCH /assistant/{id}`. |
| `entry_router` intent classifier prompt + Squad `assistantDestinations` | **P3** (`13-P3-VENDOR-ROUTING.md`) wired the classifier; **P0** scaffolded `entry_router` | Cartridge-category classification (#4) edits this classifier's category taxonomy. |
| `budtender` member + `suggest_products`/`check_inventory`/`pair_upsell` tools + `voice/budtender_client.py` | **P1** (`11-P1-DUTCHIE-SUGGESTIONS.md`); `voice/tools/suggest.py` | Cartridge slot-flow (#4) and correction-reset (#12) operate inside the budtender member's prompt + slot state; latency tuning targets the budtender HTTP hop. |
| `escalation`/`vendor` members + `end-of-call-report` handler + `crm/sinks.py` (Email/Slack) | **P2/P3** (`12-…`, `13-…`); `voice/webhooks.py::end_of_call_report`, `crm/sinks.py` | The queue move wraps exactly this handler's post-call work. |
| Dashboard ported + "Publish to Vapi" + `_clean_graph` fail-closed | **P4** (`14-P4-DASHBOARD-PUBLISH.md`); `dashboard/views.py`, `templates/dashboard/flow.html` | Brand theming targets the dashboard templates/CSS; analytics summary is a new dashboard view. |
| `AgentPrompt`/`FlowConfig` rows for all 5 members exist as code | **P4** (read-fresh-every-turn config; `kb/models.py`) | Persona copy + cartridge-route changes are `AgentPrompt` edits, then Publish-to-Vapi. |
| budtender service reachable (Bearer `HHT_BACKEND_TOKEN`) + `/analytics/summary` endpoint | budtender repo (`C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender`); `_research-suggestion-engine.md` §5.1 (`POST /analytics/summary`) | Analytics summary merges budtender funnel/merch counts; O-1 placeholder until owner confirms deploy. |

**Soft dependency / sequencing inside P5:** the **brand capture** (manual browser step) gates only the **theming** sub-task and the persona copy review — it does NOT gate cartridge/back-edge/latency/queue/analytics work, which proceed in parallel. Do the manual capture first so theming isn't blocked, but treat its assets as a config drop that lands late if the Vercel wall fights back (fallback: neutral theme + tokens marked `provisional`, ADR O-10 default).

---

## 3. File-by-file task list

Format: **path → responsibility → key functions/shape → port-from (with path)**. New files are marked **★ NEW**; edits to P0–P4 files are marked **EDIT**. Every parallel-mutated tool stays in its own module (ADR-020) — P5 adds **no** new tool, so `voice/tools/` is untouched except a possible read-only analytics helper.

### 3.1 Branding — capture + tokens + dashboard theme + persona

- **`brand/CAPTURE.md`** ★ NEW — the **manual browser-capture runbook** (the Vercel wall blocks automated fetch; `_research-education-blogs.md` §Provenance). Step-by-step: open `https://happytimeweed.com` + `/brands` + a store menu page in a real logged-in browser (or via the claude-in-chrome / computer-use MCP if available to the operator); capture: the **logo** (download the SVG/PNG asset from the page or screenshot + trace), the **primary/secondary/accent hex** (DevTools → Computed styles on header/buttons/links, or an eyedropper on a screenshot), and the **font families** (DevTools → Computed `font-family` on headings + body). Record provenance + capture date. This is a human/owner step — the doc tells them exactly what to grab and where it goes. **Cite:** `_research-education-blogs.md` confirmed-URL table (lines for `/`, `/brands`, `/yakima-menu`).
- **`brand/tokens.json`** ★ NEW — the version-controlled brand config the dashboard reads. Shape:
  ```json
  {
    "provenance": "manual browser capture 2026-06-2X (Vercel wall blocks auto-fetch)",
    "provisional": false,
    "logo": { "svg_path": "static/brand/happytime-logo.svg", "alt": "Happy Time" },
    "colors": {
      "primary":   "#______",   "primary_fg":  "#ffffff",
      "secondary": "#______",   "accent":      "#______",
      "bg":        "#______",   "fg":          "#______",
      "muted":     "#______",   "danger":      "#dc2626", "ok": "#16a34a"
    },
    "fonts": { "heading": "'____', system-ui, sans-serif", "body": "'____', system-ui, sans-serif" }
  }
  ```
  Until the capture lands, ship with `provisional: true` and the neutral swedish-bot palette (so P5 is never blocked on O-10).
- **`static/brand/`** ★ NEW — dropped logo asset(s) (`happytime-logo.svg`/`.png`) + any webfont files (or a documented Google-Fonts `<link>` if the captured fonts are Google-hosted).
- **`dashboard/branding.py`** ★ NEW — `load_brand_tokens()` reads `brand/tokens.json` once (module-level cache), returns a dict; `brand_css_vars()` renders the `:root{ --brand-primary: …; … }` CSS-variable block. Thin, pure, unit-testable. **Port-from:** none new — mirrors swedish-bot's settings-read idioms (`swedish-bot/config/settings.py` env-read pattern).
- **`dashboard/views.py`** EDIT — inject `brand` into the dashboard template context (a `context_processor` or per-view dict). **Do NOT touch** `_clean_graph`, `flow_save`, `agent_prompt_assist`, or the publish path (ADR-014 — guardrails stay; this is presentation only). **Port-from:** `swedish-bot/dashboard/views.py` (the existing `flow_canvas`~L492 context pattern).
- **`templates/dashboard/base.html`** (+ `flow.html`) EDIT — emit `{{ brand_css_vars }}` in `<head>`; swap hard-coded colors/logo for the CSS variables + `{{ brand.logo }}`. Keep Tailwind/Alpine intact. **Port-from:** `swedish-bot/templates/dashboard/flow.html` (the canvas styling to re-token).
- **`kb/seed.py`** EDIT (persona/tone, NOT a new source) — finalize the **"Koptza" persona/tone block** that P0 seeded as the `entry_router` (and budtender) system-prompt preamble: warm, family/community, no-pressure, conservative-on-dosing voice (per `_research-education-blogs.md` §8 house style + ADR-011 persona). This edits the **copy** only; the Cartesia voiceId / Deepgram nova-3 / keyterm list are already member-level constants from P0 (ADR-011) and are **not** duplicated here. **Port-from:** the export's persona/voice blocks (roadmap §10 anchor: the Downloads JSON) for the Koptza tone; `_research-education-blogs.md` §8 for the house behaviors. After editing, the persona reaches Vapi via **Publish-to-Vapi** (`PATCH /assistant/{id}`, P4 path) — not a new mechanism.

### 3.2 Cartridge entry from `entry_router` (export weakness #4)

- **`voice/routing.py`** ★ NEW (or EDIT if P3 created it) — the **server-side category taxonomy** the `entry_router` classifier maps to, single source of truth so the classifier prompt, the budtender slot-flow, and the tests agree. Add a `CARTRIDGE` intent/category with its trigger lexicon: `cart, carts, cartridge, 510, vape pen, vape, disposable, dispo, all-in-one, AIO, pod`. Map it to budtender's category vocabulary — **note (binding):** budtender's `CATEGORY_BY_SLOTKEY` uses `cartridge` as a top-level category (`_research-suggestion-engine.md` §5.2 request `category` enum: `flower|concentrate|cartridge|edible|tincture`), and the marketing_dashboard rules separate **`vape` (reusable 510 cart)** from **`disposable_vape` (all-in-one)** — so the router must (a) classify cartridge **up front** (not under concentrate) and (b) pass `category:"cartridge"` plus, when the caller says "disposable/dispo/AIO," a `subcategory` hint so budtender can distinguish reusable-cart vs disposable without the agent guessing. **Cite:** `_research-suggestion-engine.md` §5.2 (category enum, `subcategory` HARD filter); marketing_dashboard `.claude/rules/bi-calculations.md` "Cartridge ≠ Disposable". **Port-from:** budtender `ranking.py` `CATEGORY_BY_SLOTKEY` / `_SUBTYPE_KEYWORDS` (taxonomy parity — `_research-education-blogs.md` §11 TODO 2 demands KB/recommender vocab stay identical).
- **`kb/seed.py`** / the `entry_router` `AgentPrompt` body EDIT — add the cartridge branch to the classifier's category list with examples ("I want a cart / a 510 / a disposable vape"); the classifier's structured output category enum gains `cartridge` (it routes to the **budtender** member with `category` pre-filled, NOT to a concentrate sub-branch). The budtender member's slot-flow prompt EDIT — when it receives `category:"cartridge"` it opens directly on cartridge slots (size 0.5g/1g, reusable-vs-disposable, effect), skipping the concentrate funnel. **Cite:** export weakness #4 (synthesis brief §1; roadmap §5 P5). **Port-from:** the export's `cart_battery`/cartridge node prompts (Downloads JSON) for the cartridge slot copy.
- **`voice/tools/suggest.py`** EDIT (minimal) — ensure the `suggest_products` arg builder forwards `category:"cartridge"` (+ optional `subcategory`) through to budtender unchanged. This is a pass-through assertion, not new logic (the tool already forwards slots — P1). Add a guard so an opener-classified cartridge call never gets rewritten to `concentrate`. **Cite:** `01-ARCHITECTURE.md` §1.2 cartridge-fix note ("router classifies category up front; budtender does not bury cartridge").

### 3.3 Back-edge / correction handling (export weakness #12)

- **`voice/corrections.py`** ★ NEW — pure functions for **slot-correction detection + reset**, called server-side from the budtender member's tool/turn handling (NOT an LLM-controlled flow — code owns the FSM, swedish-bot pattern). Key functions:
  - `detect_correction(prev_slots, new_user_intent) -> CorrectionPlan | None` — recognizes "actually / wait / no, make it / change to / instead" + a new category/effect/budget/size, returning which slots to **clear** and which to **rewrite**.
  - `apply_correction(slot_state, plan) -> slot_state` — clears downstream slots when the category changes (e.g. category flower→edible clears `subcategory/size/strain_type` because they don't transfer; preserves `effect_desired`/`budget`/`store` which are category-agnostic), so the next `suggest_products` call is internally consistent. Deterministic; unit-testable; no network.
  **Port-from:** swedish-bot `chat/orchestrator.py` (`process_turn` deterministic state machine + structured-JSON-per-turn) — the existing pattern where **code owns slot transitions and the LLM only fills/classifies** (roadmap §2 swedish-bot table). The correction logic is a new code-owned transition, consistent with "LLM never controls flow."
- **budtender member `AgentPrompt` body** EDIT — instruct the model to emit a `correction` signal (a structured field) when the caller revises a prior choice, and to NOT silently continue; the server reads that signal, runs `apply_correction`, and the member resumes slot-filling from the corrected state. The classifier/budtender prompts gain 2–3 few-shot correction examples ("I changed my mind — edibles instead"). **Cite:** export weakness #12 (strictly-forward graph, no back-edges); `01-ARCHITECTURE.md` §1.2 (budtender owns the slot-fill ladder).
- **`voice/webhooks.py`** EDIT (minimal) — when a `tool-calls`/turn payload carries the correction signal, route through `voice/corrections.py` before building the budtender request. Stateless-turn discipline: the corrected slot state is persisted in the budtender session (`/chat/persist/`) / `VoiceCall`, **not** in process memory (roadmap §8 risk: "voice inverts SSE into stateless turn webhooks; persist state in VoiceCall/budtender, not in memory"). **Cite:** `_research-suggestion-engine.md` §5.1 (`POST /chat/persist/`); roadmap §8.

### 3.4 Latency / load tuning

- **`docs/plans/_p5-latency-budget.md`** ★ NEW — the measured baseline + the target + the knobs. Documents the p95 **turn** target (recommend **≤ 1500 ms** server-side handler time for a tool-call webhook, i.e. the time inside `/api/voice/vapi` excluding Vapi/LLM/TTS, which Vapi owns) and the **end-to-end** observation method (Vapi's own call latency report). Lists each tunable + its setting:
  - `HHT_BUDTENDER_TIMEOUT` (env, default 8s — `03-CONVENTIONS.md` §3.4) → tighten to a voice-appropriate budget (recommend 3–4s) with a **fast graceful-empty** path so a slow budtender never holds the turn (budtender returns `[]` safely when cold — `_research-suggestion-engine.md` §7).
  - budtender **pre-warm**: rely on budtender's pre-synced per-store `Product` table (ranks in-memory, not a live Dutchie call — `01-ARCHITECTURE.md` §3 latency property); add a startup/health ping from `voice/budtender_client.py` so the first real call isn't a cold connection.
  - **KB retrieval cache**: confirm `kb/semantic.py` content-hash-keyed in-memory cosine cache is warm (swedish-bot pattern; `01-ARCHITECTURE.md` §4) — the FAQ path must not re-embed on every call.
  - **Gemini call budget**: KB grounding + call summary use `core/services/gemini.py`; on the **turn** path use the cheapest grounding (cached embeddings, no full-doc context unless needed); move the **summary** off the turn path entirely (it's post-call — see §3.5).
  - **HTTP connection reuse**: `voice/budtender_client.py` uses a pooled `requests.Session`/`httpx.Client` (keep-alive) instead of a fresh connection per tool call.
- **`voice/budtender_client.py`** EDIT — pooled session + per-call timeout from `HHT_BUDTENDER_TIMEOUT` + a fast fallback (`return {"results": []}` on timeout, logged, never raised into the turn). **Port-from:** budtender `auth.py` Bearer pattern (constant-time, fail-closed) for the auth header; standard `requests.Session` pooling.
- **`tools/loadtest_voice.py`** ★ NEW — a small concurrent-call simulator that POSTs realistic `tool-calls` payloads (faq_lookup, suggest_products, pair_upsell) to `/api/voice/vapi` at N concurrency with a valid HMAC, records per-request latency, prints p50/p95/p99 + error rate. **Not** a real-telephony test (that's the manual call script); this isolates the **server-side** handler latency the repo owns. **Port-from:** none — a thin `asyncio`/`concurrent.futures` driver; sign payloads with the same HMAC helper `core/middleware.py` uses (reuse, don't reimplement).

### 3.5 Optional: move post-call work to Celery (mirror budtender)

> **Gate this behind a decision flag** so the queue is genuinely optional (roadmap §3 P5: "*optional* move"). Default: inline (P2 behavior) unless `HHT_USE_CELERY=1`.

- **`core/celery.py`** ★ NEW — Celery app wired to the existing Redis (swedish-bot has Redis-free deploy; budtender uses Redis + Celery). **Port-from VERBATIM-ish:** `happytime-budtender/core/celery.py` (the proven app/broker/beat wiring) — adapt the app name + settings module only. Add Redis to `docker-compose.yaml`/`docker-compose.prod.yaml` (a worker service + broker) — **Port-from:** budtender's compose celery/worker service.
- **`voice/tasks.py`** ★ NEW — three idempotent tasks mirroring budtender's `tasks.py` discipline:
  - `summarize_call(voice_call_id)` — Gemini call summary via `voice/summarize.py` → writes back onto the `VoiceCall` row.
  - `dispatch_alerts(voice_call_id)` — fires `crm/sinks.py` Email (+ optional Slack) per ADR-017; **immediate** on escalation/vendor/defective outcomes (those can stay inline OR be a high-priority queue; document the choice).
  - `rollup_analytics(date)` — nightly per-period aggregate (feeds §3.6).
  Each task is **idempotent** (keyed on `voice_call_id`; re-run = no dup email — mirror budtender's idempotent-per-`(request,sink)` sink discipline, `crm/sinks.py`). **Port-from:** `happytime-budtender/budtender/tasks.py` (idempotency + retry/backoff idioms) + swedish-bot `crm/sinks.py` (the sink itself, already ported in P2).
- **`voice/webhooks.py::end_of_call_report`** EDIT — keep the **durable `VoiceCall` write synchronous** (never queue the record — ADR-017 / roadmap "durable record never silently dropped"); enqueue ONLY the summary/email/rollup when `HHT_USE_CELERY=1`, else call them inline exactly as P2 did. The handler returns 200 to Vapi immediately after the synchronous write. **Cite:** ADR-017; `01-ARCHITECTURE.md` §6.1 (eocr → VoiceCall → email).
- **`config/settings.py`** EDIT — add `HHT_USE_CELERY` (default `0`), `CELERY_BROKER_URL` (Redis), worker concurrency env. Prod-fail-closed unchanged. **Cite:** `03-CONVENTIONS.md` §1.2 prod-fail-closed.

### 3.6 Analytics summary

- **`voice/analytics.py`** ★ NEW — `call_summary(start, end, store=None) -> dict` reads the durable `VoiceCall`/`Outcome`/`VoiceTurn` rows (P0 models) + merges budtender `/analytics/summary` (funnel/merch counts — `_research-suggestion-engine.md` §5.1). Metrics: total calls, outcome breakdown (`faq_answered`/`suggested`/`escalation`/`vendor_callback`/`abandoned`), escalation count + reasons (defective_return / repeated_request / dispute), vendor-callback count, **suggestion accept-rate** (from budtender's conversion attribution — `_research-suggestion-engine.md` §3.2), avg/p95 **server-side** turn latency (from a `latency_ms` field stamped on `VoiceTurn`), top categories asked. Pure aggregation; deterministic; unit-testable against a seeded fixture. **Leak-safe:** reads only `public_product`-shaped budtender fields + its own non-PII rows (no cost/margin — ADR-008). **Port-from:** budtender `/analytics/summary` contract (`_research-suggestion-engine.md` §5.1) for the merge; swedish-bot CRM aggregate idioms.
- **`voice/models.py`** EDIT (small, additive) — add `latency_ms` to `VoiceTurn` (stamp the server-side handler time in `voice/webhooks.py`) so p95 is computable from durable rows, not logs. A migration. **Cite:** `03-CONVENTIONS.md` §1.3 (`makemigrations --check` gate).
- **`dashboard/views.py`** EDIT — a new read-only `analytics_summary` view rendering `voice/analytics.call_summary(...)` with a date-range picker + a "email me this" button (reuses `crm/sinks.py` EmailSink). Read-only; no guardrail surface touched. **Port-from:** swedish-bot `dashboard/views.py` view-render pattern.
- **`templates/dashboard/analytics.html`** ★ NEW — the summary page (themed by §3.1 brand tokens, Tailwind/Alpine, no Chart.js dependency required — simple tables + sparkline-optional). **Port-from:** `swedish-bot/templates/dashboard/*` styling.
- **`dashboard/urls.py`** EDIT — route `/<dashboard>/analytics/`.

---

## 4. Data contracts / JSON schemas

### 4.1 `brand/tokens.json` (see §3.1 for the full shape)
Authoritative brand config. **Invariant:** `colors.*` are 6-hex strings; `provisional:true` means the dashboard renders the neutral fallback and shows a "brand not yet captured" badge. No secrets here — purely presentational, committed to the repo.

### 4.2 Cartridge classification contract (router → budtender)
The `entry_router` structured classifier output gains a category value, forwarded to `suggest_products`:
```json
{
  "intent": "retail",
  "category": "cartridge",
  "subcategory": "disposable",   // optional: "disposable"|"cartridge"(reusable 510)|null — only when caller specifies
  "effect_desired": "uplifted",  // if stated
  "size": "1g"                   // 0.5g|1g if stated
}
```
- `category:"cartridge"` is a member of budtender's HARD category enum (`_research-suggestion-engine.md` §5.2: `flower|concentrate|cartridge|edible|tincture`) — **never** rewritten to `concentrate`.
- `subcategory` distinguishes reusable-cart vs all-in-one disposable (marketing_dashboard "Cartridge ≠ Disposable" rule); omitted when the caller didn't specify (budtender's facets pick real in-stock subtypes — `_research-suggestion-engine.md` §6).

### 4.3 Correction signal contract (budtender member → server)
The budtender member emits, on a caller revision, a structured field on its turn/tool payload:
```json
{
  "correction": {
    "kind": "category",                 // category|effect|budget|size|cancel
    "to": "edible",                     // new value (budtender category enum or slot value)
    "raw": "actually make it edibles"   // the caller phrase, for the log
  }
}
```
Server applies `voice/corrections.apply_correction`:
- `kind:"category"` → clear `subcategory,size,strain_type,price_tier` (don't transfer across categories); **keep** `effect_desired,budget,store,phone_hash`.
- `kind:"effect"|"budget"|"size"` → overwrite just that slot.
- `kind:"cancel"` → reset the budtender slot-state to the category-entry stage.
The corrected state persists via budtender `POST /chat/persist/` (`_research-suggestion-engine.md` §5.1) — never in process memory (stateless-turn discipline).

### 4.4 `voice/analytics.call_summary(...)` output
```json
{
  "period": { "start": "2026-06-01", "end": "2026-06-22", "store": "yakima|null" },
  "calls_total": 412,
  "outcomes": { "faq_answered": 180, "suggested": 150, "escalation": 22, "vendor_callback": 18, "abandoned": 42 },
  "escalations": { "defective_return": 9, "repeated_request": 8, "dispute": 5 },
  "vendor_callbacks": 18,
  "suggestion_accept_rate": 0.31,        // from budtender conversion attribution
  "latency_ms": { "p50": 240, "p95": 1180, "p99": 1900 },
  "top_categories": [ {"category":"flower","n":120}, {"category":"cartridge","n":74}, ... ]
}
```
**Leak-safe (ADR-008):** no `cost`/`margin`/`velocity`/`bucket` field is ever present (asserted by the §6 contract test). Phone numbers appear nowhere — only counts (PII discipline, `01-ARCHITECTURE.md` §7).

### 4.5 Load-test output (`tools/loadtest_voice.py`)
Prints (and writes JSON):
```json
{ "concurrency": 10, "n_requests": 500, "tool_mix": {"faq_lookup":0.4,"suggest_products":0.5,"pair_upsell":0.1},
  "latency_ms": {"p50":..,"p95":..,"p99":..}, "errors": 0, "hmac_rejections": 0 }
```

---

## 5. Vapi deploy steps

P5 adds **no** new assistant, tool, squad, or phone number (ADR-002/003). It only **re-publishes copy + classifier taxonomy** on existing members via the documented `PATCH` path P0/P4 already built. Steps:

1. **Persona/tone (Koptza) + cartridge taxonomy + correction few-shots** are edited as `AgentPrompt` rows (P4 read-fresh config) for the `entry_router` and `budtender` members.
2. **Publish to Vapi** (dashboard action or `tools/provision_vapi.py` re-run — idempotent, ADR-003): `PATCH /assistant/{entry_router_id}` (updated system prompt with the cartridge category in the classifier + the correction examples) and `PATCH /assistant/{budtender_id}` (cartridge slot-flow + correction handling). **GET-then-PATCH**, never blind POST → zero drift (ADR-003 acceptance criterion).
3. **No per-node duplication** is introduced (ADR-011) — voice/transcriber/model stay member-level constants; the test in §6 re-asserts no duplication after the re-publish.
4. **No `/workflow` call** (ADR-002). Only `/assistant` PATCH.
5. **Squad shape unchanged** — cartridge routes to the **existing** `budtender` member (the destination already exists from P0/P1); we do not add a Squad member. So `PATCH /squad/{id}` is a no-op unless P4 already manages member ordering.
6. **Re-provision is drift-free:** running `python tools/provision_vapi.py` after the P5 edits produces zero new Vapi objects (only PATCHes) — verify in the provisioning test (§6).

---

## 6. Acceptance criteria (testable, concrete)

Each is a pass/fail gate; numbers/assertions are explicit.

**AC-1 Cartridge entry (#4).**
- Unit: `voice/routing.classify("I want a cart")`, `("got any 510 carts?")`, `("a disposable vape pen")` → all map to `category:"cartridge"` (disposable openers also set `subcategory:"disposable"`), never `concentrate`.
- Unit: the `suggest_products` arg builder forwards `category:"cartridge"` unchanged (no concentrate rewrite).
- Manual call: opener "I want a cart" → the agent goes straight into cartridge slot-filling (size 0.5g/1g, effect), and the spoken picks are cartridges. Paste transcript + the `suggest_products` tool args from the `VoiceCall` log.

**AC-2 Back-edge / correction (#12).**
- Unit: `detect_correction({category:"flower", size:"3.5g", effect_desired:"relaxed", budget:40}, "actually make it edibles")` → `CorrectionPlan(kind="category", to="edible", clear=["subcategory","size","strain_type","price_tier"], keep=["effect_desired","budget","store"])`.
- Unit: `apply_correction` clears the downstream slots and preserves the category-agnostic ones exactly as the plan says.
- Manual call: mid-flow "actually make it edibles" → the next `suggest_products` call carries `category:"edible"` with `effect_desired/budget` preserved and `size/subcategory` reset; the agent does not march forward on flower. Paste transcript + the before/after slot state from `/chat/persist/` or the `VoiceCall` log.

**AC-3 Branding applied.**
- `brand/tokens.json` exists and is non-`provisional` (or, if O-10 still blocked, is `provisional:true` with the capture runbook `brand/CAPTURE.md` complete and the neutral fallback rendering — documented, not a failure).
- Dashboard renders with the captured palette/fonts/logo (CSS variables from `brand_css_vars()`); screenshot before/after.
- The Koptza persona/tone copy is finalized on `entry_router`/`budtender` `AgentPrompt` rows and **published** (a `PATCH /assistant/{id}` issued — assert via the Vapi client mock in the contract test, or the live GET shows the new prompt). No per-node voice/model duplication (re-run the ADR-011 no-dup test → pass).

**AC-4 Latency / load.**
- `tools/loadtest_voice.py` at concurrency 10, ≥500 requests, realistic tool mix → **p95 server-side handler latency ≤ the documented target** (recommend ≤ 1500 ms), **0 HMAC rejections** on valid signatures, error rate 0. Paste the printed p50/p95/p99.
- A slow/unreachable budtender (simulated timeout) does **not** push the turn over budget — the fast-empty fallback returns `{"results": []}` within `HHT_BUDTENDER_TIMEOUT` and the handler still answers. Test asserts the fallback path + a logged warning.

**AC-5 Queue (only if `HHT_USE_CELERY=1`).**
- With the worker up: `end_of_call_report` returns 200 after the **synchronous** `VoiceCall` write; `summarize_call`/`dispatch_alerts`/`rollup_analytics` run on the worker; the email/summary land within a short SLA. Paste the worker log + the resulting `VoiceCall` row (summary populated).
- With the worker **down**: the `VoiceCall` row is still written (durable record never lost — ADR-017); the task is queued/retried; the webhook still returns 200. Test asserts the record exists even when the broker is unavailable.
- Idempotency: re-delivering the same `end-of-call-report` does **not** create a duplicate `VoiceCall` or send a duplicate email (keyed on the Vapi call id / `voice_call_id`).

**AC-6 Analytics summary.**
- `voice/analytics.call_summary(start, end)` on a seeded fixture equals a **hand-authored expected dict** (§4.4 shape) — outcome counts, escalation reasons, accept-rate, p95 all match (test data discipline: expected values hand-authored, not generated by the code under test — `03-CONVENTIONS.md` §5).
- The dashboard analytics page renders the summary and the "email me this" button delivers via `crm/sinks.py` (assert one email queued/sent).

**AC-7 Leak-safety re-asserted (non-negotiable gate — `03-CONVENTIONS.md` §5).**
- Contract test: no `"cost"` / `"margin"` substring in any `suggest_products`/`pair_upsell`/`check_inventory` response **or** in `voice/analytics.call_summary` output. (Re-run the P1 Leak-Guard test plus a new analytics-output assertion.)

**AC-8 HMAC fail-closed re-asserted (non-negotiable gate).**
- The webhook still rejects a missing/bad Vapi signature with 401 before any P5 handler (cartridge/correction/queue) runs; the load-test's malformed-signature requests are all rejected. (Re-run the P0 HMAC test.)

**AC-9 Hygiene.**
- `ruff check` + `ruff format --check` clean; `python manage.py check` clean; `makemigrations --check` exit 0 (the `VoiceTurn.latency_ms` migration is committed); targeted `pytest` green. **Paste all four outputs** (`03-CONVENTIONS.md` §1.3 — never claim passing without pasted output).

---

## 7. Test plan

Mirrors the four planes in `03-CONVENTIONS.md` §5 (Unit · Contract · Provisioning · Manual call). P5 touches a tool path (cartridge forward), the webhook (correction + queue), and produces serialized output (analytics) → the **Leak-Guard** and **HMAC-fail-closed** tests are mandatory gates.

### 7.1 Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network)
- `tests/test_routing_cartridge.py` — cartridge lexicon → `category:"cartridge"` (+ disposable subcategory); never `concentrate`; non-cartridge openers unaffected (no regression to flower/edible classification).
- `tests/test_corrections.py` — `detect_correction` over a table of phrases ("actually edibles", "no, make it a cart", "change my budget to 60", "cancel that"); `apply_correction` slot clear/keep matrix; category-agnostic slots preserved; idempotent (applying twice == once).
- `tests/test_branding.py` — `load_brand_tokens()` parses `tokens.json`; `brand_css_vars()` emits valid `--brand-*` vars; `provisional:true` path renders neutral fallback.
- `tests/test_analytics.py` — `call_summary` over a seeded fixture == hand-authored expected dict (§4.4); empty-period → zeros, never an error.
- `tests/test_budtender_client_timeout.py` — a simulated timeout returns `{"results": []}` within budget, logs a warning, never raises into the turn.

### 7.2 Contract (`pytest -m integration`, budtender stubbed/recorded, Vapi client mocked)
- `tests/test_leak_guard_p5.py` (**mandatory**) — no `"cost"`/`"margin"` substring in any tool response **or** in `call_summary` output (ADR-008 / AC-7).
- `tests/test_hmac_fail_closed_p5.py` (**mandatory**) — bad/missing signature → 401 before the cartridge/correction/queue handlers; valid signature passes (AC-8).
- `tests/test_cartridge_forward.py` — given a `tool-calls` payload classified cartridge, `voice/tools/suggest.py` forwards `category:"cartridge"` to the (stubbed) budtender `/products/search/` unchanged.
- `tests/test_correction_webhook.py` — a `tool-calls` payload carrying the §4.3 `correction` block routes through `apply_correction` and persists the corrected slots (stubbed `/chat/persist/`).
- `tests/test_publish_no_dup.py` — after the P5 `AgentPrompt` edits, the mocked Publish-to-Vapi issues only `PATCH` (no POST) and introduces no per-node voice/model duplication (ADR-011 re-assert).
- `tests/test_eocr_queue.py` (if `HHT_USE_CELERY=1`) — synchronous `VoiceCall` write happens even with the broker down (eager-mode + broker-down simulation); post-call tasks idempotent; no duplicate email on re-delivery (AC-5).

### 7.3 Provisioning (`python tools/provision_vapi.py --dry-run` then live against a sandbox key)
- Re-running provisioning after the P5 prompt edits yields **zero new Vapi objects** (only PATCHes) — drift-free (ADR-003). Paste the dry-run diff (PATCH-only).

### 7.4 Load (`python tools/loadtest_voice.py`)
- Concurrency 10, ≥500 requests, tool mix 40% faq / 50% suggest / 10% pair, valid HMAC → p50/p95/p99 + error rate + hmac_rejections. Gate: p95 ≤ target, 0 errors, 0 false HMAC rejections (AC-4). Re-run with a budtender-timeout fault injected → fallback path holds the budget.

### 7.5 Manual call script (the per-phase definition of done — `03-CONVENTIONS.md` §5)
Dial `VAPI_PHONE_NUMBER_ID` (O-4 placeholder; use the provisioned test number) and run, pasting transcript + the resulting `VoiceCall` row for each:
1. **Cartridge opener:** "Hi — I'm looking for a cart." → agent confirms 21+, goes straight into cartridge slots, speaks ≤3 in-stock cartridge picks with `why_this` + OTD price (AC-1).
2. **Correction mid-flow:** start a flower request, then "actually, make it edibles instead." → agent switches to edibles, keeps the stated effect/budget, re-suggests edibles (AC-2).
3. **Persona check:** the greeting/tone is the finalized Koptza voice (warm, family, no-pressure, conservative on dosing — `_research-education-blogs.md` §8); no "peek at your ID" (ADR-018); no literal `{{store_name}}` (export #11).
4. **Post-call:** confirm the `VoiceCall` row + the staff email/summary landed (queued or inline per `HHT_USE_CELERY`), and the analytics summary now counts this call.

**Test-data discipline:** deterministic fixtures; expected values hand-authored. The Leak-Guard and HMAC tests are non-negotiable gates on this phase (it touches a tool path + the webhook). Coverage: ~90% diff coverage on the new `voice/routing.py`, `voice/corrections.py`, `voice/analytics.py`, `dashboard/branding.py`; never lower an existing ratchet.

---

## 8. Risks / open questions

| Risk / open item | Impact | Mitigation / disposition |
|---|---|---|
| **O-10 brand capture still blocked by the Vercel wall** (`_research-education-blogs.md` §Provenance) | Theming can't use real tokens | Ship `tokens.json` with `provisional:true` + neutral fallback; `brand/CAPTURE.md` is the human runbook (browser/computer-use MCP if the operator has it). P5 is **not blocked** — theming lands as a config drop. |
| **Cartridge vs disposable mis-classification** (Cartridge ≠ Disposable, marketing_dashboard rule) | A reusable 510 cart suggested as a $3 disposable (or vice-versa) | Router passes `subcategory` only when the caller is explicit; otherwise budtender's facets pick real in-stock subtypes. Unit-tested both ways (AC-1). Never let the agent *guess* the subtype (Numbers/category-Guard). |
| **Correction logic over-clears or loops** | Caller frustration, dropped context | `apply_correction` is deterministic + unit-tested with an explicit clear/keep matrix; code owns the transition (not the LLM); corrected state persisted in budtender/`VoiceCall`, not memory (stateless-turn discipline). |
| **Celery adds operational weight swedish-bot didn't have** | More moving parts (Redis, worker) | Make it **optional** (`HHT_USE_CELERY=0` default → inline P2 behavior). Durable `VoiceCall` write stays synchronous regardless (ADR-017). Port the proven wiring from budtender, don't invent. |
| **Latency target is server-side only; true UX latency includes Vapi LLM+TTS** | "Looks fast in test, feels slow on a call" | Document the split explicitly in `_p5-latency-budget.md`: we own/tune the webhook handler (≤1500 ms p95); Vapi owns LLM/TTS turn latency (observe via Vapi's call report, tune by keeping prompts small — already done via member-level config + small prompts, ADR-002/011). |
| **Re-publishing prompts could reintroduce per-node config** (export #7) | Token bloat + drift | The publish path sets voice/model **once per member** (ADR-011); the no-dup test (§7.2) is a gate after every re-publish. |
| **Analytics merge could surface a forbidden field** from budtender | Leak (ADR-008) | `call_summary` reads only `public_product`-shaped + own non-PII rows; AC-7 contract test asserts no cost/margin substring in its output. |
| **budtender `/analytics/summary` shape/availability** (O-1) | Accept-rate/merch counts may be unavailable until budtender deploy confirmed | Degrade gracefully — `call_summary` returns the VoiceCall-derived metrics and marks budtender-sourced fields `null` when the service is unreachable (it boots and returns safely with no creds — `_research-suggestion-engine.md` §7). |
| **Open: is the Celery move desired now or deferred to EXP?** | Scope | Default OFF; ship the wiring behind the flag so the owner flips it without a code change. If the owner says "defer," the flag stays 0 and §3.5 is dormant (still tested in eager mode). |
| **Open: exact p95 latency target** | Acceptance threshold | Recommend ≤1500 ms server-side; confirm with owner once the baseline from `tools/loadtest_voice.py` is measured (the doc records baseline → target). |

---

## 9. Documentation protocol (close-out — binding, `03-CONVENTIONS.md` §6)

On completing P5, **in the same change:** bump this doc's status to `DONE` with the live-verified note; check off `15-P5-POLISH-BRAND.md` in `00-MASTER-ROADMAP.md` §7; record any new env vars (`HHT_USE_CELERY`, `CELERY_BROKER_URL`) in `03-CONVENTIONS.md` §3; append an ADR if the Celery-queue move is taken (it's an architectural decision — ADR-0XX "post-call work on Celery, durable write stays sync"); and add the brand-token provenance + the measured latency baseline/target to the relevant docs. No task completes without docs updated.
