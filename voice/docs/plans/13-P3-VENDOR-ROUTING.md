# 13 — P3 — VENDOR ROUTING — Executable Plan

> **Status:** EXECUTABLE SPEC (authoritative for P3). Written 2026-06-22.
> **Subsystem:** S4 (Vendor routing). **Capability:** C4 (owner request D — "Vendor-call detection + pass-through + callback-to-agent"). **Fixes export weakness #6** ("No vendor/wholesale call path — purely a B2C retail-buyer flow; the same store number takes vendor/delivery/manifest calls with no routing for them").
> **Implements ADRs (binding, never contradicted here):** ADR-002 (Assistants + ONE Squad, never a Workflow), ADR-003 (idempotent code-provisioned via documented Vapi REST), ADR-006 (peppered phone-hash; raw numbers never persisted), ADR-008 (leak-safe), ADR-010 (gpt-4.1-mini assistants / Gemini server-side), ADR-011 (voice/persona set ONCE per member), ADR-015 (**the vendor flow — detect at entry → warm transfer → on no-answer return to AI → capture reason → callback + alert + state window**), ADR-016 (warm `transferCall` + `summaryPlan` injecting `{{transcript}}`), ADR-017 (eocr → durable `VoiceCall` → email sink; immediate alert on vendor), ADR-019 (HMAC fail-closed, prod-fail-closed, per-store keys only in budtender), ADR-020 (`voice/tools/` package + registry — P3 adds its OWN module `voice/tools/vendor.py`).
> **Read order before executing (mandatory):** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → this file.
> **Ports from:** `swedish-bot` (`crm/models.py` `phone_hash`~L17 + the idempotent-`ServiceRequest`/`LeadDelivery` durability idioms, `crm/sinks.py` `EmailSink`~L40 / `dispatch`~L119, `dashboard/views.py::session_list`~L93 list/sort/paginate pattern for the P4 queue). **Reuses (does NOT re-implement):** P0's `voice/webhooks.py` dispatcher + `voice/tools/__init__.py::TOOL_REGISTRY` + `voice/models.VoiceCall` + `crm/models.phone_hash` + `kb/` store-facts; P2's `crm/sinks.py` immediate-alert routing + `build_assistant_payload`/`build_squad_payload` shared builders + `voice/outcomes.classify_outcome`. **Net-new:** the `entry_router` intent classifier prompt, the `vendor` Squad member, `voice/tools/vendor.py::notify_vendor_callback`, `crm/models.VendorCallback`.
>
> **One-line goal:** a vendor / wholesale / delivery / manifest caller is classified at `entry_router` and handed to the `vendor` member (never the retail budtender flow); the `vendor` member **warm-transfers to the store human**; on **NO ANSWER** control **returns to the AI**, which **asks the caller to explain what they're calling about**, logs a **`VendorCallback`** (idempotent), **emails/alerts staff immediately**, and **states a callback window** — with a durable `VoiceCall(outcome=vendor_callback)` written and the dashboard vendor-callback queue (P4) fed.

---

## 0. The owner flow this phase encodes (read this first — it is the spec)

This is **ADR-015**, the owner's exact words, made executable. Every later section serves this one sequence:

```
Caller dials → entry_router (greets as Koptza, confirms 21+)
  → opener is "I'm dropping off a delivery / I'm a vendor / here's a manifest / wholesale order"
  → entry_router CLASSIFIES intent = VENDOR  (never retail — the gate that fixes export #6)
  → handoff → vendor member
  → vendor: warm transferCall to the store human  (per-location HHT_TRANSFER_NUMBER_<STORE>)
       ├─ IF a human ANSWERS → warm transfer completes (operator hears {{transcript}} summary). DONE.
       └─ IF NO ANSWER (transfer fails / times out / busy) → control RETURNS to the vendor AI member
  → vendor AI: "I couldn't reach the team right now — can you tell me what you're calling about?"
       → caller explains (delivery / wholesale order / manifest correction / sample drop / invoice)
  → tool notify_vendor_callback{store, caller_phone_hash, reason, summary}   (ASYNC tool)
       → POST /api/voice/vapi → voice/tools/vendor.py::notify_vendor_callback
           → crm/models.VendorCallback row created  (IDEMPOTENT on (vapi_call_id))
           → crm/sinks.dispatch(...) email/alert to staff  (IMMEDIATE — outcome=vendor)
           → optional Slack secondary sink
       → returns a stated callback window string  (from a config, NOT LLM-originated)
  → vendor AI: "Got it — someone will call you back within {window}. Thanks for calling Happy Time!"
  → end-of-call-report → VoiceCall(outcome=vendor_callback) + immediate staff alert (P2 recognizes the label)
```

**Three load-bearing invariants in this flow (binding):**
1. **VENDOR is detected BEFORE retail.** A vendor opener must never fall into the budtender slot-fill (it would waste the call and mis-route a B2B caller into a B2C funnel). The classifier's vendor branch is checked with **high precedence** (§4.1).
2. **The warm transfer is tried FIRST; the callback is the FALLBACK.** The owner wants vendors to reach a human first. `notify_vendor_callback` only fires on the **no-answer return-to-AI** leg — it is never the first action.
3. **The callback window is config, not invented.** The spoken window comes from `settings.HHT_VENDOR_CALLBACK_WINDOW` / a `StoreFact` row (Numbers-Guard — the LLM never originates a figure, `03-CONVENTIONS.md` §1.5). Default "one business day."

---

## 1. Goal & scope

### 1.1 In scope (this phase ships all of)

