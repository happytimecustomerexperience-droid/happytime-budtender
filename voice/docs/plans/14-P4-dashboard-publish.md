# 14 — P4 — DASHBOARD + PUBLISH TO VAPI — Executable Plan

> **Status:** EXECUTABLE SPEC (authoritative for P4). Written 2026-06-22.
> **Subsystem:** S5 (Dashboard + publish). **Implements:** ADR-002/003/010/011/014/016/017/019/020.
> **Read order before executing:** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → this file.
> **Ports from:** `swedish-bot` (`dashboard/views.py`, `dashboard/urls.py`, `templates/dashboard/flow.html`, `kb/models.py` `AgentPrompt`+`FlowConfig`, `crm/sinks.py`). **Expands** with: agents editor, flow canvas (config+docs only), KB manager + KB-source manager + reindex, ranking-weights tuner, live call monitor + call log + transcript, vendor-callback queue, escalation review, specials/hours editor, analytics, and the **Publish to Vapi** action.
>
> **One-line goal:** the owner manages the whole voice stack from one Django dashboard — edits every assistant prompt/model/voice/transfer-number, edits the KB and ranking weights, reviews every call/vendor-callback/escalation — and clicks **Publish to Vapi** to push the local config to the live Squad via `PATCH /assistant/{id}` + `PATCH /squad/{id}`. Safety guardrails stay in version-controlled Python and **cannot be deleted from the UI** (`_clean_graph` fail-closed).

---

## 1. Goal & scope

### 1.1 In scope (this phase ships all of)
A single staff-only Django dashboard app (`dashboard/`) forked from swedish-bot and expanded, exposing:

1. **Agents editor** — one card per Squad member (`entry_router`, `budtender`, `faq`, `vendor`, `escalation`); edit system-prompt body, `model_id`, temperature/max-tokens, `voice_id`, attached `tool_names`, `transfer_number_key`, active flag; inline HTMX save (live for the next call, **server-side**; Vapi only reflects it after Publish). Ported from `agent_config`/`agent_save`/`agent_detail`.
2. **Flow canvas (config + docs ONLY)** — the Alpine/SVG drag canvas ported from `flow.html`, retargeted to the 5 Squad members + Vapi node kinds (`agent`/`handoff`/`tool`/`transfer`/`end`). It documents the Squad shape and per-transition trigger conditions; it **never** edits a runtime guardrail. `_clean_graph` fail-closed validation retained and tightened (role allowlist = the 5 members; node-kind allowlist; MAX_NODES=80).
3. **`agent_prompt_assist`** — Gemini "AI: add to this prompt (never rewrites)" additive editor, ported verbatim in behavior (proposes, never auto-saves; safety rules can only be strengthened).
4. **KB manager + KB-source manager** — list/edit `FAQEntry`/`PolicyDocument`/`StoreFact`/`EducationDoc`/`BlogDoc` rows (FAQ / return-policy / store-facts / WA-law / weights-types taxonomy / education / blogs); add/edit/delete; an **embeddings reindex button** that rebuilds the `kb/semantic.py` cosine cache AND re-mirrors to Vapi Files via `kb/vapi_files.py`.
5. **Ranking-weights tuner** — edit `W_ANON`/`W_KNOWN` weight dicts + the margin-emphasis knob; persisted to a `RankingWeights` singleton; **read by budtender** (pushed over the budtender admin contract — see §6.3). Surfaces the owner's "high margin first" lever.
6. **Live call monitor + call log + transcript view** — a near-real-time list of in-flight + recent `VoiceCall` rows (outcome, store, duration, escalated flag), a paginated searchable call log, and a per-call transcript replay (`VoiceTurn` bubbles + AI summary).
7. **Vendor-callback queue** — list `VendorCallback` rows (open/contacted/closed), mark contacted/closed, re-send the staff alert.
8. **Escalation review** — filtered call log of escalation outcomes (defective-return / repeated-human / dispute) with transcript + the warm-transfer disposition.
9. **Specials/hours editor** — edit weekly specials + per-store hours `StoreFact` rows (the KB the `faq` assistant speaks). Mt Vernon hours gated behind the O-8 "confirm" flag.
10. **Analytics** — call-volume + outcome funnel + suggestion-acceptance + escalation/vendor rates, ported in spirit from `analytics_dashboard`.
11. **Publish to Vapi** — the control-plane action: maps each edited `AgentPrompt` → `PATCH /assistant/{id}` and the Squad shape → `PATCH /squad/{id}` via `core/services/vapi.py`; writes back any new ids; reports a per-object publish result. Idempotent (GET-then-PATCH).

### 1.2 Out of scope (other phases / EXP)
- Creating the Vapi objects from nothing — that is `tools/provision_vapi.py` (P0, ADR-003). P4 **publishes edits to already-provisioned objects**; if an id is missing it calls provision's upsert, never a blind POST loop in a view.
- The tool *handlers* themselves (`voice/tools/*`) — owned by P0/P1/P3.
- Brand visuals / theme (logo/hex/fonts) — P5 (O-10); P4 ships the neutral theme.
- Web-chat widget, SMS, pgvector — EXP.

### 1.3 Non-negotiable boundaries (binding)
- **The flow canvas is config + docs only.** Safety guardrails live in `voice/guardrails.py` (version-controlled) and the Squad transition *trigger conditions* live in code/provisioning; the canvas can re-arrange and document them but a Publish can NEVER delete a guardrail or a required transition. `_clean_graph` fail-closed (MAX_NODES=80, role allowlist, kind allowlist, coord clamp, char caps) is the enforcement.
- **Numbers-Guard + Leak-Guard hold in the dashboard too** — the weights tuner edits *weights*, never per-product cost/margin; the call log/transcript views render only KB-grounded + budtender-`public_product` fields; the dashboard never displays a product's cost/margin (it never has them — budtender's allowlist serializer never sent them).
- **Staff-only.** Every view is `@staff_member_required`. The Vapi webhook surface (`/api/voice/vapi`) is HMAC-gated and is NOT part of the dashboard; the dashboard is cookie-session + CSRF, behind `CSRF_TRUSTED_ORIGINS`.
- **Publish is GET-then-PATCH, idempotent, fail-loud.** A re-publish with no edits produces zero drift. A publish failure is surfaced per-object, never silently swallowed.