1. **The `entry_router` intent classifier prompt + structured output (the central deliverable).** P0/P1 scaffolded the Squad `assistantDestinations` (`entry_router → budtender/faq`) but explicitly deferred the *classifier that fires them* to P3 (`11-P1` §5 step 4: "the full classifier taxonomy that fires this transition is P3/P5 prompt work"). P3 writes the one-turn slot-filling classifier that emits `{intent, store, ...}` and drives the handoff to **budtender / faq / vendor / escalation**, with the **vendor branch first-class** (the export had no vendor branch at all). The classifier is an `AgentPrompt` row (`role="entry_router"`) — owner-editable in P4, published via the existing PATCH path.
2. **The `vendor` Squad member** — a focused B2B assistant that NEVER enters retail. Its system prompt encodes the ADR-015 flow: greet the vendor, warm-`transferCall` to the store; on no-answer, ask the reason, call `notify_vendor_callback`, state the callback window, close warmly. Voice/transcriber/model set ONCE at the member level (ADR-011), reusing P0's `voice/constants.py` constants.
3. **`voice/tools/vendor.py::notify_vendor_callback`** — the async tool handler registered into P0's `TOOL_REGISTRY` (its OWN module per ADR-020, so it cannot collide with P1's `tools/suggest.py` in a parallel worktree). It validates args, writes an idempotent `VendorCallback`, fires the immediate staff alert via `crm/sinks.dispatch`, and returns the spoken callback-window string.
4. **`crm/models.VendorCallback`** — the durable B2B-callback record (store, caller phone-hash, reason, summary, status, idempotency key, FK to `VoiceCall`). Idempotent on `vapi_call_id` (re-delivery never double-creates / double-alerts).
5. **The warm-transfer config on the `vendor` member** — a Vapi-native `transferCall` tool to the per-location number with `transferPlan.mode = "warm-transfer-wait-for-operator"` + a `summaryPlan` injecting `{{transcript}}` (reusing P2's `build_*_transfer_tool` shape so there is ONE transfer-payload shape across vendor + escalation).
6. **The no-answer → return-to-AI transition** — the Vapi mechanism (a `transferCall` failure/no-answer **does not** end the call; control returns to the calling assistant member) wired and documented, with the vendor member's prompt instructed to take the reason-capture path on return.
7. **The squad-shape additions** — `entry_router → vendor` ("vendor/wholesale/manifest") and `vendor → escalation` (a vendor who turns hostile/disputes), re-asserted from code in `build_squad_payload` (so the canvas can never delete them — P4 boundary).
8. **The `vendor_callback` outcome wiring** — P2's `voice/outcomes.classify_outcome` already *recognizes* the `vendor_callback` label (precedence 4, immediate alert — `12-P2` §4.4); P3 makes it *real* by writing the `VendorCallback` and setting `VoiceCall.outcome = vendor_callback` / `reason = "vendor"`. The dashboard vendor-queue (P4) reads these rows.

### 1.2 Out of scope (other phases / EXP)

- **The dashboard vendor-callback queue UI** (list / mark-contacted / mark-closed / re-send alert) — **P4** (`14-P4` §3.2 `vendor_queue`/`vendor_callback_update`; §4 reads the `VendorCallback` rows P3 writes). P3 ships the **model + the write path + the read-ready fields**; P4 ships the operator screen.
- **The escalation member, the eocr handler, `crm/sinks.py` itself, `classify_outcome`** — **P0/P2** (P3 *consumes* them; it adds the `vendor` outcome write but does not author the sink or the classifier).
- **The budtender retail flow / Dutchie / suggestions** — **P1**. The vendor member never calls a product tool; vendor is B2B.
- **Cartridge entry / back-edge corrections / brand visuals / Celery / analytics** — **P5**. (The P5 cartridge classifier edits the SAME `entry_router` `AgentPrompt` P3 authors here — P3 ships the classifier taxonomy that P5 extends with a cartridge *category*, not a new intent.)
- **n8n / CRM secondary routing of the vendor callback** — the owner runs n8n as the existing returns/auto-return gateway; P3 ships an **optional webhook secondary sink** behind `VENDOR_CALLBACK_WEBHOOK_URL` (off by default, O-9-style), but the durable `VendorCallback` + email are authoritative. Deep n8n workflow wiring is an EXP item.

### 1.3 Non-negotiable boundaries (binding)

- **Warm-transfer-first, callback-as-fallback (ADR-015).** `notify_vendor_callback` is invoked ONLY on the no-answer return-to-AI leg. A unit/contract test asserts the vendor prompt + tool ordering never calls the callback tool before a transfer is attempted (the tool itself is idempotent + harmless, but the *flow* is the spec).
- **VENDOR before RETAIL (export #6 fix).** The classifier's vendor lexicon is checked with high precedence; a vendor opener never routes to budtender. A classifier unit test pins the matrix (§7 A).
- **Numbers-Guard.** The callback **window** and any store fact spoken come from config / KB rows, never the LLM (`03-CONVENTIONS.md` §1.5; `_research-education-blogs.md` §1 house rule). The vendor member is told "state the callback window from the tool's returned `callback_window` string; never invent a time."
- **Leak-Guard (defensive).** The vendor surface touches **no** product data, but the contract test still asserts no `"cost"`/`"margin"` substring in the `notify_vendor_callback` response (ADR-008) — the guard holds across every tool surface uniformly.
- **PII discipline (ADR-006/019).** The raw caller number is **never persisted**. `VendorCallback` stores `caller_phone_hash = phone_hash(number)` (peppered, `PHONE_HASH_PEPPER ≠ SECRET_KEY`). The operator reaches the live vendor via the warm-transfer PSTN leg or by reading the hash-keyed record + calling back through their own records — never via a stored raw number in this repo.
- **HMAC fail-closed at the edge.** The `tool-calls` webhook carrying `notify_vendor_callback` is rejected with 401 on a missing/bad signature **before** the handler runs (P0's `voice/signing.verify_signature`; re-asserted on the vendor path — §7 G).
- **Idempotent, fail-loud.** `VendorCallback` is idempotent on `vapi_call_id`; the email sink fails loud (logged + recorded as `failed`, never silently swallowed). A re-delivered tool-call never creates a second row or sends a second email.
- **`voice/tools/vendor.py` is P3's OWN module** (ADR-020) — P3 appends ONE import line (`from . import vendor`) to `voice/tools/__init__.py`; it never edits P1's `suggest.py` or P0's `faq.py` body (parallel-worktree safety, roadmap §6 shared-file hazard).

---

## 2. Dependencies (what MUST exist first)

P3 is **parallelizable with P1 ∥ P2 after P0** (roadmap §4/§6: "P1 ∥ P2 ∥ P3 run in parallel after P0; they touch disjoint files"). P3's worktree is `wt-p3-vendor`. Every dependency below is a hard prerequisite; the table cites the doc/path that establishes it and the graceful-degradation if an owner-supplied placeholder is absent.

| # | Dependency | Where it comes from | What P3 consumes / degrade if absent |
|---|---|---|---|
| D1 | `voice/webhooks.py` POST `/api/voice/vapi` dispatcher (the 4 event kinds + the §4 frozen shapes) + `voice/signing.verify_signature` (HMAC fail-closed) | **P0** (`10-P0` §3.3, §4 — THE shared contract; the FORK GATE) | The `tool-calls` branch routes `notify_vendor_callback` through `TOOL_REGISTRY`. P3 forks only AFTER the fork gate (`10-P0` §0). |
| D2 | `voice/tools/__init__.py::TOOL_REGISTRY` + `register(name)` + `dispatch(name, args, ctx)` (the ADR-020 scaffold) | **P0** (`10-P0` §3.3) | P3 adds `voice/tools/vendor.py` and ONE import line. Without the registry, P3 has no parallel-safe place to land its handler. |
| D3 | `voice/models.VoiceCall` / `VoiceTurn` / `Outcome` (incl. the `VENDOR_CALLBACK` enum value + `reason` field) | **P0** (`10-P0` §4.6) | `notify_vendor_callback` sets `VoiceCall.outcome = vendor_callback`, `reason = "vendor"`; FK target for `VendorCallback`. The `Outcome.VENDOR_CALLBACK` value already exists (P0 froze it for P3). |
| D4 | `crm/models.phone_hash(number)` (peppered SHA-256) | **P0** ports it from `swedish-bot/crm/models.py`~L17 | Hashes the caller number for `VendorCallback.caller_phone_hash`. ADR-006. |
| D5 | `crm/sinks.py` `EmailSink` + `dispatch(record)` + `_recipients_for(store)` + the immediate-alert (`[URGENT]`) routing + optional `SlackSink` | **P2** (`12-P2` §3.1, §4.5, §4.6 — the email/alert plumbing) | `notify_vendor_callback` calls `crm/sinks.dispatch(voice_call, immediate=True)`. P2 owns the sink; P3 invokes it with `outcome=vendor_callback`. If P2's worktree hasn't merged yet, P3 codes against the **frozen `dispatch(record, immediate)` signature** (`12-P2` §4.5) and the integration test stubs the sink. |
| D6 | `voice/outcomes.classify_outcome` recognizing the `vendor_callback` label (precedence 4, immediate) | **P2** (`12-P2` §4.4) | The eocr handler classifies the call as `vendor_callback` when a `notify_vendor_callback` fired. P3 ALSO sets the outcome directly in the tool (belt-and-suspenders), so it is correct even if the eocr arrives before classification runs. |
| D7 | `core/services/vapi.py` REST client + `tools/provision_vapi.py` idempotent provisioner (GET-then-PATCH; `find_*_by_name`) | **P0** (`10-P0` §3.2, §3.6, ADR-003) | P3 provisions the `notify_vendor_callback` tool + the `vendor` assistant + the squad edges. `vapi_tool_id`/`vapi_assistant_id` written back. |
| D8 | The shared payload builders `build_assistant_payload(prompt)` + `build_squad_payload()` + the transfer-tool builder | **P0** ships the builder; **P2** added `build_*_transfer_tool` (`12-P2` §6.1) for the warm transfer. P4 reuses them for "Publish to Vapi". | P3 reuses `build_*_transfer_tool` for the vendor warm transfer (ONE transfer shape across vendor + escalation) and `build_squad_payload` to add the vendor edges (re-asserted from code). |
| D9 | The `budtender` + `faq` + `escalation` members scaffolded as Squad members | **P0** (`faq`/`entry_faq`), **P1** (`budtender`), **P2** (`escalation`) | The `entry_router` classifier hands off to all four; the squad destinations reference them by name. If a sibling worktree hasn't merged its member yet, the destination name resolves at provision time (Vapi matches `assistantName`); a missing member surfaces as a provision warning, not a crash. |
| D10 | `HHT_TRANSFER_NUMBER_{YAKIMA,MTVERNON,PULLMAN}` + `HHT_DEFAULT_STORE` (**O-4 placeholders**) | owner (`03-CONVENTIONS.md` §3.5) | The vendor warm-transfer destination reads `HHT_TRANSFER_NUMBER_<KEY>`; unset → a documented placeholder number + a `"transfer number not configured for <KEY>"` warning at provision (does NOT block). The no-answer/callback path is testable with the placeholder. |
| D11 | `HHT_VENDOR_CALLBACK_WINDOW` (callback window string) + `STAFF_ALERT_EMAIL` / SMTP (**O-9 placeholders**) | owner (`03-CONVENTIONS.md` §3.7 + a new var §10) | The spoken window defaults to "one business day" when unset; the email degrades to a logged no-op with no SMTP but the durable `VendorCallback` write still happens (the record is never lost — ADR-017). |
| D12 | `VENDOR_CALLBACK_WEBHOOK_URL` (optional n8n/CRM secondary sink, **O-6/O-9 placeholder**) | owner | When unset, the optional webhook sink is a no-op; the email + DB record are authoritative. |

**Graceful-degradation rule (so P3 is never hard-blocked by env placeholders):** every owner-supplied placeholder (transfer numbers O-4, callback window / SMTP / Slack / n8n webhook O-9/O-6) is **read at call time, never required at import**. The vendor member provisions, the tool handler runs, the `VendorCallback` is written, and the spoken flow completes against placeholders; the email/transfer destinations are configuration, not code.

---

## 3. File-by-file task list

Each entry: **exact path → responsibility → key functions/shape → source file to port from (with its path)**. New files marked ★; ported-and-adapted cite the source; reused-as-is (P0/P1/P2) marked PLUG.

### 3.1 `voice/` — the vendor tool handler + the registry hook

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `voice/tools/vendor.py` ★ | **The async tool handler `notify_vendor_callback(args, ctx)`.** Validates args (`store`, `reason`, `summary`; `caller_phone_hash` derived from `ctx` if absent); resolves the `VoiceCall` by `ctx.call_id` (created/updated idempotently); writes an idempotent `VendorCallback`; fires `crm/sinks.dispatch(voice_call, immediate=True)`; computes the spoken callback window from config; returns the §4.2 result. **Self-registers** via `@register("notify_vendor_callback")`. Leak-Guard + Numbers-Guard applied to the response. | `notify_vendor_callback(args, ctx) -> dict`, `_resolve_store(args, ctx)`, `_callback_window()`, `_upsert_vendor_callback(voice_call, store, reason, summary, phone_hash)`. | net-new; registry pattern = `voice/tools/faq.py` (P0); idempotent-write idiom = `swedish-bot/crm/models.py` `ServiceRequest`/`LeadDelivery` (idempotent per key). |
| `voice/tools/__init__.py` EDIT (ONE line) | Register the vendor module. | append `from . import vendor  # noqa: F401  (self-registers notify_vendor_callback)` after P0's `from . import faq` and P1's `from . import suggest`. **No body edit** — only an import append (ADR-020 parallel-safety). | P0 scaffold (`10-P0` §3.3). |
| `voice/vendor_flow.py` ★ (small, pure) | **Pure helpers for the vendor flow** so the webhook/tool stay thin and unit-testable with no network: `is_no_answer(ended_reason_or_status) -> bool` (maps Vapi transfer-failure/no-answer/busy reasons to True), `normalize_reason(raw) -> str` (folds the caller's free-text "why" into a stable `reason` enum: `delivery|wholesale_order|manifest|sample_drop|invoice|other`), `callback_window_text(cfg) -> str`. | `is_no_answer`, `normalize_reason`, `callback_window_text`. | net-new; deterministic; mirrors `voice/outcomes.py` (P2) pure-function style. |
| `voice/guardrails.py` PLUG | Reuse P0's `assert_no_leak(payload)` on the vendor tool response (defensive Leak-Guard; the vendor surface has no product fields, but the guard is uniform). | `assert_no_leak`. | P0 (`10-P0` §3.3). |
| `voice/webhooks.py` PLUG (no edit) | The `tool-calls` branch already dispatches by `function.name` through `TOOL_REGISTRY` (P0). `notify_vendor_callback` arrives there with no change to the dispatcher. The `end-of-call-report` branch (P2) classifies `vendor_callback` and is unchanged. | — | P0 (`10-P0` §4.3) / P2 (`12-P2` §4.4). |
| `voice/models.py` PLUG (no schema change) | `VoiceCall.outcome = Outcome.VENDOR_CALLBACK` + `reason = "vendor"` are SET by the tool; the enum value + `reason` field already exist (P0 froze them). **No migration in `voice/`.** | `Outcome.VENDOR_CALLBACK` (P0). | P0 (`10-P0` §4.6). |

### 3.2 `crm/` — the durable B2B-callback record

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `crm/models.py` EDIT (additive) | **`VendorCallback` model** (§4.3). + a `VendorCallbackStatus` `TextChoices` (`open`/`contacted`/`closed`). FK to `voice.VoiceCall` (the call that produced it). Idempotency = `vapi_call_id` unique. `caller_phone_hash` only (raw number never stored — ADR-006). A `created_at`/`updated_at`/`contacted_at`. Migration in `crm/migrations`. | `VendorCallback`, `VendorCallbackStatus`, `VendorCallback.mark_contacted()`/`mark_closed()` (used by P4). | `swedish-bot/crm/models.py` (`ServiceRequest`~the durable-record shape + `phone_hash`~L17; the idempotent-record + status-lifecycle pattern). |
| `crm/sinks.py` PLUG (no edit — P2 owns it) | `dispatch(record, immediate=True)` + `_recipients_for(store)` + `EmailSink`/`SlackSink`. P3 **calls** it with the `vendor_callback` outcome; the subject/body contract (`12-P2` §4.5) already handles the `vendor` outcome label + `[URGENT]` prefix. P3 adds **no** sink code. (If the optional n8n webhook sink is wired, it is a new `VendorWebhookSink` — see below — gated and additive.) | `dispatch`, `_recipients_for`. | P2 (`12-P2` §3.1, §4.5/§4.6). |
| `crm/sinks.py` OPTIONAL ADD (gated) | `VendorWebhookSink.enabled()` → `bool(VENDOR_CALLBACK_WEBHOOK_URL)`; POSTs `{store, reason, summary, callback_window, vapi_call_id}` (NO PII, NO cost/margin) to the owner's n8n/CRM. Off by default; never a primary record. Mirrors P2's `SlackSink.enabled()` shape exactly. | `VendorWebhookSink`. | `swedish-bot/crm/sinks.py` `WebhookSink` shape; P2's `SlackSink` enabled-gate idiom. |

### 3.3 `kb/` — the `entry_router` classifier prompt + the vendor member prompt + vendor store-facts

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `kb/seed.py` EDIT (additive — `seed_agent_prompts`) | **Author/extend two `AgentPrompt` rows** (idempotent `get_or_create` by `role`): (1) **`entry_router`** — the intent classifier prompt + structured-output contract (§4.1); persona "Koptza", spoken 21+ confirm (ADR-018, no "peek at ID"), one-turn slot-filling that emits `{intent, store, ...}` and hands off; the **vendor branch first-class**. `vapi_model="gpt-4.1-mini"`, `voice_id=a3520a8f-…`, `tool_names=[]` (routing is a pure LLM turn; no tool). (2) **`vendor`** — the B2B member prompt encoding the ADR-015 flow (§5); `tool_names=["notify_vendor_callback"]` + the warm `transferCall` tool config (transfer is a native Vapi tool on the assistant, not a name in `tool_names`). | extend `seed_agent_prompts()` (P0) with the `entry_router` + `vendor` rows. | P0's `seed_agent_prompts` (`10-P0` §3.4/§4.7); the export's intent/greeting copy (Downloads JSON) for the Koptza tone; `_research-education-blogs.md` §8 house style. |
| `kb/seed.py` EDIT (additive — `seed_store_facts`) | **Add a `StoreFact kind="vendor"` row per store** (and an `(all)` row) carrying the **vendor-facing facts** the AI states on the no-answer leg: the callback window ("we'll call you back within one business day"), the receiving/delivery contact posture, and "leave your name, company, and what you're dropping off." So the spoken window/contact are KB-grounded (Numbers-Guard), editable in P4 without a code change. | add `seed_vendor_facts()` called from `seed_all()`. | P0's `seed_store_facts` (`10-P0` §4.7). |
| `kb/seed.py` PLUG | `StoreFact` rows for the 3 stores (phone/address/email/hours) already seeded by P0 — the vendor member localizes to the caller's `store` using them. | — | P0 (`10-P0` §4.7). |

### 3.4 `tools/` — the provisioner extension

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `tools/provision_vapi.py` EDIT (additive) | **Provision the vendor tool + member + edges (idempotent, GET-then-PATCH).** Add `notify_vendor_callback` to the tool catalog (§6.1 schema); add `ensure_assistant("vendor")` (the `vendor` `AgentPrompt` → `build_assistant_payload` + the warm-transfer tool); extend `ensure_assistant("entry_router")` (rename/split from P0's `entry_faq` per §6.4) to carry the classifier prompt; extend the squad members/destinations to add `entry_router→vendor` + `vendor→escalation` (§6.2). Write back `vapi_tool_id`/`vapi_assistant_id`. A 2nd run = zero new objects. | extend the tool catalog + `ensure_assistant` calls + the squad-shape builder. | P0's `tools/provision_vapi.py` (`10-P0` §3.6); P2's transfer-tool addition (`12-P2` §6.1). |
| `voice/constants.py` PLUG (read-only) | The vendor member reuses the member-level constants `VAPI_VOICE_ID` (Cartesia sonic-3 Koptza `a3520a8f-226a-428d-9fcd-b0a4711a6829`), `VAPI_ASSISTANT_MODEL` (`gpt-4.1-mini`), `DEEPGRAM_KEYTERMS` (the ~33-term list). Set ONCE at the member level (ADR-011); P3 does NOT re-declare per node. | `VAPI_VOICE_ID`, `VAPI_ASSISTANT_MODEL`, `DEEPGRAM_KEYTERMS`. | P0/P1 (`10-P0` §3, `11-P1` §3). |

### 3.5 `tests/` — the vendor test suite

| Path | Responsibility | Port from |
|---|---|---|
| `tests/test_routing_intent.py` ★ NEW | **Classifier matrix (the central test).** A table of openers → expected `intent` (vendor / retail / faq / escalation), with vendor openers ("I'm dropping off a delivery", "I'm a vendor", "here's a manifest", "wholesale order", "I have a sample drop", "invoice question") → `intent="vendor"` and **never** `retail`; retail/faq/escalation openers unaffected (no regression). Tests the prompt-contract via the structured-output parser, not a live LLM (deterministic — the parser/normalizer is code). | net-new (table-driven, expected values hand-authored — `03-CONVENTIONS.md` §5). |
| `tests/test_vendor_flow.py` ★ NEW | `is_no_answer` over Vapi reason strings; `normalize_reason` folding; `callback_window_text` from config; the warm-transfer-first invariant (the tool is never the first action in the flow contract). Deterministic, no network. | net-new. |
| `tests/test_vendor_callback_tool.py` ★ NEW (integration) | `notify_vendor_callback` writes an idempotent `VendorCallback` (2nd identical call → no 2nd row, no 2nd email), sets `VoiceCall.outcome=vendor_callback`/`reason=vendor`, returns the §4.2 shape with the config window; the staff alert fires immediate (`dispatch` called with `immediate=True`) — sink stubbed. | net-new; reuses P2's sink stub. |
| `tests/test_leak_guard_vendor.py` ★ NEW (contract, **mandatory gate**) | No `"cost"`/`"margin"` substring in any `notify_vendor_callback` response (ADR-008). | reuse P1's Leak-Guard fixture pattern. |
| `tests/test_hmac_fail_closed_vendor.py` ★ NEW (contract, **mandatory gate**) | A `tool-calls` payload for `notify_vendor_callback` with a missing/bad HMAC → 401 before the handler runs; a valid signature passes (ADR-019). | reuse P0's HMAC test. |
| `tests/test_pii_vendor.py` ★ NEW | The raw caller number is absent from every persisted field of `VendorCallback` + `VoiceCall`; only the peppered hash is stored (ADR-006). | reuse P1's PII test pattern. |
| `tests/test_provision_vendor.py` ★ NEW (contract) | Provision creates the `notify_vendor_callback` tool + `vendor` assistant + the `entry_router→vendor` / `vendor→escalation` edges (mocked `vapi.py`); a 2nd run issues **zero** new objects (drift-free, ADR-003); the warm-transfer tool has a **non-empty** `destinations` array (placeholder when env unset); voice/model set ONCE (ADR-011). | reuse P1/P2 provisioning-test pattern. |

---

## 4. Data contracts / JSON schemas

The load-bearing section. The classifier output, the tool envelope, and the `VendorCallback` model are frozen here.

### 4.1 `entry_router` structured-classifier output (the routing contract)

The `entry_router` runs ONE slot-filling/classification LLM turn (gpt-4.1-mini) and emits a structured object that drives the Squad handoff. P3 owns this prompt + contract; P5 EXTENDS it (adds the cartridge category to the `retail` branch — `15-P5` §4.2 — **without** changing the intent enum).

```json
{
  "intent": "vendor",              // vendor | retail | faq | escalation   (REQUIRED — the routing key)
  "store": "yakima",               // yakima | mount-vernon | pullman | null  (inferred from inbound number or asked)
  "vendor_kind": "delivery",       // delivery | wholesale_order | manifest | sample_drop | invoice | other  (when intent=vendor)
  "age_confirmed": true,           // spoken 21+ confirm (ADR-018); retail/faq require true before transacting
  "human_requested": 0,            // count of explicit "talk to a person" asks (feeds escalation precedence — 12-P2 §4.4)
  "raw_opener": "I'm dropping off a delivery for the back"   // the caller phrase, for the log + reason normalization
}
```

**Routing rules (precedence — VENDOR before RETAIL; binding, fixes export #6):**

| Order | If the opener matches… | `intent` | Handoff (Squad destination) |
|---|---|---|---|
| 1 | a **dispute / defective / refund / "broken cart"** signal, OR `human_requested >= 2` | `escalation` | → `escalation` (P2) |
| 2 | a **vendor lexicon** hit (see below) | `vendor` | → `vendor` (P3) |
| 3 | an **info** ask (hours / specials / returns / payment / pickup / location / limits / weights-types) | `faq` | → `faq` (P0) |
| 4 | a **retail-buyer** ask ("looking for / recommend / what's good for …", a category/effect/budget) | `retail` | → `budtender` (P1) |
| 5 | ambiguous / none of the above | `faq` | → `faq` (a safe, grounded default; the FAQ member can re-route) |

> **Why vendor outranks retail (precedence 2 > 4):** a B2B caller saying "I've got a delivery / a manifest / a wholesale order" must never be slot-filled as a shopper. Escalation (1) still outranks vendor so a *hostile* vendor ("your last order was wrong and I want a refund") reaches a human, not the callback loop.

**Vendor lexicon (the classifier's vendor trigger set — taught by few-shots in the prompt, NOT a code regex):**
`vendor, wholesale, distributor, supplier, rep, sales rep, delivery, dropping off / drop-off, manifest, transfer manifest, METRC / CCRS / WCIA, sample(s) / sample drop, PO / purchase order, invoice, accounts payable, "for the buyer", "for receiving", "I'm here with an order", "I'm the driver".`
(These mirror the owner's real returns/auto-return/vendor workflows — `01-ARCHITECTURE.md` §1.4; the marketing_dashboard returns/manifest domain. The classifier learns them from few-shots; the server-side `normalize_reason` folds the *spoken reason* into the stable `reason` enum independently.)

**Contract notes:**
- `store` inference: if the inbound number is one-per-store, `entry_router` receives `{{store_name}}` via the `assistant-request` `variableValues` (P0 §4.2) and sets `store` directly; for the single-number P0/P1 default it uses `HHT_DEFAULT_STORE` or asks. The vendor member uses `store` to pick the transfer number + the store-facts row.
- `age_confirmed` is **not** required for the `vendor` path (a vendor isn't purchasing) — the vendor member skips the 21+ gate (it greets B2B, not a buyer). This is an explicit difference from retail/faq.
- The classifier emits this via Vapi's structured-output / the assistant's tool-less handoff with `structuredData`; the server reads `analysis.structuredData` on `status-update`/`end-of-call-report` (P0 §4.4/§4.5) for the log + outcome.

### 4.2 `notify_vendor_callback` tool — request / response (the frozen envelope)

**Inbound (Vapi `tool-calls` → `POST /api/voice/vapi`, P0 §4.3 envelope):**
```json
{ "type": "tool-calls",
  "toolCalls": [ { "id": "call_xyz", "function": {
      "name": "notify_vendor_callback",
      "arguments": {
        "store": "yakima",                         // yakima|mount-vernon|pullman (REQUIRED; falls back to ctx/default)
        "reason": "delivery",                      // delivery|wholesale_order|manifest|sample_drop|invoice|other
        "summary": "Driver from GreenLeaf has a delivery + manifest for receiving; no one answered the back line.",
        "caller_name": "Marcus (GreenLeaf)",       // OPTIONAL — spoken name/company, free text, no PII number
        "caller_phone_hash": null                  // OPTIONAL — server derives from ctx if absent (never the raw number)
      } } } ],
  "call": { "id": "vapi-call-uuid", "customer": { "number": "+1509…" } } }
```

**Response (the frozen tool-result envelope — Vapi shape, P0 §4.3):**
```json
{ "results": [ { "toolCallId": "call_xyz",
    "result": {
      "logged": true,
      "callback_id": 4412,                         // VendorCallback pk (for the operator queue)
      "callback_window": "one business day",       // FROM CONFIG/KB — Numbers-Guard (LLM speaks this, never invents)
      "store": "yakima",
      "reason": "delivery",
      "alerted": true,                             // staff email/alert fired (immediate)
      "spoken": "Got it — I've let the {store} team know and someone will call you back within {callback_window}."
    } } ] }
```

- The assistant **speaks `result.spoken`** (or composes from `callback_window`) — the **window string is the only number in the response and it is config-sourced** (Numbers-Guard).
- **Leak-Guard:** `guardrails.assert_no_leak(result)` runs before return — no `"cost"`/`"margin"` substring (uniform across all tools, ADR-008).
- **Idempotency:** keyed on `call.id` — a re-delivered tool-call returns the SAME `callback_id`, does NOT create a 2nd `VendorCallback`, does NOT re-fire the email (`logged:true`, `alerted:false` on the duplicate — the alert already went).
- **Unknown/invalid args:** missing `store` → resolve from `ctx.store` / `HHT_DEFAULT_STORE`; missing `reason` → `normalize_reason(summary)` or `"other"`; never a 500 (returns `logged:true` with a best-effort reason).

### 4.3 `crm/models.VendorCallback` (the durable B2B record — frozen for P4)

```python
class VendorCallbackStatus(models.TextChoices):
    OPEN      = "open"
    CONTACTED = "contacted"
    CLOSED    = "closed"

class VendorCallback(models.Model):
    vapi_call_id      = CharField(max_length=64, unique=True, db_index=True)   # idempotency key (== VoiceCall.call_id)
    voice_call        = ForeignKey("voice.VoiceCall", related_name="vendor_callbacks",
                                   null=True, blank=True, on_delete=SET_NULL)  # the call that produced it
    store             = CharField(max_length=32)                              # yakima|mount-vernon|pullman
    reason            = CharField(max_length=32, blank=True)                  # delivery|wholesale_order|manifest|sample_drop|invoice|other
    summary           = TextField(blank=True)                                 # the caller's stated "why" (server-folded)
    caller_name       = CharField(max_length=128, blank=True)                 # spoken name/company; NO phone number
    caller_phone_hash = CharField(max_length=64, blank=True, db_index=True)   # peppered SHA-256; raw number NEVER stored
    callback_window   = CharField(max_length=64, blank=True)                  # the window stated to the caller (snapshot)
    status            = CharField(max_length=16, choices=VendorCallbackStatus.choices,
                                  default=VendorCallbackStatus.OPEN, db_index=True)
    alerted           = BooleanField(default=False)                           # staff alert fired
    contacted_at      = DateTimeField(null=True, blank=True)                  # set by P4 mark_contacted()
    created_at        = DateTimeField(auto_now_add=True)
    updated_at        = DateTimeField(auto_now=True)

    def mark_contacted(self): ...   # P4 queue action
    def mark_closed(self):    ...   # P4 queue action
```

- **Idempotent** on `vapi_call_id` (`get_or_create`) — a re-delivered tool-call or a retried webhook never double-creates.
- **PII (ADR-006/019):** `caller_phone_hash` only; the raw number is hashed and discarded. `caller_name` is the spoken name/company (free text), not a contact number.
- **Leak-safe:** no product/cost/margin field exists on the model (a contract test asserts no such substring in any rendered queue row — P4 §7 H2 reuses this).
- The dashboard vendor-queue (P4) lists/filters by `status`/`store`/`created_at`, marks contacted/closed, and can re-fire the alert via `crm/sinks.dispatch` (`14-P4` §3.2).

### 4.4 The no-answer → return-to-AI signal (Vapi transfer semantics)

A Vapi `transferCall` that **fails to connect** (no answer / busy / declined / timeout) does **not** end the call — **control returns to the calling assistant member** (the `vendor` member), which then continues its prompt at the reason-capture step. The handler reads the disposition from the eocr/status fields P2 already maps:

| Signal source | Field | `is_no_answer` true when… |
|---|---|---|
| `status-update` after a transfer attempt | `status` / `endedReason`-ish | `forwarding` then back to `in-progress` with no `destination` connected |
| `end-of-call-report` | `endedReason` | one of `customer-did-not-answer`, `assistant-forwarded-call-failed`, `pipeline-error-*-transfer-failed`, `twilio-failed-to-connect-call`, `no-answer`, `busy` (the exact set pinned in `20-SPEC-vapi-deploy.md`; `voice/vendor_flow.is_no_answer` owns the mapping so it is one place to update) |
| transfer connected | `message.destination` present + a transfer-completed `endedReason` | → **not** no-answer; the warm transfer succeeded; NO callback is logged |

> **Binding:** the **vendor member's PROMPT** carries the conditional ("if I couldn't reach the team, ask what they're calling about and use `notify_vendor_callback`") — the model takes the reason-capture path on return. The **server** independently classifies disposition (`is_no_answer`) for the log so the record is correct even if the model phrasing drifts. The callback is logged when the vendor member calls `notify_vendor_callback`; a *successful* warm transfer never triggers it.

### 4.5 Outcome wiring (`vendor_callback`)

- The tool sets `VoiceCall.outcome = Outcome.VENDOR_CALLBACK` and `reason = "vendor"` directly (so it is correct the instant the tool runs).
- P2's `voice/outcomes.classify_outcome` independently recognizes the `vendor_callback` label (precedence 4, **immediate alert** — `12-P2` §4.4) on the eocr, so the per-call digest + `[URGENT]` email fire even if the eocr is the first the sink sees of it.
- `is_immediate_alert(vendor_callback, "")` → `True` (`12-P2` §4.4 precedence 4) → the staff email carries the `[URGENT]` subject prefix.

---

## 5. The vendor conversational design (the `vendor` member prompt)

This is the **copy** that fixes export #6 (no vendor path). It lives in the `vendor` `AgentPrompt.body` (§3.3), grounded in the P0/P3-seeded `StoreFact kind="vendor"` rows.

### 5.1 The spoken flow (the vendor member's behavior — ADR-015)

1. **Greet B2B, warm, no 21+ gate.** "Hey — thanks for calling Happy Time {store_name}. Are you here with a delivery, a wholesale order, or something else?" (Warm/family tone — `_research-education-blogs.md` §8. No "are you 21+?" — a vendor isn't buying; §4.1 sets `age_confirmed` N/A.)
2. **Attempt the warm transfer FIRST (ADR-015 invariant).** "Let me get our receiving team on the line for you — one sec." → the native `transferCall` runs warm (`warm-transfer-wait-for-operator`, the operator hears the `{{transcript}}` summary first) to `HHT_TRANSFER_NUMBER_<store>`.
3. **IF the human answers** → the warm transfer completes; the vendor member is done; the call ends after the handoff. **No callback is logged.**
4. **IF NO ANSWER** (control returns to the vendor AI) → **pivot to reason capture, apologetic + efficient:** "Sorry — I couldn't reach the team right this second. So I can pass it along accurately, can you tell me what you're calling about — a delivery, a wholesale order, a manifest, a sample drop, an invoice question?"
5. **Collect name/company + the reason.** "Great — and who should I say it's from? … Got it." (The model captures `caller_name` + a free-text reason; the server `normalize_reason`-folds it.)
6. **Call `notify_vendor_callback`** with `{store, reason, summary, caller_name}`. The tool logs the `VendorCallback`, alerts staff immediately, and returns the **config callback window**.
7. **State the callback window (Numbers-Guard).** "Perfect — I've let the {store} team know, and someone will call you back within {callback_window}. Thanks for calling Happy Time!" — the window is the tool's returned `callback_window` string, **never** an invented time.
8. **Never enter retail.** If a vendor pivots to "actually can I buy something" the member hands to `budtender` via the squad (that's a retail intent now) — but it never *starts* a slot-fill itself.
9. **Hostile / dispute escalation.** If the vendor becomes a dispute ("your last order shorted me, I want money back") → hand to `escalation` (the `vendor→escalation` edge, §6.2) — a human, not the callback loop.

### 5.2 Why this fixes export #6

The export was "purely a B2C retail-buyer flow; the same store number takes vendor/delivery/manifest calls with no routing for them" (synthesis brief §1, weakness #6). P3 adds (a) the **classifier vendor branch** (§4.1) so the call is *detected*, (b) the **vendor member** so it is *handled B2B*, (c) the **warm-transfer-first** so the vendor reaches a human (the owner's priority), and (d) the **no-answer callback capture** so the intent is never dropped — exactly the ADR-015 sequence.

---

## 6. Vapi deploy steps (what this phase provisions/patches)

P3 provisions via the documented CRUD only (ADR-002/003) — never `/workflow`. Idempotent (GET/find-by-name-then-PATCH); a re-run yields zero drift. Reuses P0's `core/services/vapi.py` + the shared payload builders.

### 6.1 Ensure the `notify_vendor_callback` tool

`ensure_tool("notify_vendor_callback")` — find by name; if absent `POST /tool`, else `PATCH /tool/{id}`:

```json
{ "type": "function",
  "async": true,
  "function": {
    "name": "notify_vendor_callback",
    "description": "Log a vendor/wholesale/delivery/manifest callback after a no-answer transfer, alert store staff, and return the callback window to state to the caller.",
    "parameters": { "type": "object",
      "properties": {
        "store":  { "type": "string", "enum": ["yakima","mount-vernon","pullman"] },
        "reason": { "type": "string", "enum": ["delivery","wholesale_order","manifest","sample_drop","invoice","other"] },
        "summary":{ "type": "string", "description": "What the vendor is calling about, in one sentence." },
        "caller_name": { "type": "string", "description": "Name/company the caller gives. No phone number." }
      },
      "required": ["store","reason","summary"] } },
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" } }
```

- `"async": true` — the callback log + email can complete server-side while the assistant continues speaking the window (it speaks the returned `callback_window`); the result is returned within the turn budget. (Vapi async-tool semantics pinned in `20-SPEC-vapi-deploy.md`.)
- Write the returned `toolId` to the local tool-id map (`vapi_tool_id`).

### 6.2 Ensure the `vendor` assistant + the warm-transfer tool

`ensure_assistant("vendor")` — `PATCH /assistant/{vendor_id}` with `build_assistant_payload(vendor_prompt)` (the P0/P4 shared builder, `14-P4` §4.3), where `model.toolIds` includes `notify_vendor_callback` and `model.tools` includes the warm `transferCall` from the SHARED `build_*_transfer_tool` (P2 `12-P2` §6.1 — ONE transfer shape across vendor + escalation):

```json
{ "name": "vendor",
  "model": { "provider":"openai", "model":"gpt-4.1-mini", "temperature":0.3, "maxTokens":250,
             "messages":[{"role":"system","content":"<vendor AgentPrompt.body, vars hydrated>"}],
             "toolIds":["<notify_vendor_callback id>"],
             "tools":[ {
               "type":"transferCall",
               "destinations":[ {
                 "type":"number",
                 "number":"${HHT_TRANSFER_NUMBER_YAKIMA}",
                 "message":"Connecting you to our receiving team now — one moment.",
                 "transferPlan":{
                   "mode":"warm-transfer-wait-for-operator",
                   "summaryPlan":{ "enabled":true,
                     "messages":[{ "role":"system",
                       "content":"Brief the Happy Time receiving operator before connecting a vendor. Summarize in 2-3 sentences: {{transcript}}" }] } } } ] } ] },
  "voice": { "provider":"cartesia", "voiceId":"a3520a8f-226a-428d-9fcd-b0a4711a6829", "model":"sonic-3",
             "experimentalControls": { "emotion":["positivity:highest"] } },
  "transcriber": { "provider":"deepgram", "model":"nova-3", "keyterms":["…DEEPGRAM_KEYTERMS (once)…"] },
  "server": { "url":"${PUBLIC_BASE_URL}/api/voice/vapi", "secret":"${VAPI_WEBHOOK_SECRET}" } }
```

- The destination `number` resolves at provision time from `settings.HHT_TRANSFER_NUMBER_<KEY>` where `<KEY>` = the vendor's `store` (default `HHT_DEFAULT_STORE`). If unset (O-4) → a documented placeholder + a `"transfer number not configured for <KEY>"` warning (graceful-degradation §2). **This is a non-empty `destinations` array — same as the escalation fix; a vendor transfer must be reachable.**
- Voice/transcriber/model are set **ONCE** at the member level (ADR-011) by `build_assistant_payload`; the transfer tool adds no per-node config. A unit test asserts the keyterm list/voiceId/model appear exactly once (§7 G).
- Write `assistantId` → `AgentPrompt(role="vendor").vapi_assistant_id`.

### 6.3 Wire the squad edges (entry_router → vendor; vendor → escalation)

`PATCH /squad/{VAPI_SQUAD_ID}` with `build_squad_payload()` (the P0/P4 shared builder) — destinations **re-asserted from code** (`01-ARCHITECTURE.md` §1.6) so the canvas can never delete them. The relevant edges (the full member set is in `12-P2` §6.2):

```json
{ "members": [
    { "assistantId":"<entry_router id>", "assistantDestinations":[
        { "type":"assistant","assistantName":"budtender",  "description":"retail intent" },
        { "type":"assistant","assistantName":"faq",        "description":"info intent" },
        { "type":"assistant","assistantName":"vendor",     "description":"vendor/wholesale/delivery/manifest" },
        { "type":"assistant","assistantName":"escalation", "description":">=2 human / dispute / defective" } ] },
    { "assistantId":"<vendor id>", "assistantDestinations":[
        { "type":"assistant","assistantName":"escalation", "description":"vendor dispute / hostile" } ] }
] }
```

- `entry_router→vendor` (description "vendor/wholesale/delivery/manifest") is the literal fix for export #6. `vendor→escalation` routes a hostile/dispute vendor to a human.
- `vendor` is otherwise **near-terminal**: it exits via the native warm `transferCall` (a successful transfer) or via the callback log + a warm close (no-answer). It does NOT hand off to `budtender`/`faq` itself (a vendor who pivots to buying is re-classified — the squad routes via `entry_router`'s destinations, but in practice the vendor member states the callback and ends).

### 6.4 The `entry_router` split from P0's `entry_faq` (rename, not new row)

P0 shipped ONE merged `entry_faq` member with `AgentPrompt.role="faq"` so the later split is a rename, not a restructure (`10-P0` §1.1). P3 performs the **split**: it adds an `entry_router` `AgentPrompt` row (the classifier) and a separate `faq` member; provision renames/creates `entry_router` and points the phone-number/squad at `entry_router` as the front member. (If P5 prefers to keep entry+faq merged until cartridge work, the classifier still lives on the front member — the contract §4.1 is identical; the split is mechanical and provision-idempotent.) **Coordinate at merge:** P3 owns the `entry_router` classifier prompt; if P1/P2 already split or renamed, P3 PATCHes the existing row (find-by-role) — never a blind second create.

### 6.5 Register the `end-of-call-report` server URL (already P0/P2)

No new server URL. The eocr → `voice/webhooks.py::handle_end_of_call_report` (P0) → `classify_outcome` (P2) → `vendor_callback` recognition + the immediate alert. P3 adds the **tool handler** behind the existing webhook + the `vendor_callback` *write*, not a new endpoint.

### 6.6 Idempotency / zero-drift

Running `python tools/provision_vapi.py` twice after the P3 edits produces **zero** new Vapi objects — only PATCHes; a second no-edit run produces zero PATCHes (the `last_publish_hash` short-circuit, `14-P4` §5). Acceptance §7 G.

---

## 7. Acceptance criteria (testable, concrete)

Each is a concrete pass/fail assertion. They restate roadmap §5 "P3 — Vendor routing" with exact checks.

**A. Vendor detected at entry (fix #6; VENDOR before RETAIL)**
- A1. `entry_router` classifier on each vendor opener ("I'm dropping off a delivery", "I'm a vendor / distributor / rep", "here's a manifest", "wholesale order / PO", "I have samples / a sample drop", "invoice / accounts payable question", "I'm the driver") → `intent="vendor"`, and the Squad handoff target is `vendor` — **never** `budtender`/`retail`. (Table test on the structured-output parser; the export had no vendor branch at all.)
- A2. A **dispute/defective** vendor opener ("your last order shorted me, I want a refund") → `intent="escalation"` (precedence 1 > 2), routed to `escalation`, **not** the callback loop.
- A3. Retail / faq / escalation openers are **unaffected** (no regression): "recommend an indica for sleep" → `retail`→budtender; "what time do you close" → `faq`; "let me talk to a person" (×2) → `escalation`.

**B. Warm-transfer-first, callback-as-fallback (ADR-015 — the core invariant)**
- B1. The `vendor` assistant payload contains a `transferCall` tool with a **non-empty** `destinations` array to `HHT_TRANSFER_NUMBER_<store>` + `transferPlan.mode = "warm-transfer-wait-for-operator"` + a `summaryPlan` injecting `{{transcript}}`. With the env unset (O-4) it is a documented placeholder + a `"transfer number not configured for <KEY>"` warning (not a crash).
- B2. `voice/vendor_flow.is_no_answer` maps the Vapi no-answer/busy/transfer-failed reason set to `True` and a transfer-connected disposition to `False` (table test over the §4.4 reason strings).
- B3. The flow contract asserts `notify_vendor_callback` is **never** the first action — a transfer attempt precedes it (the prompt-contract + the tool's own "this fires on no-answer" doc are verified; a contract test on the recorded turn sequence confirms a `transferCall` attempt appears before the `notify_vendor_callback` tool-call).

**C. `notify_vendor_callback` — log + alert + window**
- C1. A valid tool-call writes a `VendorCallback` (store/reason/summary/caller_name/caller_phone_hash/callback_window) and sets `VoiceCall.outcome = "vendor_callback"`, `reason = "vendor"`. The response matches §4.2 (`logged:true`, a `callback_id`, the **config** `callback_window`, `alerted:true`, a `spoken` string).
- C2. **Idempotency:** a re-delivered identical tool-call returns the SAME `callback_id`, creates **no** 2nd `VendorCallback`, and does **not** re-fire the email (`alerted:false` on the duplicate). (Assert row count == 1 and the sink called once.)
- C3. **Numbers-Guard:** the `callback_window` in the response equals `HHT_VENDOR_CALLBACK_WINDOW` (or the `StoreFact kind="vendor"` row), never an LLM-originated time. With the env unset → "one business day" default.
- C4. **Immediate staff alert:** `crm/sinks.dispatch` is called with `immediate=True`; the email subject carries `[URGENT]` and `_recipients_for(store)` (shared `STAFF_ALERT_EMAIL` + any per-store override) — reusing P2's contract (`12-P2` §4.5). With no SMTP, the durable `VendorCallback` still exists and the would-send is logged (record never lost — ADR-017).

**D. `VendorCallback` model + read-ready for P4**
- D1. `VendorCallback` is idempotent on `vapi_call_id`; `status` defaults `open`; `mark_contacted()`/`mark_closed()` set `status`/`contacted_at`. The dashboard queue (P4) can list/filter by `status`/`store`/`created_at`.
- D2. **PII (ADR-006/019):** the raw caller number is absent from every persisted `VendorCallback` + `VoiceCall` field; only `caller_phone_hash` (peppered) is stored. `caller_name` is the spoken name/company (no number). (Grep-style assertion over the model rows.)

**E. Never enters retail**
- E1. The `vendor` member's `tool_names` are `["notify_vendor_callback"]` (+ the native `transferCall`) — **no** `suggest_products`/`check_inventory`/`pair_upsell`. A test asserts no product tool is attached to the vendor assistant payload (the vendor surface is B2B-only).
- E2. The squad has **no** `vendor→budtender`/`vendor→faq` destination (a vendor pivot is re-classified at entry, not a vendor-member handoff) — only `vendor→escalation` (§6.3).

**F. Leak-safety + Numbers-Guard (non-negotiable gates — `03-CONVENTIONS.md` §5)**
- F1. No `"cost"`/`"margin"` substring in any `notify_vendor_callback` response (ADR-008) — `tests/test_leak_guard_vendor.py`.
- F2. The only number the vendor member speaks (the callback window) is config/KB-sourced (Numbers-Guard) — C3 + a prompt-contract check that the member is told to speak the tool's returned window, never invent one.

**G. Vapi provisioning + no per-node dup (ADR-003/011)**
- G1. Provision creates the `notify_vendor_callback` tool (POST-once) + the `vendor` assistant + the `entry_router→vendor` / `vendor→escalation` edges; ids written back. (Mocked `vapi.py`.)
- G2. **Idempotency / zero-drift:** a 2nd provision run issues **zero** new Vapi objects (only GET/PATCH/no-op) — assert create-call count == 0 on the 2nd run.
- G3. **No per-node duplication:** the `vendor` assistant payload sets voice/transcriber/model ONCE; the keyterm list/voiceId/model appear exactly once (export #7 guard).

**H. Security / fail-closed (ADR-019)**
- H1. A `tool-calls` webhook for `notify_vendor_callback` with a missing/bad HMAC → **401 before** the handler runs (`tests/test_hmac_fail_closed_vendor.py`); a valid signature passes.
- H2. `HHT_BACKEND_TOKEN` / `VAPI_WEBHOOK_SECRET` / the raw caller number never appear in any tool result, rendered output, or log line.

---

## 8. Test plan

Mirrors the four planes in `03-CONVENTIONS.md` §5 (Unit · Contract · Provisioning · Manual call). P3 touches a tool path (`notify_vendor_callback`) and the classifier → the **Leak-Guard** and **HMAC-fail-closed** tests are mandatory gates. **Test-data discipline:** deterministic fixtures; expected values hand-authored, not generated by the code under test.

### 8.1 Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network)
- `tests/test_routing_intent.py` — the classifier matrix (A1/A2/A3): vendor lexicon → `intent="vendor"` (never `retail`); dispute → `escalation`; retail/faq unaffected. Tests the structured-output **parser/normalizer** (code), not a live LLM.
- `tests/test_vendor_flow.py` — `is_no_answer` over the §4.4 reason strings (B2); `normalize_reason` folding ("dropping off a pallet"→`delivery`, "got a PO"→`wholesale_order`, "manifest correction"→`manifest`); `callback_window_text` from config (C3 default).
- `tests/test_pii_vendor.py` — `phone_hash(number) == crm.phone_hash(number)`; the raw number absent from every persisted field (D2).

### 8.2 Contract (`pytest -m integration`, Vapi + sinks mocked/stubbed)
- `tests/test_vendor_callback_tool.py` — `notify_vendor_callback` writes the idempotent `VendorCallback`, sets the outcome, returns §4.2; 2nd identical call → 1 row, 1 email (C1/C2/C4); the window is config-sourced (C3). Sink stubbed (P2's stub).
- `tests/test_leak_guard_vendor.py` (**mandatory**) — no `"cost"`/`"margin"` in any `notify_vendor_callback` response (F1).
- `tests/test_hmac_fail_closed_vendor.py` (**mandatory**) — bad/missing signature → 401 before the handler (H1).
- `tests/test_vendor_no_product_tool.py` — the `vendor` assistant payload has no product tool; only `notify_vendor_callback` + `transferCall` (E1); squad has only `vendor→escalation` (E2).
- `tests/test_warm_transfer_first.py` — the recorded turn sequence shows a `transferCall` attempt before any `notify_vendor_callback` (B3); a *successful* transfer fixture logs **no** `VendorCallback`.

### 8.3 Provisioning (`python tools/provision_vapi.py --dry-run` then live against a sandbox key)
- `tests/test_provision_vendor.py` — provision creates the tool + `vendor` assistant + the two edges (mocked `vapi.py`); the warm-transfer `destinations` is non-empty (placeholder when env unset, B1); voice/model set once (G3); a 2nd run = zero new objects (G2). Paste the dry-run diff (PATCH-only on re-run).

### 8.4 Manual call script (the per-phase definition of done — `03-CONVENTIONS.md` §5)
Dial `VAPI_PHONE_NUMBER_ID` (O-4 placeholder; use the provisioned test number, with `HHT_TRANSFER_NUMBER_<store>` pointed at a **test line you can leave unanswered**). Paste the transcript + the resulting `VendorCallback` + `VoiceCall` row for each:
1. **Vendor detected + warm transfer + NO ANSWER → callback (the headline flow):** open with "Hi, I'm dropping off a delivery and I've got a manifest for receiving." → agent classifies vendor (not retail), tries the warm transfer to the (unanswered) store line, returns to the AI, asks the reason, you say "delivery plus a manifest correction," → agent logs the callback, states "someone will call you back within one business day," and ends. **Confirm:** a `VendorCallback(store, reason="delivery"/"manifest", status="open")` row, `VoiceCall(outcome="vendor_callback")`, and an `[URGENT]` staff email landed (or the logged no-op with no SMTP). **No product tool was ever called.**
2. **Vendor + transfer ANSWERED (the happy path):** point the test transfer line at a number you DO answer; open as a vendor → the warm transfer connects (you hear the `{{transcript}}` summary) → confirm **no** `VendorCallback` is logged (the callback is the fallback, not the default).
3. **Wholesale opener:** "I'm a distributor with a wholesale order question" → `intent=vendor`, same flow; `reason="wholesale_order"`.
4. **Regression — retail still works:** "recommend something for sleep under $40" → routes to **budtender** (P1), NOT vendor.
5. **Regression — dispute escalates:** "your last delivery shorted me and I want a refund" → routes to **escalation** (P2), NOT the vendor callback loop.

---

## 9. Risks / open questions

| Risk / open item | Impact | Mitigation / disposition |
|---|---|---|
| **Vapi no-answer → return-to-AI semantics** (the exact `endedReason`/disposition strings) may differ from the assumed set (§4.4). | The vendor AI might not get control back, or `is_no_answer` mis-maps → a vendor is dropped instead of captured. | `is_no_answer` is ONE pure function (`voice/vendor_flow.py`) with the reason set pinned in `20-SPEC-vapi-deploy.md`; verified live in the manual call (§8.4 step 1) by leaving the transfer line unanswered. The vendor member's PROMPT ALSO carries the reason-capture path so the model recovers even if a disposition string drifts; the server-side classification is belt-and-suspenders. |
| **Classifier mis-routes a vendor into retail** (export #6 regressing). | A B2B caller wastes a call in the budtender slot-fill. | VENDOR precedence 2 > RETAIL 4 (§4.1); a table test pins the matrix (A1); the lexicon mirrors the owner's real returns/manifest vocab. Owner can tune the lexicon in the P4 dashboard (`AgentPrompt` edit + Publish) without a code change. |
| **A hostile vendor lands in the callback loop instead of a human.** | A dispute isn't escalated. | Escalation precedence 1 > vendor 2 (§4.1 A2); the `vendor→escalation` edge (§6.3) catches a mid-call pivot. |
| **Callback window invented by the LLM** (Numbers-Guard violation). | A vendor is told a wrong/over-promised time. | The window is the tool's returned `callback_window` (config/KB), and the prompt says "state the window from the tool, never invent" (F2/C3). |
| **`notify_vendor_callback` re-delivered** (Vapi retries) → duplicate rows/emails. | Spam + a confused queue. | Idempotent on `vapi_call_id` (`get_or_create`); the duplicate returns the same `callback_id` and `alerted:false` (C2). |
| **Transfer numbers unset (O-4).** | Vendor transfer points at a placeholder. | Provision emits `"transfer number not configured for <KEY>"`, substitutes a documented placeholder, and does not block; reads `HHT_TRANSFER_NUMBER_*` env (B1). |
| **Parallel-worktree collision on `voice/tools/__init__.py`** (P1 also appends an import). | Merge conflict. | Both phases append a single import line at the documented spot (P0 ships the file with a marked append region); a one-line append rarely conflicts and is trivially resolved. The handler BODIES are in disjoint files (`suggest.py` vs `vendor.py`) — ADR-020. |
| **The `entry_router` split from P0's `entry_faq`** (§6.4) collides with P1/P2/P5 also touching the front member. | Double-create / drift on the front assistant. | Provision is find-by-role-then-PATCH (never blind create — §6.4); whichever phase splits first creates the `entry_router` row, the rest PATCH it. Coordinate at the merge gate (roadmap §6). |
| **Open (O-6): vendor "callback to agent" semantics** — is the n8n/CRM secondary sink wanted now? | Scope. | Default: durable `VendorCallback` + immediate email are authoritative; the n8n `VendorWebhookSink` is **off** behind `VENDOR_CALLBACK_WEBHOOK_URL` (additive, mirrors P2's `SlackSink`). Owner flips it on without a code change. |
| **Open (O-9): staff alert routing** — shared vs per-store email for vendor alerts. | Inbox routing. | Reuses P2's `_recipients_for(store)` (shared `STAFF_ALERT_EMAIL` + optional `STAFF_ALERT_EMAIL_<STORE>`); no new decision needed. |
| **Open: exact callback window default + business-hours awareness.** | "One business day" may be wrong after hours / on weekends. | Ship `HHT_VENDOR_CALLBACK_WINDOW` (default "one business day") + a `StoreFact kind="vendor"` row the owner edits in P4; a business-hours-aware window is an EXP refinement, flagged to owner. |

---

## 10. New env vars (record in `03-CONVENTIONS.md` §3 on close-out)

| Var | Description | Default / placeholder |
|---|---|---|
| `HHT_VENDOR_CALLBACK_WINDOW` | The spoken callback window for vendor callbacks (Numbers-Guard source). | `one business day` |
| `VENDOR_CALLBACK_WEBHOOK_URL` | Optional n8n/CRM secondary sink for vendor callbacks (O-6/O-9). Off when unset. | `(owner-supplied)` |

(Reuses, no new var: `HHT_TRANSFER_NUMBER_{YAKIMA,MTVERNON,PULLMAN}`, `HHT_DEFAULT_STORE`, `STAFF_ALERT_EMAIL[_<STORE>]`, `SLACK_*`, `PHONE_HASH_PEPPER`, `VAPI_*` — all already in `03-CONVENTIONS.md` §3.)

---

## 11. Definition of done (P3)

- All §7 acceptance criteria pass with pasted output (`ruff check`, `ruff format --check`, the targeted `pytest`, `manage.py check`, `makemigrations --check` — the `crm/migrations` `VendorCallback` migration committed and exit 0).
- The manual call script §8.4 step 1 (vendor → warm transfer → no answer → reason captured → `VendorCallback` logged + `[URGENT]` staff alert + callback window stated) is demonstrated with a pasted transcript + the `VendorCallback` + `VoiceCall` rows; step 2 proves a *successful* transfer logs no callback; steps 4–5 prove retail/dispute still route correctly (no regression).
- A re-provision proves zero drift (G2); the `vendor` member carries no product tool (E1) and a non-empty warm-transfer destination (B1).
- Docs updated in the SAME change (`03-CONVENTIONS.md` §6): check off `13-P3-VENDOR-ROUTING.md` in `00-MASTER-ROADMAP.md` §7; record the two new env vars (§10) in `03-CONVENTIONS.md` §3; note the `crm/models.VendorCallback` model + the `entry_router` classifier contract in `01-ARCHITECTURE.md` §8; append an ADR only if a real architectural decision emerged (e.g. the `VendorWebhookSink` secondary sink). Append a `brain/Daily/` line.

---

## 12. Source-file anchors (for the executor)

- **Foundation:** `C:\happytime-voice\docs\plans\{00-MASTER-ROADMAP,01-ARCHITECTURE,02-DECISIONS,03-CONVENTIONS}.md` (ADR-015 = the owner flow; `01-ARCHITECTURE.md` §1.4 = the `vendor` member, §6.3 = the vendor sequence diagram).
- **Sibling phase docs P3 depends on / cross-refs:** P0 `10-P0-CHASSIS-FAQ.md` (§0 fork gate, §3.3 `voice/tools/` registry + `voice/models.VoiceCall`, §4.3 tool envelope, §4.6 `Outcome.VENDOR_CALLBACK`, §4.7 seed map, §3.6 provisioner); P1 `11-P1-DUTCHIE-SUGGESTIONS.md` (§5 step 4 = "classifier taxonomy is P3 work"); P2 `12-P2-ESCALATION-TRANSFER-EMAIL.md` (§4.4 `classify_outcome` `vendor_callback` precedence 4, §4.5/§4.6 `EmailSink`/`SlackSink` + `_recipients_for`, §6.1 `build_*_transfer_tool` warm-transfer shape, §6.2 squad members). P4 `14-P4-dashboard-publish.md` (§3.2 `vendor_queue`/`vendor_callback_update` read P3's `VendorCallback`; §4.3/§4.4 the shared payload builders). P5 `15-P5-polish-brand.md` (§3.2 extends THIS `entry_router` classifier with a cartridge category).
- **swedish-bot (port):** `C:\Users\vladi\OneDrive\Desktop\swedish-bot\crm\models.py` (`phone_hash`~L17; the idempotent `ServiceRequest`/`LeadDelivery` durable-record + status-lifecycle pattern for `VendorCallback`), `crm\sinks.py` (`EmailSink`~L40, `dispatch`~L119, `WebhookSink` shape for the optional `VendorWebhookSink`), `dashboard\views.py` (`session_list`~L93 list/sort/paginate the P4 vendor-queue reuses).
- **Research:** `_research-education-blogs.md` §1 (Numbers-Guard house rule), §8 (Koptza house style for the vendor member tone); the synthesis brief (`…/tasks/wp427jhrt.output`) §1 weakness #6 (no vendor path), §3 capability D (vendor detection + pass-through + callback-to-agent), §4.6 (vendor/escalation/staff-email design).
- **Vapi export (legacy artifact, the design source for tone, NOT wiring):** `C:\Users\vladi\Downloads\happy-time-voice-agent-(full-script)-(uploaded-via-json).json` (Koptza greeting/persona copy; it has NO vendor branch — the gap P3 fills).
- **Deferred Vapi-spec detail:** `20-SPEC-vapi-deploy.md` (the exact async-tool semantics, the `endedReason`/transfer-disposition string set `is_no_answer` maps, the signature header scheme — pinned there, referenced not duplicated).