---

## 2. Dependencies (what MUST exist first)

P4 is **serial after P1–P3** (roadmap §4/§6) — the Publish editor needs every assistant/tool/squad row to exist as code so the editor has real rows to map to `PATCH`. Concretely, before P4 starts:

| # | Dependency | Where it comes from | What P4 consumes from it |
|---|---|---|---|
| D1 | `core/services/vapi.py` REST client (GET/PATCH on `/assistant`, `/squad`, `/tool`, `/phone-number`; Bearer `VAPI_PRIVATE_KEY`; base `https://api.vapi.ai`; never `/workflow`) | **P0** (roadmap §7) | The Publish action calls `vapi.get_assistant(id)` then `vapi.patch_assistant(id, payload)`; same for squad. |
| D2 | `tools/provision_vapi.py` idempotent provisioner — creates the Squad + 5 assistants + tools + phone number, writes `assistantId`/`squadId`/`toolId`/`phoneNumberId` back onto local rows | **P0** (ADR-003) | Publish reuses provision's per-assistant upsert when an id is missing; the editor reads the written-back ids. |
| D3 | `kb/models.AgentPrompt` (forked) + `FlowConfig` (singleton JSON graph) | **P0** ports `kb/models.py` (swedish-bot `AgentPrompt`~L226, `FlowConfig`~L255). P4 EXTENDS `AgentPrompt` with the voice-specific fields in §4.1. | The agents editor + flow canvas + Publish read/write these rows. |
| D4 | `voice/models.VoiceCall` / `VoiceTurn` / `Outcome` durable call log (written by `voice/webhooks.py::end_of_call_report`) | **P0** (the durable record); enriched outcomes by **P1/P2/P3** | The call monitor, call log, transcript view, escalation review, analytics read these. |
| D5 | `crm/models.VendorCallback` (idempotent on `(store, transfer_id)` or `(store, caller_phone_hash, day)`) + `crm/sinks.py` `EmailSink`/`dispatch` | **P3** creates the model + handler; **P2** wires `crm/sinks.py` (forked from swedish-bot) | The vendor-callback queue lists/mutates these + re-fires the alert. |
| D6 | `kb/semantic.py` (cosine cache, content-hash keyed) + `kb/vapi_files.py` (KB → Vapi Files + Query Tool mirror) | **P0** ports `kb/semantic.py`; `kb/vapi_files.py` is net-new in P0 | The reindex button calls the rebuild + re-mirror. |
| D7 | The 5 Vapi assistants + 1 Squad + the tool set actually provisioned (real ids on rows) | **P0–P3** (provision run) | Publish targets these ids; with no ids the editor shows "not yet provisioned — run provision". |
| D8 | budtender admin/weights contract reachable (push `RankingWeights` → budtender) OR a documented stub | **budtender** (O-1) — see §6.3 | The weights tuner persists locally always; pushes to budtender when reachable, else "saved locally; will sync when budtender is reachable". |

**Graceful-degradation rule (so P4 is not hard-blocked by env placeholders):** every dependency that is an owner-supplied env placeholder (transfer numbers O-4, budtender base URL O-1, Vapi phone id O-4, Slack O-9) is *read*, never *required at import*. The editor renders, saves locally, and reports "publish/sync deferred — <placeholder> not configured" instead of crashing.

---

## 3. File-by-file task list

Each entry: **exact path → responsibility → key functions/shape → source file to port from (with path)**. New files marked ★; ported files cite the swedish-bot original.

### 3.1 `dashboard/` (the app)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `dashboard/views.py` | All staff views: agents editor, flow canvas, KB+source manager + reindex, weights tuner, call monitor/log/transcript, vendor queue, escalation review, specials/hours, analytics, **Publish to Vapi**. | See §3.2 (function inventory). | `swedish-bot/dashboard/views.py` (whole file: `overview`, `analytics_dashboard`, `agent_config`~L280, `agent_save`~L288, `agent_detail`~L355, `agent_prompt_assist`~L385, `flow_canvas`~L492, `_clean_graph`~L511, `_coord`~L563, `flow_save`~L572, `default_flow_graph`~L430, `kb_manager`, `faq_list`, `session_list`/`session_detail`/`conversation_replica`, `_toast`~L26, `_resolve_sort`~L31, `_querystring`~L46). |
| `dashboard/urls.py` | URL map for every view. | `app_name`-free named routes `dash-*` (see §3.5). | `swedish-bot/dashboard/urls.py` (whole file). |
| `dashboard/forms.py` ★ | ModelForms for KB rows (`FAQEntryForm`, `PolicyForm`, `StoreFactForm`, `EducationDocForm`, `BlogDocForm`, `SpecialsForm`), `RankingWeightsForm`, `VendorCallbackForm`. | Standard Django `ModelForm`s; validate weight dicts sum≈1 (warn, not block — owner override wins). | `swedish-bot/dashboard/forms.py` (the form-module shape; the actual fields are voice-domain). |
| `dashboard/publish.py` ★ | The Publish-to-Vapi mapping logic, isolated from views (thin views call it). | `publish_assistant(prompt) -> PublishResult`, `publish_squad() -> PublishResult`, `publish_all() -> list[PublishResult]`, `build_assistant_payload(prompt)`, `build_squad_payload()`. See §5. | net-new; uses `core/services/vapi.py` (D1) + `tools/provision_vapi.py` upsert. |
| `dashboard/weights.py` ★ | Read/write the `RankingWeights` singleton + push to budtender. | `get_weights()`, `save_weights(data)`, `push_to_budtender(weights) -> SyncResult`. | net-new; uses `voice/budtender_client.py` (P1). |
| `dashboard/monitor.py` ★ | Call-monitor query helpers (in-flight vs recent; outcome badges). | `live_calls()`, `recent_calls(filters)`, `call_outcome_badge(outcome)`. | net-new; reads `voice/models.VoiceCall`. |

### 3.2 `dashboard/views.py` — function inventory (what to write)

Ported / adapted (keep helper utilities `_toast`, `_resolve_sort`, `_querystring`, `PER_PAGE` verbatim):

- `overview(request)` — KPI pills (calls today, escalations, vendor callbacks, suggestion-accept rate) + the at-risk banner. *(adapt `overview`+`analytics_dashboard`).*
- `analytics_dashboard(request)` — call-volume trend, outcome funnel, suggestion acceptance, escalation/vendor rate, `days∈{7,30,90}`. *(adapt `analytics_dashboard`~L71).*
- `agent_config(request)` — list the 5 `AgentPrompt` cards. *(port `agent_config`~L280).*
- `agent_save(request, pk)` — inline HTMX save of one assistant's editable config **incl. the new voice fields** (`voice_id`, `tool_names`, `transfer_number_key`, `vapi_model`); fail-closed numeric validation. Returns the re-rendered card + a toast "updated — live server-side; click Publish to push to Vapi". *(port `agent_save`~L288, extend the field list).*
- `agent_detail(request, role)` — per-assistant full editor page. *(port `agent_detail`~L355; replace `AGENT_FLOW` with `VOICE_AGENT_FLOW`, §3.3).*
- `agent_prompt_assist(request, pk)` — Gemini additive prompt assist (proposes, never saves; safety only strengthened). *(port `agent_prompt_assist`~L385 verbatim; swap `_ASSIST_SYSTEM` copy to the Happy Time persona, §3.4).*
- `flow_canvas(request)` — render the canvas with the voice graph + the 5 agents. *(port `flow_canvas`~L492; `agents` dict adds `voice_id`/`tool_names`/`transfer_number_key`).*
- `flow_save(request)` — POST the cleaned graph. *(port `flow_save`~L572 + `_clean_graph`~L511 + `_coord`~L563; retarget `NODE_KINDS`/`_AGENT_ROLES`, §3.3).*

New (net-new for the voice expansions):

- `kb_manager(request)` — landing: source groups (FAQ / Return-policy / Store-facts / WA-law / Taxonomy / Education / Blogs) with counts. *(adapt `kb_manager`~L672 grouping pattern).*
- `kb_source_list(request, kind)` — list/search rows of one KB kind.
- `kb_row_new(request, kind)` / `kb_row_edit(request, pk)` / `kb_row_delete(request, pk)` — CRUD a KB row (FAQ/policy/store-fact/education/blog/special/hours). *(adapt `faq_entry_new`/`site_faq_new`~L900-934 + the create-form pattern).*
- `kb_reindex(request)` — `@require_POST`: rebuild `kb/semantic.py` cache + `kb/vapi_files.py.mirror_all()`; toast with counts. Long-running → dispatched but bounded (see §8 risk).
- `weights_tuner(request)` — GET render + POST save `W_ANON`/`W_KNOWN`/margin-emphasis; on save calls `weights.push_to_budtender`. *(net-new; `RankingWeightsForm`).*
- `call_monitor(request)` — live + recent calls (HTMX `hx-trigger="every 5s"` poll on the live strip). *(net-new; `monitor.live_calls`).*
- `call_log(request)` — paginated/searchable/sortable `VoiceCall` list. *(adapt `session_list`~L93 + `_resolve_sort`).*
- `call_detail(request, pk)` — one call: metadata + outcome + AI summary + transcript replay + (if escalation) transfer disposition. *(adapt `session_detail`~L123 + `conversation_replica`~L146).*
- `call_transcript(request, pk)` — read-only `VoiceTurn` bubble replay partial (popup/modal). *(adapt `conversation_replica`~L146).*
- `escalation_review(request)` — call_log pre-filtered to escalation outcomes + reason facets. *(net-new; thin wrapper over call_log).*
- `vendor_queue(request)` — list `VendorCallback` (status filter). *(net-new; paginate like `session_list`).*
- `vendor_callback_update(request, pk)` — `@require_POST`: mark contacted/closed; optional re-fire alert via `crm/sinks.dispatch`. *(net-new).*
- `publish_vapi(request)` — `@require_POST`: `publish.publish_all()`; render per-object result + toast. *(net-new; §5).*
- `publish_assistant_one(request, pk)` — `@require_POST`: publish a single assistant (the per-card "Publish this" button). *(net-new).*

### 3.3 `dashboard/views.py` constants to retarget (from swedish-bot's HVAC graph → voice Squad)

```python
# Vapi node kinds the canvas can place (docs/config only):
NODE_KINDS = ["agent", "handoff", "tool", "transfer", "end"]
MAX_NODES, MAX_EDGES, MAX_COLLECT = 80, 160, 30
# the role allowlist = exactly the 5 Squad members (fail-closed in _clean_graph):
_AGENT_ROLES = {"entry_router", "budtender", "faq", "vendor", "escalation"}

# Replaces swedish-bot AGENT_FLOW — staff-facing blurb + step per Squad member:
VOICE_AGENT_FLOW = {
  "entry_router": {"step": "1", "blurb": "Greets as Koptza, confirms 21+, classifies intent in one turn → budtender / faq / vendor / escalation."},
  "budtender":    {"step": "2", "blurb": "Slot-fills, calls suggest_products / check_inventory / pair_upsell; speaks OTD prices + why_this; one gated upsell."},
  "faq":          {"step": "2", "blurb": "Grounded answers from the KB (hours/returns/limits/payment/pickup/weights-types). Numbers come from KB rows only."},
  "vendor":       {"step": "2", "blurb": "Never retail. Warm-transfers to the store; on no-answer captures the reason → VendorCallback + staff alert + callback window."},
  "escalation":   {"step": "✓", "blurb": "≥2 human requests / return dispute / defective return → warm transferCall with {{transcript}} summary to the per-location number."},
}
```

`default_flow_graph()` (replaces `default_flow_graph`~L430) seeds the canvas to mirror the live Squad: nodes for the 5 members (`kind:"agent"`), `transfer`/`end` terminals, and `handoff` edges carrying the **trigger condition** as the edge label + a read-only `collect` describing slots (informational only — the canvas does not drive runtime). Trigger labels: entry→budtender "retail intent"; entry→faq "info intent"; entry→vendor "vendor/wholesale/manifest"; entry→escalation "≥2 human / dispute / defective"; budtender→escalation "human request mid-flow"; faq→{budtender,escalation}; vendor→escalation; escalation→transfer "warm transfer".

### 3.4 `agent_prompt_assist` system-prompt copy (swap)

Port `_ASSIST_SYSTEM` (swedish-bot `dashboard/views.py`~L365) verbatim in structure; swap the company line to: *"…editing the SYSTEM PROMPT of a production AI **voice** budtender for **Happy Time Weed** (a family-owned Washington cannabis retailer; persona 'Koptza')."* Keep the safety clause unchanged — additions only, never weaken a guardrail, never reduce safety, output only the new full prompt. Use `core/services/gemini.py` (`MODELS["flash"]`), temp 0.2, max 8192, strip code fences. (Server-side LLM stays Gemini per ADR-010, even though the assistants run gpt-4.1-mini.)

### 3.5 `dashboard/urls.py` — named routes (extend swedish-bot's)

```
""                          dash-overview
"analytics/"                dash-analytics
# agents
"agents/"                   dash-agents
"agents/<int:pk>/save"      dash-agent-save
"agents/<int:pk>/assist"    dash-agent-assist
"agents/<int:pk>/publish"   dash-agent-publish          # ★ per-card publish
"agents/<slug:role>/"       dash-agent-detail
# flow canvas (config+docs only)
"flow/"                     dash-flow
"flow/save"                 dash-flow-save
# KB + KB-source manager + reindex
"kb/"                       dash-kb
"kb/<slug:kind>/"           dash-kb-source              # FAQ/policy/store-fact/education/blog/special/hours
"kb/<slug:kind>/new/"       dash-kb-row-new
"kb/row/<int:pk>/"          dash-kb-row-edit
"kb/row/<int:pk>/delete"    dash-kb-row-delete
"kb/reindex"                dash-kb-reindex             # ★ embeddings + Vapi Files re-mirror
# ranking weights
"weights/"                  dash-weights                # ★
# calls
"calls/"                    dash-calls                  # live monitor (HTMX poll)
"calls/log/"                dash-call-log
"calls/<int:pk>/"           dash-call-detail
"calls/<int:pk>/transcript" dash-call-transcript
"escalations/"              dash-escalations            # ★ filtered call log
# vendor callbacks
"vendor-callbacks/"         dash-vendor-queue           # ★
"vendor-callbacks/<int:pk>/update" dash-vendor-update   # ★
# specials/hours live under kb/<kind> (special, hours)
# publish to Vapi
"publish/"                  dash-publish                # ★ publish all
```

### 3.6 Templates (`templates/dashboard/`)

Port the swedish-bot template chassis (`base.html` with the toast stack + HTMX + self-hosted Alpine; `_chat_replica.html` bubble partial; the list/sort/paginate partials) and add the voice surfaces. New/retargeted templates:

| Template | Purpose | Port from |
|---|---|---|
| `base.html` | Shell: nav (Overview · Agents · Flow · KB · Weights · Calls · Escalations · Vendor · Analytics · **Publish**), toast stack, HTMX, Alpine vendored, neutral theme. | `swedish-bot/templates/dashboard/base.html`. |
| `agent_config.html` / `_agent_card.html` / `agent_detail.html` | Agents editor cards + full editor; the new fields (`voice_id`, `tool_names` multiselect, `transfer_number_key` select, `vapi_model`). | swedish-bot `agent_config.html`/`_agent_card.html`/`agent_detail.html`. |
| `flow.html` ★retarget | The Alpine/SVG canvas, retargeted: `kinds` = the 5 voice kinds; `agents` adds voice fields; the green banner reworded to "documents the Squad; guardrails stay in code". | swedish-bot `templates/dashboard/flow.html` (whole file — keep the JS `flowCanvas()` engine; only change LABELS/ICONS/HEADERS, the agent-panel fields, and the banner copy). |
| `kb_manager.html` / `kb_source.html` / `kb_form.html` | KB source groups + per-kind list + create/edit form + the **Reindex** button. | swedish-bot `kb_manager.html`/`faq.html`/`faq_form.html`. |
| `weights.html` ★ | Ranking-weights tuner: two weight grids (W_ANON/W_KNOWN) + margin-emphasis slider + "push to budtender" status. | net-new. |
| `calls.html` ★ | Live monitor strip (HTMX `every 5s`) + recent table. | net-new; reuse list partials. |
| `call_log.html` / `call_detail.html` / `_call_transcript.html` | Paginated log + per-call detail + transcript bubbles. | swedish-bot `session_list.html`/`session_detail.html`/`_chat_replica.html`. |
| `escalations.html` ★ | Escalation-filtered log + reason facets. | reuse `call_log.html`. |
| `vendor_queue.html` ★ | Vendor-callback queue + status actions. | net-new; reuse list partials. |
| `publish.html` / `_publish_result.html` ★ | Publish page: per-object diff/result table + "Publish all" + "Publish this assistant". | net-new. |
| `analytics.html` | Call analytics. | swedish-bot `analytics.html`. |

### 3.7 Models touched (extend, don't re-invent)

- `kb/models.AgentPrompt` (forked in P0) — **extend** with voice fields (§4.1). Migration in `kb/migrations`.
- `kb/models.FlowConfig` — used as-is (singleton JSON graph).
- New KB models (P0 seeds them; P4 edits them): `FAQEntry`, `PolicyDocument`, `StoreFact`, `EducationDoc`, `BlogDoc`, `WeeklySpecial` (or `StoreFact` rows tagged `kind=special`/`hours`).
- `dashboard/models.RankingWeights` ★ — singleton; §4.2.
- `voice/models.VoiceCall`/`VoiceTurn` (P0) — read-only here.
- `crm/models.VendorCallback` (P3) — read + status mutate here.

---

## 4. Data contracts / JSON schemas

### 4.1 `AgentPrompt` voice extension (the publish source row)

P0 forks `AgentPrompt` (swedish-bot `kb/models.py`~L226). P4 adds the fields the Publish mapping needs. Final shape:

```python
class AgentPrompt(models.Model):
    role = CharField(max_length=32, unique=True,
        choices=[("entry_router",…),("budtender",…),("faq",…),("vendor",…),("escalation",…)])
    body = TextField()                      # the system prompt (Koptza persona for entry_router)
    model_id = CharField(max_length=64)     # server-side default (Gemini) — NOT the Vapi model
    # runtime knobs (server-side, ported):
    temperature = FloatField(null=True, blank=True)
    max_output_tokens = IntegerField(null=True, blank=True)
    is_active = BooleanField(default=True)
    prompt_version = IntegerField(default=1)
    updated_at = DateTimeField(auto_now=True)
    # ── P4 voice/Vapi fields (the publish payload source) ──
    vapi_assistant_id = CharField(max_length=64, blank=True)   # written by provision; PATCH target
    vapi_model = CharField(max_length=64, default="gpt-4.1-mini")  # ADR-010 single model
    voice_id = CharField(max_length=64, default="a3520a8f-226a-428d-9fcd-b0a4711a6829")  # Cartesia sonic-3 Koptza
    tool_names = JSONField(default=list)    # e.g. ["faq_lookup"] / ["suggest_products","check_inventory","pair_upsell"]
    transfer_number_key = CharField(max_length=32, blank=True)  # "YAKIMA"/"MTVERNON"/"PULLMAN" → env HHT_TRANSFER_NUMBER_*
    last_published_at = DateTimeField(null=True, blank=True)
    last_publish_hash = CharField(max_length=64, blank=True)    # sha256 of last-published payload → zero-drift detection
```

> Voice/transcriber/model are set **once per assistant** (ADR-011) — `voice_id`/`vapi_model` live on the row, never per node. The transcriber (Deepgram nova-3 + the ~33-term keyterm list) is a single shared constant (`voice/constants.py::DEEPGRAM_KEYTERMS`), not editable per row in v1 (it's the same for all members).

### 4.2 `RankingWeights` singleton

```python
class RankingWeights(models.Model):     # singleton (pk forced to 1)
    w_anon  = JSONField(default=dict)   # {"margin":0.55,"affinity":0.0,"effect":0.18,"category":0.05,"bucket":0.12,"quality":0.0,"budget":0.10}
    w_known = JSONField(default=dict)   # {"margin":0.22,"affinity":0.34,"effect":0.10,"category":0.04,"bucket":0.12,"quality":0.14,"budget":0.04}
    margin_emphasis = FloatField(default=1.0)   # multiplier on the margin term for the anon set (owner's "more margin" lever)
    updated_at = DateTimeField(auto_now=True)
    last_synced_at = DateTimeField(null=True, blank=True)
```

Defaults = budtender's `W_ANON`/`W_KNOWN` (research §2.1) so a fresh install is byte-identical to budtender's current behavior. The form WARNS (does not block) if a weight set doesn't sum≈1.0 — owner override wins (per global rules), but the tuner shows the normalized preview budtender will apply.

### 4.3 `build_assistant_payload(prompt)` → Vapi `PATCH /assistant/{id}` body

```json
{
  "name": "entry_router",
  "model": {
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "messages": [{ "role": "system", "content": "<AgentPrompt.body, vars hydrated>" }],
    "temperature": 0.3,
    "maxTokens": 250,
    "toolIds": ["<vapi tool id for faq_lookup>"]
  },
  "voice": {
    "provider": "cartesia",
    "voiceId": "a3520a8f-226a-428d-9fcd-b0a4711a6829",
    "model": "sonic-3",
    "experimentalControls": { "emotion": ["positivity:highest"] }
  },
  "transcriber": {
    "provider": "deepgram",
    "model": "nova-3",
    "keyterms": ["flower","dabs","wax","shatter","resin","carts","510","disposable","gummies","tincture", "...33 terms..."]
  },
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" }
}
```

- `toolIds` is resolved from `AgentPrompt.tool_names` → the local tool rows' `vapi_tool_id` (written by provision). If a name has no provisioned id, Publish reports "tool not provisioned: <name>" for that assistant and skips the PATCH (fail-loud, never PATCH a dangling tool).
- For `escalation`/`vendor`, the payload also carries the warm-transfer config built from `transfer_number_key` → `settings.HHT_TRANSFER_NUMBER_<KEY>`:

```json
"model": { "...": "...", "tools": [{
  "type": "transferCall",
  "destinations": [{
    "type": "number",
    "number": "${HHT_TRANSFER_NUMBER_YAKIMA}",
    "transferPlan": {
      "mode": "warm-transfer-wait-for-operator",
      "summaryPlan": { "enabled": true,
        "messages": [{ "role": "system", "content": "Summarize for the operator: {{transcript}}" }] }
    }
  }]
}]}
```

If the transfer number env is unset (O-4 placeholder), Publish substitutes a documented placeholder and flags "transfer number not configured for <KEY>" — does not block the rest of the publish.

### 4.4 `build_squad_payload()` → Vapi `PATCH /squad/{id}` body

```json
{
  "name": "Happy Time Voice",
  "members": [
    { "assistantId": "<entry_router id>",
      "assistantDestinations": [
        { "type": "assistant", "assistantName": "budtender",  "message": "", "description": "retail intent" },
        { "type": "assistant", "assistantName": "faq",        "message": "", "description": "info intent" },
        { "type": "assistant", "assistantName": "vendor",     "message": "", "description": "vendor/wholesale/manifest" },
        { "type": "assistant", "assistantName": "escalation", "message": "", "description": ">=2 human / dispute / defective" }
      ] },
    { "assistantId": "<budtender id>",  "assistantDestinations": [{ "type":"assistant","assistantName":"escalation","description":"human request mid-flow" }] },
    { "assistantId": "<faq id>",        "assistantDestinations": [{ "type":"assistant","assistantName":"budtender" }, { "type":"assistant","assistantName":"escalation" }] },
    { "assistantId": "<vendor id>",     "assistantDestinations": [{ "type":"assistant","assistantName":"escalation" }] },
    { "assistantId": "<escalation id>", "assistantDestinations": [] }
  ]
}
```

The destinations are derived from the **code-defined** Squad shape (`01-ARCHITECTURE.md` §1.6), NOT freely from the canvas. The canvas's `FlowConfig` graph is validated against this shape on Publish: a canvas edge that would *remove* a required transition (e.g. budtender→escalation) is ignored (the required set is re-asserted from code); a canvas edge that adds an *out-of-allowlist* destination is rejected by `_clean_graph`. This is the "guardrails cannot be deleted from the UI" enforcement at the publish boundary.

### 4.5 `PublishResult` (returned to the UI)

```json
{
  "object": "assistant", "role": "budtender",
  "id": "asst_...", "action": "patched|created|skipped|error|nodrift",
  "drift": true,
  "changed_fields": ["model.messages", "model.toolIds"],
  "error": null,
  "warnings": ["transfer number not configured for MTVERNON"]
}
```

`action="nodrift"` when the new payload sha256 == `AgentPrompt.last_publish_hash` (idempotency — re-publishing with no edits is a no-op and an acceptance criterion).

### 4.6 budtender weights push contract (§6.3)

`POST {HHT_BUDTENDER_BASE_URL}/api/v1/admin/ranking-weights` (Bearer `HHT_BACKEND_TOKEN`), body `{ "w_anon": {...}, "w_known": {...}, "margin_emphasis": 1.0 }` → `{ "ok": true, "applied": {...normalized...} }`. **If budtender does not yet expose this admin endpoint (O-1), the push is a documented no-op** that returns `{"ok": false, "reason": "endpoint not available"}`; the weights stay persisted locally and the tuner shows "saved locally; budtender sync pending". (This is the one place P4 touches budtender's *admin* surface; the data-plane read endpoints are P1's.)

---

## 5. Publish-to-Vapi: the control-plane action (detail)

`dashboard/publish.py`:

```
publish_all() -> list[PublishResult]:
    1. for each AgentPrompt (5 members), in dependency order (assistants before squad):
         publish_assistant(prompt)
    2. publish_squad()
    3. return [results]

publish_assistant(prompt) -> PublishResult:
    if not prompt.vapi_assistant_id:
        # never blind-POST in a view; reuse the idempotent provisioner upsert
        id = provision_vapi.ensure_assistant(prompt)   # GET-by-name then POST-once
        prompt.vapi_assistant_id = id; prompt.save()
    payload = build_assistant_payload(prompt)
    h = sha256(canonical_json(payload))
    if h == prompt.last_publish_hash:
        return PublishResult(action="nodrift", ...)     # zero-drift idempotency
    current = vapi.get_assistant(prompt.vapi_assistant_id)   # GET-then-PATCH
    changed = diff(current, payload)
    vapi.patch_assistant(prompt.vapi_assistant_id, payload)  # PATCH only
    prompt.last_publish_hash = h; prompt.last_published_at = now(); prompt.save()
    return PublishResult(action="patched", changed_fields=changed, ...)

publish_squad() -> PublishResult:
    payload = build_squad_payload()              # destinations re-asserted from CODE, not the canvas
    vapi.patch_squad(settings.VAPI_SQUAD_ID, payload)   # GET-then-PATCH
    ...
```

**Invariants (binding):**
- **GET-then-PATCH, never blind POST in a request handler.** Object creation only via the provisioner upsert (ADR-003). A re-run with no edits → all `nodrift`/`skipped` → zero drift (acceptance criterion).
- **Squad destinations come from code** (`build_squad_payload` reads the architecture's fixed shape), so the canvas cannot delete a required transition. The canvas edits *documentation + labels*; the runtime topology is code-owned.
- **Fail-loud per object.** A 4xx/5xx from Vapi on assistant N is captured in that `PublishResult.error` and does NOT abort the others; the UI shows a per-row pass/fail. The whole action never 500s the dashboard (mirror `agent_prompt_assist`'s 502-on-AI-failure pattern).
- **Tool ids resolved or skipped.** A `tool_name` without a provisioned `vapi_tool_id` → that assistant's PATCH is skipped with a clear warning (never PATCH a dangling toolId).
- **Secrets never logged.** `VAPI_PRIVATE_KEY`/`VAPI_WEBHOOK_SECRET`/`HHT_BACKEND_TOKEN` never appear in `PublishResult` or logs; payload logging redacts `server.secret`.

---

## 6. Vapi deploy steps (what this phase actually calls)

P4 does **not** create the Squad/assistants (that's provision, P0). P4's deploy surface is the **edit→publish** path:

1. **Prereq:** `tools/provision_vapi.py` has run once → the Squad + 5 assistants + tool set + phone number exist; each `AgentPrompt.vapi_assistant_id`, each tool row's `vapi_tool_id`, and `settings.VAPI_SQUAD_ID` are populated.
2. **Edit** an assistant in the dashboard (prompt/model/voice/tool selection/transfer key) → `agent_save` persists locally (server-side live immediately for any server-side turn logic; Vapi unchanged yet).
3. **Publish** (`dash-publish` or per-card `dash-agent-publish`):
   - `PATCH /assistant/{vapi_assistant_id}` with `build_assistant_payload` (system prompt, `gpt-4.1-mini`, Cartesia sonic-3 Koptza voice, Deepgram nova-3 + keyterms, resolved `toolIds`, warm `transferPlan`+`summaryPlan` for vendor/escalation, `server.url`+`secret`).
   - `PATCH /squad/{VAPI_SQUAD_ID}` with `build_squad_payload` (members + `assistantDestinations` from the code-defined shape).
4. **Verify:** GET the assistant/squad back; assert the changed fields match; write `last_published_at`/`last_publish_hash`.

### 6.3 budtender weights sync (separate from Vapi)
Saving the weights tuner calls `weights.push_to_budtender` → `POST /api/v1/admin/ranking-weights` (§4.6). This is NOT a Vapi call; it tunes the suggestion engine the `budtender` assistant consumes at suggestion time. Degrades to local-only when the endpoint is absent (O-1).

---

## 7. Acceptance criteria (testable, concrete)

Each is a concrete assertion (mirrors roadmap §5 P4 + the success criteria there).

**A. Agents editor**
- A1. Editing an `AgentPrompt` (body + `vapi_model` + `voice_id` + `tool_names` + `transfer_number_key`) and POSTing `agent_save` persists all fields and re-renders the card with the "updated — live server-side; Publish to push to Vapi" toast. Numeric fields out of range (temp >2, tokens <1) are rejected with a field error and the row is NOT saved.
- A2. `agent_prompt_assist` returns a COMPLETE proposed prompt that contains the original body verbatim plus the requested addition; an instruction that asks to *remove a safety rule* yields a proposal that still contains the safety rule (never weakened). The proposal is NOT auto-saved.

**B. Flow canvas (config+docs only)**
- B1. `flow_save` with a graph containing a node whose `role` is not in `{entry_router,budtender,faq,vendor,escalation}` → HTTP 400 `"unknown agent role"`, nothing persisted (fail-closed).
- B2. `flow_save` with `len(nodes) > 80` → 400 `"graph too large"`. With a node `kind` not in `NODE_KINDS` → 400. With coords beyond bounds → clamped to `[0,6000]`, not rejected.
- B3. A canvas edit that DELETES the `budtender→escalation` transition, then Publish → the published squad payload STILL contains `budtender→escalation` (re-asserted from code). The UI shows a note "required transition restored from policy". (Guardrails cannot be deleted from the UI.)

**C. KB + source manager + reindex**
- C1. CRUD on each KB kind (FAQ/policy/store-fact/education/blog/special/hours) creates/edits/deletes the row; the change is visible to the `faq_lookup` tool's KB read on the next call (no redeploy) — assert by reading `kb/` after the edit.
- C2. `kb_reindex` rebuilds the `kb/semantic.py` cache (content-hash changes) AND calls `kb/vapi_files.py.mirror_all()`; the toast reports `{n} chunks reindexed, {m} files mirrored`. With `kb/vapi_files` unconfigured (no Vapi file API key), it reindexes locally and reports "Vapi mirror skipped (not configured)".

**D. Weights tuner**
- D1. Saving `W_ANON`/`W_KNOWN`/`margin_emphasis` persists the `RankingWeights` singleton; defaults on a fresh DB equal budtender's `W_ANON`/`W_KNOWN` exactly.
- D2. A weight set not summing to 1.0 is saved (owner override) but the form shows the normalized preview; on save, `push_to_budtender` is attempted and the result ("synced" / "saved locally; budtender sync pending") is shown.

**E. Call monitor / log / transcript / escalation review**
- E1. `call_log` lists `VoiceCall` rows paginated (25/page), searchable (by outcome/store/phone-hash prefix), sortable by an allowlisted column (no arbitrary `order_by`).
- E2. `call_detail` renders the AI summary + a `VoiceTurn` bubble transcript; an escalation call shows the transfer disposition (transferred / no-answer).
- E3. `escalation_review` shows only escalation-outcome calls with a reason facet (defective_return / repeated_human / dispute).
- E4. `call_monitor` live strip refreshes via HTMX `every 5s` and shows in-flight calls (status not yet `ended`).

**F. Vendor-callback queue**
- F1. `vendor_queue` lists `VendorCallback` rows with status; `vendor_callback_update` marks contacted/closed and (optionally) re-fires the staff alert via `crm/sinks.dispatch`, returning a toast.

**G. Publish to Vapi**
- G1. With provisioned ids, `publish_vapi` issues a `PATCH /assistant/{id}` for each edited member and one `PATCH /squad/{id}`; the live assistant (GET-back) reflects the new system prompt/model/voice/toolIds/transferPlan. (Test against a mocked `vapi.py`; asserts the exact payload shape of §4.3/§4.4.)
- G2. **Idempotency / zero-drift:** publishing twice with no edits in between → the second run returns all `action="nodrift"` and issues **zero** PATCH calls (assert mock call count == 0 on the second run).
- G3. A Vapi 4xx on one assistant is captured in that `PublishResult.error`, the other assistants still publish, and the dashboard does NOT 500.
- G4. A `tool_name` with no provisioned `vapi_tool_id` → that assistant is `action="skipped"` with `warnings=["tool not provisioned: <name>"]`; no dangling-tool PATCH is sent.

**H. Security**
- H1. Every dashboard view requires `@staff_member_required` — an anonymous GET to each route redirects to login (assert for all `dash-*` routes via a parametrized test).
- H2. No view renders a product cost/margin (the data is never present); a grep-style contract test asserts no `cost`/`margin` field is read from any budtender response shape used in the dashboard.
- H3. Secrets (`VAPI_PRIVATE_KEY`, `VAPI_WEBHOOK_SECRET`, `HHT_BACKEND_TOKEN`) never appear in any rendered template, `PublishResult`, or log line.

---

## 8. Test plan

**Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network):**
- `_clean_graph`: role allowlist (B1), node-kind allowlist, MAX_NODES/MAX_EDGES (B2), `_coord` clamp, MAX_COLLECT, char caps — port swedish-bot's `_clean_graph` tests and retarget the roles.
- `build_assistant_payload` / `build_squad_payload`: exact JSON shape (§4.3/§4.4) for each of the 5 members; voice/transcriber set once (no per-node dup — assert the keyterm list appears exactly once); transfer payload built from `transfer_number_key`; tool-name→toolId resolution + the skip-on-missing path (G4).
- `RankingWeights` defaults == budtender `W_ANON`/`W_KNOWN`; sum-≈1 warning logic (D1/D2).
- `agent_save` numeric validation (A1); the new-field persistence.
- `publish.publish_assistant` zero-drift hash logic (G2): same payload → `nodrift`, no `vapi.patch_*` call.
- `agent_prompt_assist` fence-strip + the safety-preservation contract (A2) with Gemini mocked.

**Contract (`pytest -m integration`, Vapi + budtender mocked/recorded):**
- Publish-all against a mocked `core/services/vapi.py`: assert exactly N `patch_assistant` + 1 `patch_squad` on the first run; **0** on the immediate re-run (G2). Per-object error isolation (G3).
- The squad-shape re-assertion: feed a `FlowConfig` graph missing `budtender→escalation`, publish, assert it is present in the squad payload (B3).
- `weights.push_to_budtender` against a stubbed budtender admin endpoint (success path) and against a 404 (degrade-to-local path) (D2).
- **Leak-Guard:** assert no `cost`/`margin` substring in any dashboard-rendered call/transcript/suggestion context (H2) — reuse the P1 contract-test fixtures for budtender responses.
- Staff-gate sweep: parametrized over every `dash-*` route, anonymous → 302 to login (H1).

**Manual (the phase's definition of done — paste evidence):**
1. Run `provision_vapi.py` (sandbox key) → 5 assistants + squad + tools exist; ids written back.
2. In the dashboard: edit `budtender`'s system prompt (add one sentence), change `vapi_model` is left as `gpt-4.1-mini`, ensure `tool_names=["suggest_products","check_inventory","pair_upsell"]` → Save → Publish.
3. GET the live assistant from Vapi → confirm the new prompt + toolIds + Cartesia sonic-3 Koptza voice + Deepgram nova-3 keyterms; **paste the GET-back JSON.**
4. Re-click Publish with no edits → confirm "no drift, 0 PATCH calls"; **paste the result table.**
5. Edit a KB FAQ row + Reindex → place a test call asking that FAQ → confirm the new answer is spoken; **paste the transcript + the `VoiceCall` row.**
6. Open the call log → confirm the call appears with transcript + outcome; open the vendor-callback queue + escalation review.

---

## 9. Risks / open questions

| Risk / question | Impact | Mitigation / owner item |
|---|---|---|
| **Canvas vs code topology drift** — an operator rearranges the canvas expecting it to change routing. | Confusion / false expectation. | The canvas is labelled "documents the Squad; routing is code-owned"; Publish re-asserts the required transitions from code (B3). A help tooltip states this. |
| **`vapi.py` payload shape** (exact `voice`/`transcriber`/`transferPlan` keys) may differ from the documented Vapi schema at build time. | Publish PATCH 400s. | `build_*_payload` is centralized in `dashboard/publish.py` with a single schema constant; the contract test pins the shape; provision (P0) already exercises the same payload builder → one shape, two callers. |
| **Reindex latency** — rebuilding embeddings + mirroring to Vapi Files can exceed a request timeout. | Slow/failed reindex. | Bound the work (KB is small at this scale); if it grows, move to a `manage.py` command + a "reindex queued" toast (swedish-bot has no Celery — same pattern). Document the seam. |
| **budtender admin weights endpoint may not exist yet (O-1).** | Weights don't reach budtender. | Degrade-to-local-only (§4.6); persist locally; show "sync pending"; flip on when budtender ships `/admin/ranking-weights` (a small budtender follow-up, flagged to owner). |
| **Transfer numbers unset (O-4).** | Publish emits placeholder transfer destinations. | Publish flags "transfer number not configured for <KEY>" per assistant; does not block; reads `HHT_TRANSFER_NUMBER_*` env. |
| **Per-card "Publish this" partial failure** leaving squad/assistant out of sync. | Live squad references a stale assistant shape. | `publish_squad` is always safe to re-run; the publish page has a "Publish all" that reconciles; `last_publish_hash` makes re-runs cheap. |
| **Deepgram keyterm list duplication creeping back** (export bug #7). | Token bloat. | `DEEPGRAM_KEYTERMS` is one shared constant in `voice/constants.py`; a unit test asserts the transcriber block appears exactly once in each assistant payload (ADR-011). |
| **O-8 Mt Vernon hours conflict.** | Wrong hours spoken. | The specials/hours editor gates Mt Vernon hours behind a "confirmed" flag; until set, the KB row stays "call to confirm" (per 02-DECISIONS O-8). |

---

## 10. Definition of done (P4)

- All §7 acceptance criteria pass with pasted output (`ruff check`, `ruff format --check`, the targeted `pytest`, `manage.py check`, `makemigrations --check`).
- A real edit→Publish→GET-back round-trip is demonstrated (manual §8 step 3) and a no-edit re-publish proves zero drift (G2).
- The owner can, from one dashboard, edit every assistant + the KB + the weights, review the call log / vendor queue / escalations, and publish — with the flow canvas confirmed as docs-only (B3).
- Docs updated in the SAME change: tick `14-P4-DASHBOARD-PUBLISH.md` in `00-MASTER-ROADMAP.md §7`; append any new ADR (e.g. ADR for the budtender admin-weights contract) to `02-DECISIONS.md`; note the `AgentPrompt` voice-field extension + `RankingWeights` model in `01-ARCHITECTURE.md §8`.

---

## 11. Source-file anchors (for the executor)

- swedish-bot dashboard (port): `C:\Users\vladi\OneDrive\Desktop\swedish-bot\dashboard\views.py` (`agent_config`/`agent_save`/`agent_detail`/`agent_prompt_assist`/`flow_canvas`/`_clean_graph`/`_coord`/`flow_save`/`default_flow_graph`/`kb_manager`/`faq_list`/`session_list`/`session_detail`/`conversation_replica`/`analytics_dashboard`/`_toast`/`_resolve_sort`/`_querystring`), `dashboard\urls.py`, `templates\dashboard\flow.html`.
- swedish-bot models (port + extend): `C:\Users\vladi\OneDrive\Desktop\swedish-bot\kb\models.py` (`AgentPrompt`~L226, `FlowConfig`~L255).
- swedish-bot sinks (vendor-queue re-alert): `C:\Users\vladi\OneDrive\Desktop\swedish-bot\crm\sinks.py` (`EmailSink`~L40, `dispatch`~L119).
- budtender weights/leak contract (read): `C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender\budtender\ranking.py` (`W_ANON`/`W_KNOWN`), `serializers.py` (`PUBLIC_PRODUCT_FIELDS`).
- Foundation: `C:\happytime-voice\docs\plans\{00-MASTER-ROADMAP,01-ARCHITECTURE,02-DECISIONS,03-CONVENTIONS}.md`; research: `_research-suggestion-engine.md` (weights), `_research-education-blogs.md` (KB sources for the source-manager).
- Dependencies authored by other phases: P0 (`core/services/vapi.py`, `tools/provision_vapi.py`, `voice/models.VoiceCall/VoiceTurn`, `kb/vapi_files.py`, `kb/semantic.py`, forked `AgentPrompt`/`FlowConfig`), P1 (`voice/budtender_client.py`, tool ids), P3 (`crm/models.VendorCallback`).
```