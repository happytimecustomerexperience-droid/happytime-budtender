# 12 — P2 — ESCALATION + WARM TRANSFER + STAFF EMAIL — Executable Plan

> **Status:** DONE (2026-06-22) — built on the committed P0/P1 baseline (branch `p2/escalation`).
> Escalation member seeded (`kb/seed.ESCALATION_BODY`, de-escalation + WAC 314-55-079) + provisioned
> (`voice/provision.P2_MEMBER_ROLES`); the 3 inbound …→escalation edges + the warm transferCall
> (`warm-transfer-wait-for-operator` + `{{transcript}}` summaryPlan, env-resolved per-store number)
> land; the eocr handler classifies via `voice/outcomes.py` (defective/repeated/dispute precedence)
> and writes the durable `VoiceCall` (idempotent, raw number never stored) → `crm/sinks.py` email
> sink (URGENT on immediate alert) made idempotent via the `crm.AlertDelivery` ledger. Full suite
> **165 passed**; `ruff check` + `ruff format --check` clean; `manage.py check` clean;
> `makemigrations --check` exit 0. The HMAC-fail-closed + Leak-Guard gates are green.
> **Original status below.**
>
> **Status:** EXECUTABLE SPEC (authoritative for P2). Written 2026-06-22.
> **Subsystem:** S3 (Escalation + transfer + email). **Capabilities:** C5 (problem resolution / de-escalation) + C6 (staff email alerts) — synthesis brief §3 rows E/F.
> **Fixes export weaknesses:** **#3** (escalation is a dead orphan: zero inbound edges + empty `transfer_call.destinations: []`), **#9** (Slack-only, best-effort record → silent data loss), **#10** (age/ID + defective-return path is cosmetic — no de-escalation, no WA defective exception path).
> **Read order before executing (mandatory):** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → `_research-education-blogs.md` → this file.
> **Honors ADRs (binding, never contradicted here):** ADR-002 (Squad, not Workflow), ADR-003 (idempotent code-provisioned), ADR-010 (gpt-4.1-mini assistants), ADR-011 (voice/persona set ONCE per member), ADR-012 (KB seeds WAC 314-55-079), ADR-016 (escalation: fix the orphan; real inbound transitions + warm transfer with `{{transcript}}` summary), ADR-017 (eocr → durable `VoiceCall` → email sink; Slack optional), ADR-018 (drop "peek at ID"; spoken 21+ confirm), ADR-019 (HMAC fail-closed, constant-time, per-store keys only in budtender), ADR-020 (`voice/tools/` package + registry).
> **Open items consumed as env placeholders (do NOT block):** O-4 (per-location transfer numbers + inbound number), O-9 (staff alert email routing + Slack), O-8 (Mt Vernon hours — KB content only, not P2).
>
> **One-line goal:** make the export's dead human-handoff path real and reliable — wire **real inbound transitions** into an `escalation` assistant on (≥2 explicit human requests) OR (return dispute) OR (defective-product return), run a **de-escalation script + the WA defective-product path (WAC 314-55-079)**, **warm `transferCall`** (`transferPlan.mode = "warm-transfer-wait-for-operator"` + a `summaryPlan` injecting `{{transcript}}`) to a **populated** per-location number, and on every call write a **durable `VoiceCall`/`Outcome`** from the `end-of-call-report` webhook and fire a **staff email** (`happytimeyak509@gmail.com` + per-store) — **immediately** on escalation/vendor/defective-return.

---

## 1. Goal & scope

### 1.1 In scope (this phase ships all of)

P2 delivers three tightly-coupled deliverables that together fix export #3/#9/#10:

1. **The `escalation` Squad member, reachable (fix #3).** A focused assistant whose system prompt runs a **de-escalation script** + the **WA defective-product path** (WAC 314-55-079), wired with **real inbound transitions** from `entry_router`, `budtender`, and `faq` (the export's `escalation` node had *zero inbound edges* — an orphan — and was reachable only as a forward target of an "ask twice" prose policy that never fired a real handoff). Trigger conditions: **(≥2 explicit human requests) OR (return dispute) OR (defective-product return).**
2. **Warm `transferCall` to a populated per-location number (fix #3 part 2).** The export's `transfer_call` node had `"destinations": []` (empty) → handoff was unreachable even if the orphan had been wired. P2 builds the warm-transfer tool config: `transferPlan.mode = "warm-transfer-wait-for-operator"` + a `summaryPlan` injecting `{{transcript}}` so the operator hears the context before connecting, with the destination resolved from `settings.HHT_TRANSFER_NUMBER_<KEY>` (O-4 env placeholder).
3. **`end-of-call-report` webhook → durable `VoiceCall`/`Outcome` → email sink (fix #9 + #10).** Every call writes a durable `VoiceCall` row (never silently dropped — replaces the export's best-effort Slack-only path), an `Outcome` is classified, and `crm/sinks.py` fires a staff **email** to `happytimeyak509@gmail.com` (+ per-store env) on every call, with an **immediate** alert on `escalation` / `vendor` / `defective_return` outcomes. Slack is an **optional** secondary sink (off until `SLACK_WEBHOOK_URL` is supplied, O-9).

This phase also lands the **two durable-log models** (`VoiceCall`, `VoiceTurn`, `Outcome`) that P0 scaffolds and P1/P3/P4 enrich — but P2 owns the **`end-of-call-report` write path** and the **email dispatch**, so the models' final shape is pinned here (§4.1).

### 1.2 Out of scope (other phases / EXP)

- **The vendor flow** (warm transfer → no-answer → `notify_vendor_callback`) — that is **P3** (`13-P3-VENDOR-ROUTING.md`, ADR-015). P2 ships the `escalation` member + the eocr/email path; the `vendor` member is P3. The two share `crm/sinks.py` (P2 forks it) and the eocr handler (P2 builds it; P3 adds the `vendor_callback` outcome branch). **Shared-file hazard:** both P2 and P3 register a tool module under `voice/tools/` — P2 adds **no** new tool module here (the warm transfer is a Vapi-native `transferCall` tool config, not a webhook handler), so there is no collision (§6.4).
- **The retail suggestion path** (`suggest_products`/`pair_upsell`) — **P1**.
- **The dashboard escalation-review / call-log / vendor-queue UI** — **P4** (`14-P4-dashboard-publish.md` §3.2 reads the `VoiceCall`/`Outcome` rows P2 writes).
- **Moving the eocr post-call work onto Celery** — **P5** (`15-P5-polish-brand.md` §3.5 wraps exactly P2's handler; P2 ships it **inline**, the durable write synchronous).
- **The KB seed of the WAC 314-55-079 return-policy text + store-facts** — seeded in **P0** (`kb/seed.py`, ADR-012). P2 *consumes* that KB content in the escalation prompt; it does not re-seed it.
- **Mt Vernon hours** (O-8) — KB content, owner-gated; not touched by P2.

### 1.3 Non-negotiable boundaries (binding)

- **The durable `VoiceCall` write is SYNCHRONOUS and fail-loud.** The eocr handler writes the `VoiceCall`/`Outcome` row **before** it does anything slow (Gemini summary, email, Slack). A slow/failed email never loses the record (ADR-017). The write is idempotent on the Vapi call id (`vapi_call_id` unique) — a re-delivered eocr does NOT create a duplicate row or send a duplicate email.
- **HMAC fail-closed on the webhook (ADR-019).** The eocr (and every Vapi event) is HMAC/secret-verified in `core/middleware.py` (constant-time `hmac.compare_digest`) and rejected with **401 before any P2 handler runs** on a missing/bad signature. P2 does not weaken or bypass this.
- **The transfer destination is config, never code.** `HHT_TRANSFER_NUMBER_*` are env placeholders (O-4). The transfer tool reads the env; a missing number is flagged ("transfer number not configured for `<KEY>`") and degrades to a documented placeholder **for testing** — but the escalation de-escalation script and the durable record still run. P2 ships and is testable against placeholders.
- **Numbers-Guard holds in the escalation copy.** The WAC 314-55-079 path (defective exception, "no time limit", "original packaging + legible lot ID + receipt") comes from the **KB return-policy row** (P0 seed), not invented by the model. The escalation prompt instructs the agent to ground the policy in the KB and to **transfer for the actual dispute resolution** — it never promises a refund/exchange itself.
- **PII discipline.** The caller's raw number is never persisted — only the peppered phone-hash (`PHONE_HASH_PEPPER` ≠ `SECRET_KEY`, ADR-006). The email body carries the hash + the transcript summary, never the raw number (the operator gets the raw number via the warm transfer, on the live PSTN leg, not via the email record).
- **Leak-Guard is unaffected but re-asserted.** P2 surfaces no product fields, but the eocr summary/email could echo a `suggest_products` result from earlier in the call — the email body is built from `VoiceCall` fields + the AI summary, both of which only ever held `public_product`-shaped data (budtender's allowlist serializer never sent cost/margin). A contract test asserts no `cost`/`margin` substring in the email body.

---

## 2. Dependencies (what MUST exist first)

P2 is one of the three phases that run **in parallel after P0** (roadmap §4/§6: "P1 ∥ P2 ∥ P3"). Its worktree is `wt-p2-escalation` (roadmap §6). Every item below is a hard prerequisite established by P0 (or an owner env placeholder treated as config).

| # | Dependency | Established by | What P2 consumes from it |
|---|---|---|---|
| D1 | **The Vapi webhook contract + HMAC middleware** — `POST /api/voice/vapi` routing `assistant-request` / `tool-calls` / `status-update` / `end-of-call-report` by event name; `core/middleware.py` constant-time HMAC verify, fail-closed (ADR-019). | **P0** (roadmap §7; `01-ARCHITECTURE.md` §0/§7) | P2 adds the `end_of_call_report(event)` handler behind the already-verified, HMAC-gated entry point. P2 never re-implements the signature check. |
| D2 | **`core/services/vapi.py`** REST client (GET/PATCH on `/assistant`, `/squad`, `/tool`; Bearer `VAPI_PRIVATE_KEY`; base `https://api.vapi.ai`; never `/workflow`) + **`tools/provision_vapi.py`** idempotent provisioner (writes `assistantId`/`squadId`/`toolId` back onto local rows). | **P0** (ADR-003) | P2's provisioning step (§6) ensures/patches the `escalation` assistant + its `transferCall` tool config + the squad `assistantDestinations`. GET-then-PATCH, never blind POST. |
| D3 | **The `escalation` assistant scaffolded as an `AgentPrompt` row + the Squad with 5 members** (`entry_router`/`budtender`/`faq`/`vendor`/`escalation`), voice/transcriber/model set once per member (ADR-011). | **P0** scaffolds the 5 members + the Squad shape (`01-ARCHITECTURE.md` §1.6) | P2 writes the **escalation system prompt body** (de-escalation + WAC path) into the `escalation` `AgentPrompt` row and wires the **inbound** `assistantDestinations` from entry/budtender/faq → escalation. |
| D4 | **`voice/models.py` durable-log models** — `VoiceCall`/`VoiceTurn`/`Outcome` (P0 scaffolds the skeleton; **P2 pins the final shape** in §4.1 because P2 owns the eocr write path). | **P0** scaffold; **P2** finalizes | The eocr handler writes these; the email sink reads them. |
| D5 | **`crm/models.py` `Caller` (peppered phone-hash)** + the `phone_hash(phone)` helper (`PHONE_HASH_PEPPER` ≠ `SECRET_KEY`). | **P0** ports `swedish-bot/crm/models.py` (`phone_hash`~L17, `Caller`~L34) | P2 resolves/creates a `Caller` by hash on the eocr write (PII discipline) and stamps `caller_phone_hash` on the email + record. |
| D6 | **`core/services/gemini.py`** (lifted verbatim) for the **post-call AI summary** of the transcript. | **P0** (ADR-001; `01-ARCHITECTURE.md` §8) | `voice/summarize.py` calls Gemini to produce `VoiceCall.ai_summary` from the eocr transcript/messages (server-side LLM = Gemini per ADR-010, even though the assistants run gpt-4.1-mini). |
| D7 | **KB return-policy row (WAC 314-55-079) + store-facts (3 stores, hours, phones)** seeded in `kb/`. | **P0** (`kb/seed.py`, ADR-012) | The escalation prompt grounds the defective-exception language in this row (Numbers-Guard); the de-escalation script localizes to the caller's store. |
| D8 | **Per-location transfer numbers + the inbound number** — env placeholders. | **owner (O-4)** — `.env.example` keys `HHT_TRANSFER_NUMBER_{YAKIMA,MTVERNON,PULLMAN}`, `VAPI_PHONE_NUMBER_ID`, `HHT_DEFAULT_STORE` | The warm-transfer tool config reads `settings.HHT_TRANSFER_NUMBER_<KEY>`; a placeholder is used in test. **Not a blocker** (graceful-degradation rule below). |
| D9 | **Email transport** — SMTP env + `STAFF_ALERT_EMAIL` (default `happytimeyak509@gmail.com`) + optional per-store + optional Slack. | **owner (O-9)** — `.env.example` §3.7/§3.8 | `crm/sinks.py` EmailSink reads these; Slack behind `SLACK_ALERTS_ENABLED=0` until a webhook is supplied. |

**Graceful-degradation rule (so P2 is not hard-blocked by env placeholders):** every owner-supplied env placeholder (O-4 transfer numbers / inbound number, O-9 email + Slack) is **read at use, never required at import**. The escalation assistant provisions and the eocr handler writes the durable record + classifies the outcome even when a transfer number is a placeholder; the email sink degrades to `skipped` (logged "disabled or not configured") when no recipient is configured — never crashing the webhook. The durable `VoiceCall` write **never** depends on any placeholder.

---

## 3. File-by-file task list

Format: **exact path → responsibility → key functions/shape → port-from (with path)**. New files marked **★ NEW**; edits to P0 files marked **EDIT**; ported files cite the swedish-bot original.

### 3.1 `voice/` — the eocr write path + summary + outcome classification

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `voice/webhooks.py` **EDIT** | Add the `end_of_call_report(event)` handler behind the HMAC-gated `POST /api/voice/vapi` dispatcher. Synchronous durable write first; then summary → email/Slack. Idempotent on `vapi_call_id`. | `end_of_call_report(event: dict) -> JsonResponse`; `_extract_eocr(event) -> EocrFields`; calls `outcomes.classify_outcome(...)`, `summarize.summarize_call(...)`, `crm.sinks.dispatch(...)`. Returns 200 fast after the sync write. | `swedish-bot/chat/views.py` (the channel-adapter endpoint shape: `@csrf_exempt` + middleware-gated; "exactly the shape a Vapi/Twilio webhook handler takes" — roadmap §2). The event dispatch-by-name lands in P0; P2 adds this one branch. |
| `voice/outcomes.py` **★ NEW** | Deterministic outcome classification from the eocr payload + the call's transitions/transcript (no LLM — code owns the label; the model only fills slots). | `classify_outcome(eocr, transcript, transitions) -> Outcome`; constants `OUTCOME_CHOICES = {"faq_answered","suggested","escalation","vendor_callback","defective_return","abandoned","other"}`; `escalation_reason_of(transcript) -> {"defective_return","repeated_request","dispute"} | None`; `is_immediate_alert(outcome, reason) -> bool` (True for escalation/vendor/defective_return). | net-new; mirrors `swedish-bot/crm/leads.py` deterministic-enrichment discipline ("deterministic CRM enrichment, never LLM" — synthesis brief §2). |
| `voice/summarize.py` **★ NEW** (P0 may scaffold; P2 fills) | Post-call AI summary of the transcript via Gemini. Server-side LLM only. | `summarize_call(transcript: str, slots: dict) -> str` (≤ ~600 chars; deterministic-ish prompt: location, category, SKUs mentioned, outcome, human-requested flag — mirrors the export's `slack_summary` intent). Fail-soft: returns a deterministic fallback string on Gemini error (never raises into the webhook). | `swedish-bot/core/services/gemini.py` (`generate`, `MODELS["flash"]`, token accounting); the export's `slack_summary` node intent (the Downloads JSON `slack_summary` prompt — location/category/SKUs/outcome/human-requested). |
| `voice/models.py` **EDIT/PIN** | Final shape of `VoiceCall`/`VoiceTurn`/`Outcome` (P0 scaffolds; P2 pins the fields the eocr write + email need). | See §4.1. Key: `VoiceCall.vapi_call_id` UNIQUE (idempotency), `outcome` FK/choice, `escalation_reason`, `caller_phone_hash`, `ai_summary`, `transferred` + `transfer_disposition`, `store`. | `swedish-bot/crm/models.py` (`Session`~L86 — `ai_summary`~L119; `ServiceRequest`~L138 — `escalation_reason`~L138; `Caller`/`phone_hash`~L17) — adapt the Session/ServiceRequest durable-record + escalation-reason idiom to a voice call. |
| `voice/constants.py` **EDIT** | Add the de-escalation / transfer constants used by the prompt builder + provisioner: `WARM_TRANSFER_MODE = "warm-transfer-wait-for-operator"`, `TRANSFER_NUMBER_KEYS = ("YAKIMA","MTVERNON","PULLMAN")`, the `summaryPlan` system message template. | module-level constants; no logic. | net-new (alongside P0's `DEEPGRAM_KEYTERMS`). |

### 3.2 `crm/` — the email/Slack sink (port + adapt)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `crm/sinks.py` **★ PORT** | The pluggable sink set for voice: `DBSink` (always — the `VoiceCall` row IS the durable record), `EmailSink` (staff alert), `SlackSink` (optional secondary, off until configured). `dispatch(voice_call)` fires every sink independently, idempotent per `(voice_call, sink)`, never raises. | `class VoiceSink` (`name`, `enabled()`, `deliver(voice_call)`); `DBSink`/`EmailSink`/`SlackSink`; `SINKS: list[VoiceSink]`; `dispatch(voice_call) -> dict[str,str]`. EmailSink builds the body from `VoiceCall` fields + `ai_summary`; recipient = `_recipients_for(voice_call.store)` (shared `STAFF_ALERT_EMAIL` + per-store override). | `swedish-bot/crm/sinks.py` **verbatim structure** (`LeadSink`~L21, `DBSink`~L31, `EmailSink`~L40 incl. `enabled()` gating on the recipient + `send_mail(fail_silently=False)`, `WebhookSink`~L66, `dispatch`~L119 with idempotent `get_or_create` per `(request, sink)` + `delivery.status=="success"` short-circuit + never-raises). **Swap** the lead-domain body for the voice-call body; **swap** `LeadDelivery` for `AlertDelivery` (§4.2); **add** `SlackSink` (env-gated) in place of `WordPressOffertSink`. |
| `crm/models.py` **EDIT** | Add `AlertDelivery` (idempotency ledger, one row per `(voice_call, sink)`) — mirrors swedish-bot `LeadDelivery`. | `AlertDelivery(voice_call FK, sink CharField, status, attempts, last_error, created/updated)`; `unique_together = ("voice_call","sink")`. | `swedish-bot/crm/models.py` `LeadDelivery` (the `(service_request, sink)` idempotency row the `dispatch` loop `get_or_create`s). |

### 3.3 KB / prompt — the escalation assistant copy (de-escalation + WAC path)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `kb/seed.py` **EDIT (escalation prompt body only)** | Author the **`escalation` `AgentPrompt.body`**: the de-escalation script + the WA defective-product path. The prompt instructs: acknowledge + de-escalate; localize to the caller's store; ground the WAC 314-55-079 exception in the KB return-policy row (Numbers-Guard — never invent terms); confirm intent; warm-transfer to a human (don't resolve the dispute itself). | One `AgentPrompt(role="escalation", body=…)` seed. The body references (does not duplicate) the KB return-policy + store-facts rows. Includes the spoken-21+ posture (ADR-018 — never "peek at your ID"). | `_research-education-blogs.md` §8 (house style: warm, conservative, cite the source) + the WAC 314-55-079 text from the P0-seeded return-policy row (synthesis brief §2 "Return policy"); the export's `escalation` node prose (Downloads JSON `escalation` node, line 3537) for the de-escalation tone (acknowledge warmly; "ask twice" gate). |
| `kb/seed.py` **EDIT (transition-trigger few-shots)** | Add 2–3 few-shot examples to the `entry_router`, `budtender`, and `faq` `AgentPrompt` bodies so they emit the **handoff-to-escalation** signal on the trigger conditions (≥2 human requests / return dispute / defective product) — the inbound-transition copy side of fixing the orphan. | Few-shot lines in each of the 3 source members' bodies (the structured "handoff: escalation, reason: <…>" the Squad reads). The runtime topology is provisioned in §6 (code); these few-shots make the model actually *take* the edge. | the export `globalPrompt` "Escalation and transfer" clause (Downloads JSON line 3960, rule 6) — but corrected: the export's policy was prose with no real edge; here it maps to a real `assistantDestination`. |

> **Why the prompt edits are KB/seed edits, not code:** per ADR-014 / P4, the assistant bodies are `AgentPrompt` rows (read-fresh-every-turn, dashboard-editable). P0 seeds them; P2 authors the *escalation-specific* copy into the seed. After P4, the owner edits them in the dashboard and re-publishes; P2 does not hardcode prompts in Python.

### 3.4 Provisioning — wire the inbound edges + the warm transfer (code-owned topology)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `tools/provision_vapi.py` **EDIT** | Ensure/patch: (a) the `escalation` assistant carries the **`transferCall` tool config** (warm mode + `summaryPlan` `{{transcript}}` + destination from env); (b) the **Squad `assistantDestinations`** include `entry_router→escalation`, `budtender→escalation`, `faq→escalation` (the inbound edges that fix the orphan) with their **trigger descriptions**. GET-then-PATCH, idempotent. | `build_escalation_transfer_tool(transfer_key) -> dict` (§4.3); `ensure_escalation_destinations(squad_payload) -> squad_payload` (re-asserts the inbound edges from code — `01-ARCHITECTURE.md` §1.6). Reuses P0's `ensure_assistant`/`patch_squad`. | net-new builder; the squad-shape source of truth is `01-ARCHITECTURE.md` §1.6; the payload shape mirrors `14-P4-dashboard-publish.md` §4.3/§4.4 (`build_assistant_payload`/`build_squad_payload`) so provision and publish emit **one shape, two callers**. |

### 3.5 `config/` — env + sink wiring

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `config/settings.py` **EDIT** | Read the P2 env: `HHT_TRANSFER_NUMBER_{YAKIMA,MTVERNON,PULLMAN}`, `HHT_DEFAULT_STORE`, `STAFF_ALERT_EMAIL` (+ per-store), `LEAD_EMAIL_FROM`, `SLACK_WEBHOOK_URL`, `SLACK_ALERTS_ENABLED`. Email backend already configured by P0; P2 only adds the alert-routing vars. Prod-fail-closed unchanged. | env reads via `django-environ`; no logic. | `swedish-bot/config/settings.py` (env-read idiom + the `LEAD_EMAIL_FROM`/`LEAD_EMAIL_TO` pattern → renamed `STAFF_ALERT_EMAIL`). |
| `.env.example` **EDIT** | Document every P2 var with a placeholder (already catalogued in `03-CONVENTIONS.md` §3.5/§3.7/§3.8). | — | `03-CONVENTIONS.md` §3 catalog. |

### 3.6 Tests (`tests/`)

| Path | Responsibility | Port from |
|---|---|---|
| `tests/test_outcomes.py` **★ NEW** | `classify_outcome` over a table of eocr fixtures (faq / suggested / 2×human-request / dispute / defective / abandoned); `escalation_reason_of`; `is_immediate_alert`. Deterministic, no network. | net-new (table-driven, expected values hand-authored). |
| `tests/test_eocr_write.py` **★ NEW** | The synchronous durable write: a `VoiceCall` row is created with the right outcome/summary/hash; **idempotent** on `vapi_call_id` (re-deliver → no dup row, no dup email); the write happens even when the email sink raises. | net-new; reuse swedish-bot `dispatch` idempotency idiom. |
| `tests/test_sinks_email.py` **★ NEW** | EmailSink body contains the summary + store + hash + reason; recipient resolution (shared vs per-store); `enabled()` False when no recipient → `skipped`; **no `cost`/`margin` substring** (Leak-Guard); `dispatch` never raises and records per-sink status. | `swedish-bot/crm/sinks.py` test idiom; the Leak-Guard contract test (`03-CONVENTIONS.md` §5). |
| `tests/test_escalation_payload.py` **★ NEW** | `build_escalation_transfer_tool` emits `transferPlan.mode == "warm-transfer-wait-for-operator"` + a `summaryPlan` whose message contains `{{transcript}}` + a destination from the env key; placeholder-degrade when the env is unset (flag, don't crash). The squad payload contains the 3 inbound edges into escalation (B-orphan-fix). | mirrors `14-P4` §8 `build_*_payload` shape tests. |
| `tests/test_hmac_fail_closed_p2.py` **★ NEW (mandatory gate)** | A missing/bad Vapi signature → 401 **before** `end_of_call_report` runs (no `VoiceCall` written, no email sent). Valid signature passes. | the P0 HMAC test (re-asserted on the eocr branch). |

---

## 4. Data contracts / JSON schemas

### 4.1 `voice/models.py` — `VoiceCall` / `VoiceTurn` / `Outcome` (P2-pinned final shape)

P0 scaffolds these; P2 pins the fields the eocr write + email need. (P4 reads them read-only; P5 adds `VoiceTurn.latency_ms` — that field is P5's, noted here so the migration order is known.)

```python
class VoiceCall(models.Model):
    # ── identity / idempotency ──
    vapi_call_id   = CharField(max_length=128, unique=True, db_index=True)   # the Vapi call id — idempotency key
    store          = CharField(max_length=24, blank=True)                    # "yakima"/"mount-vernon"/"pullman" (HHT_DEFAULT_STORE fallback)
    caller_phone_hash = CharField(max_length=64, blank=True, db_index=True)  # peppered SHA-256 (ADR-006); NEVER the raw number
    # ── lifecycle ──
    started_at     = DateTimeField(null=True, blank=True)
    ended_at       = DateTimeField(null=True, blank=True)
    duration_sec   = IntegerField(null=True, blank=True)
    ended_reason   = CharField(max_length=64, blank=True)                    # Vapi endedReason (passthrough)
    # ── outcome / escalation ──
    outcome        = CharField(max_length=24, choices=OUTCOME_CHOICES, default="other", db_index=True)
    escalation_reason = CharField(max_length=32, blank=True)                 # defective_return | repeated_request | dispute | ""
    human_requested_count = IntegerField(default=0)                          # how many times the caller asked for a person
    # ── transfer disposition ──
    transferred    = BooleanField(default=False)
    transfer_disposition = CharField(max_length=24, blank=True)              # connected | no_answer | not_attempted
    transfer_number_key  = CharField(max_length=16, blank=True)             # YAKIMA/MTVERNON/PULLMAN actually targeted
    # ── content ──
    ai_summary     = TextField(blank=True)                                   # Gemini post-call summary (server-side LLM)
    transcript     = TextField(blank=True)                                   # full eocr transcript (for replay)
    # ── bookkeeping ──
    raw_eocr       = JSONField(default=dict)                                  # the raw end-of-call-report (debug; redacted of secrets)
    created_at     = DateTimeField(auto_now_add=True)
    updated_at     = DateTimeField(auto_now=True)

class VoiceTurn(models.Model):
    call    = ForeignKey(VoiceCall, related_name="turns", on_delete=CASCADE)
    role    = CharField(max_length=16)        # "user" | "assistant" | "system" | "tool"
    text    = TextField(blank=True)
    at      = DateTimeField(null=True, blank=True)
    # latency_ms = IntegerField(null=True)    # ← P5 adds this (analytics p95). Noted, NOT created here.

OUTCOME_CHOICES = [
    ("faq_answered","FAQ answered"), ("suggested","Suggestion made"),
    ("escalation","Escalation / human transfer"), ("vendor_callback","Vendor callback"),
    ("defective_return","Defective return"), ("abandoned","Abandoned"), ("other","Other"),
]
```

> `Outcome` is a value, not a separate model in v1 — it lives as `VoiceCall.outcome` + `escalation_reason` (mirrors swedish-bot's `Session`/`ServiceRequest` where the outcome is a field, not a table). If a richer outcome ledger is ever needed it becomes a model (EXP); the choices tuple is the canonical enum P4's dashboard + P5's analytics read.

### 4.2 `crm/models.py` — `AlertDelivery` (idempotency ledger)

```python
class AlertDelivery(models.Model):      # one row per (voice_call, sink) — mirrors swedish-bot LeadDelivery
    voice_call = ForeignKey("voice.VoiceCall", related_name="alert_deliveries", on_delete=CASCADE)
    sink       = CharField(max_length=24)        # "db" | "email" | "slack"
    status     = CharField(max_length=16, default="pending")   # pending|success|failed|skipped
    attempts   = IntegerField(default=0)
    last_error = CharField(max_length=500, blank=True)
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
    class Meta:
        unique_together = ("voice_call", "sink")     # the idempotency guarantee
```

### 4.3 `end-of-call-report` inbound payload (Vapi → `/api/voice/vapi`) — what P2 reads

The fields P2 extracts (`_extract_eocr`). Vapi's eocr is verbose; P2 reads only what it needs and stores the rest in `raw_eocr` (secrets redacted):

```json
{
  "message": {
    "type": "end-of-call-report",
    "call": { "id": "call_abc123", "phoneNumber": { "number": "+1509…" }, "assistantId": "asst_…" },
    "endedReason": "customer-ended-call",
    "startedAt": "2026-06-22T19:00:00Z",
    "endedAt":   "2026-06-22T19:04:12Z",
    "durationSeconds": 252,
    "transcript": "AI: Happy Time, this is Koptza… \nUser: my cart is defective …",
    "messages": [ { "role": "user", "message": "…" }, { "role": "assistant", "message": "…" } ],
    "summary": "Caller reported a defective vape cart; transferred to Yakima.",
    "analysis": { "successEvaluation": "…", "structuredData": { "store": "yakima", "human_requested": 2, "category": "cartridge" } },
    "destination": { "type": "number", "number": "+1509…" }     // present if a transferCall fired
  }
}
```

- `caller_phone_hash = phone_hash(message.call.phoneNumber.number)` — the raw number is hashed and discarded (ADR-006).
- `store` = `analysis.structuredData.store` if present, else `HHT_DEFAULT_STORE`.
- `human_requested_count` = `analysis.structuredData.human_requested` (the entry/escalation members are prompted to count + emit this) — feeds the `repeated_request` outcome reason.
- `transferred` = `message.destination` present; `transfer_disposition` derived from `endedReason` (a transfer-completed reason → `connected`; an assistant-ended-without-transfer → `not_attempted`; a transfer-failed/no-answer reason → `no_answer`).

### 4.4 Outcome classification rules (`voice/outcomes.classify_outcome`)

Deterministic precedence (highest-severity wins — an immediate-alert outcome is never masked by a softer one):

| Precedence | Condition (from eocr fields/transcript/transitions) | `outcome` | `escalation_reason` | immediate alert? |
|---|---|---|---|---|
| 1 | a `defective`/`broken`/`malfunction` return signal OR a transition to `escalation` with `structuredData.reason == "defective_return"` | `defective_return` | `defective_return` | **yes** |
| 2 | `human_requested_count >= 2` OR a transition to `escalation` with `reason == "repeated_request"` | `escalation` | `repeated_request` | **yes** |
| 3 | a return-dispute signal (caller contests a sale, no defect) OR `escalation` with `reason == "dispute"` | `escalation` | `dispute` | **yes** |
| 4 | a transition to `vendor` ended in a callback log (P3 owns the write; P2 recognizes the label) | `vendor_callback` | "" | **yes** |
| 5 | a `suggest_products` tool fired and the caller engaged | `suggested` | "" | no |
| 6 | only `faq_lookup` fired / informational | `faq_answered` | "" | no |
| 7 | call < ~15s with no slot progress | `abandoned` | "" | no |
| 8 | none of the above | `other` | "" | no |

`is_immediate_alert(outcome, reason)` → `True` for outcomes 1–4 (the email fires with an `[URGENT]` subject prefix and is the *only* dispatch that may also hit Slack when enabled). Non-urgent outcomes still get the per-call digest email (ADR-017: "an email per call") — they are just not `[URGENT]`.

### 4.5 Staff alert email — body contract (`crm/sinks.EmailSink`)

```
Subject:  [Happy Time voice] {STORE} — {OUTCOME}{ " — URGENT" if immediate }
          e.g. "[Happy Time voice] Yakima — defective_return — URGENT"

To:       _recipients_for(store)  =  STAFF_ALERT_EMAIL  (+ STAFF_ALERT_EMAIL_<STORE> if set)
From:     LEAD_EMAIL_FROM  (e.g. bot@happytimeweed.com)

Body:
  New voice call — {STORE}.
  Outcome: {outcome}{ "  (reason: " + escalation_reason + ")" if escalation_reason }
  Caller (hashed): {caller_phone_hash[:12]}…        # NEVER the raw number
  Duration: {duration_sec}s   Ended: {ended_reason}
  Human requested: {human_requested_count}×
  Transfer: {transfer_disposition or "—"}  ({transfer_number_key or "—"})

  Summary:
  {ai_summary}

  Call id: {vapi_call_id}   ·   logged {created_at}
```

- **Leak-safe:** the body is built ONLY from `VoiceCall` fields + `ai_summary`. No product `cost`/`margin` field exists on any of them (a contract test asserts no `cost`/`margin` substring — `03-CONVENTIONS.md` §5).
- **PII:** the hash, not the number. The operator reaches the live caller via the warm-transfer PSTN leg, not via this record.
- `send_mail(..., fail_silently=False)` — the EmailSink fails loud (recorded as `failed` in `AlertDelivery`, never silently swallowed), exactly like swedish-bot's `EmailSink`.

### 4.6 Slack secondary sink (optional, O-9)

`SlackSink.enabled()` → `SLACK_ALERTS_ENABLED == "1" and bool(SLACK_WEBHOOK_URL)` (mirrors swedish-bot `WebhookSink.enabled`). On an immediate-alert outcome it POSTs a compact JSON block (`{store, outcome, reason, summary, call_id}`) to `SLACK_WEBHOOK_URL`. **Off by default** (`SLACK_ALERTS_ENABLED=0`). Never a primary record — the durable `VoiceCall` + email are authoritative (ADR-017).

---

## 5. The escalation conversational design (de-escalation + WAC path)

This is the **copy** that fixes export #10 (the cosmetic age/ID + missing defective path). It lives in the `escalation` `AgentPrompt.body` (§3.3), grounded in the P0-seeded KB.

### 5.1 De-escalation script (the spoken behavior)

1. **Acknowledge + validate, immediately.** "I'm really sorry that happened — let me help you get this sorted." (Warm, family tone — `_research-education-blogs.md` §8; the export's `escalation` node tone, line 3537: "I totally get it.")
2. **Localize to the caller's store.** Use the KB store-facts row for the caller's `store` (Yakima / Mt Vernon / Pullman) — name + that the team there will handle it.
3. **For a defective product — speak the WA path (grounded, never invented):** read the WAC 314-55-079 exception from the KB return-policy row: a **defective product (e.g. a malfunctioning vape cart) can be exchanged**, there is **no time limit**, and the customer must bring the **original packaging with a legible lot/batch ID + the receipt**. Then: "Let me get a manager on so they can take care of the exchange." (The agent **does not** promise/process the exchange itself — it transfers for resolution. Numbers-Guard: the terms come from the KB row.)
4. **Confirm intent before transferring (the export's "ask twice" gate, made real).** On a *human request*, the agent first tries to help; **only after the caller has clearly asked for a person ≥2 times** (or it's a defective/dispute case) does it take the real `transferCall` edge. The count is emitted as `structuredData.human_requested` (feeds §4.4).
5. **Warm transfer.** State that it's connecting them; the `transferCall` runs warm (operator hears the `{{transcript}}` summary first).
6. **21+ posture (ADR-018):** if age comes up, a spoken "are you 21 or older?" — never "let me peek at your ID" (a phone agent can't see ID). The escalation member inherits the entry posture; it does not re-gate unless relevant.

### 5.2 Trigger conditions (the inbound edges — fixing the orphan)

The **runtime topology** is provisioned in §6 (code); the **prompt few-shots** (§3.3) make the model take the edge. Three source members route INTO escalation:

- **`entry_router → escalation`** — opener is a dispute/defective/"I want a human" (e.g. "my cart's broken and I want my money back"). Description on the edge: `">=2 human / dispute / defective"`.
- **`budtender → escalation`** — mid-flow, the caller asks for a person ≥2× OR raises a defect/dispute while shopping. Description: `"human request mid-flow"`.
- **`faq → escalation`** — a returns question turns into a dispute/defective claim the FAQ can't resolve. Description: `"return dispute / defective"`.

These three edges are the literal fix for export #3 (the `escalation` node had **zero** inbound edges — its only edge in the export was the outbound `escalation → transfer_call`, Downloads JSON lines 3950–3951).

---

## 6. Vapi deploy steps (what this phase actually provisions)

P2 provisions via the documented CRUD only (ADR-002/003) — never `/workflow`. The provisioning is idempotent (GET-then-PATCH); a re-run yields zero drift.

### 6.1 Ensure the `escalation` assistant carries the warm-transfer tool

`PATCH /assistant/{escalation_id}` with the body from `build_assistant_payload(escalation_prompt)` (the P4/P0 shared builder, `14-P4` §4.3), where the `model.tools` array includes the **`transferCall`** config from `build_escalation_transfer_tool`:

```json
{
  "type": "transferCall",
  "destinations": [
    {
      "type": "number",
      "number": "${HHT_TRANSFER_NUMBER_YAKIMA}",
      "message": "Connecting you to the team now — one moment.",
      "transferPlan": {
        "mode": "warm-transfer-wait-for-operator",
        "summaryPlan": {
          "enabled": true,
          "messages": [
            { "role": "system",
              "content": "You are briefing a Happy Time store operator before connecting a caller. Summarize the situation in 2-3 sentences for the operator: {{transcript}}" }
          ]
        }
      }
    }
  ]
}
```

- The destination `number` is resolved at publish/provision time from `settings.HHT_TRANSFER_NUMBER_<KEY>` where `<KEY>` comes from `escalation`'s `transfer_number_key` (the store the caller is associated with; default `HHT_DEFAULT_STORE`). If a number env is unset (O-4), provision substitutes a documented placeholder and emits a warning `"transfer number not configured for <KEY>"` — it does **not** block the rest (graceful-degradation rule §2). **This is the explicit fix for the export's `"destinations": []`.**
- `mode = "warm-transfer-wait-for-operator"` + the `summaryPlan` injecting `{{transcript}}` is the warm-transfer-with-context requirement (ADR-016).
- Voice/transcriber/model are set **once at the member level** (ADR-011) by the shared `build_assistant_payload` — the transfer tool adds no per-node config.

### 6.2 Wire the inbound `assistantDestinations` (the orphan fix)

`PATCH /squad/{VAPI_SQUAD_ID}` with `build_squad_payload()` (the P4/P0 shared builder, `14-P4` §4.4). The destinations are **re-asserted from code** (`01-ARCHITECTURE.md` §1.6) so the canvas can never delete a required transition:

```json
{
  "members": [
    { "assistantId": "<entry_router id>", "assistantDestinations": [
        { "type":"assistant","assistantName":"budtender",  "description":"retail intent" },
        { "type":"assistant","assistantName":"faq",        "description":"info intent" },
        { "type":"assistant","assistantName":"vendor",     "description":"vendor/wholesale/manifest" },
        { "type":"assistant","assistantName":"escalation", "description":">=2 human / dispute / defective" }
    ]},
    { "assistantId": "<budtender id>", "assistantDestinations": [
        { "type":"assistant","assistantName":"escalation", "description":"human request mid-flow" } ]},
    { "assistantId": "<faq id>", "assistantDestinations": [
        { "type":"assistant","assistantName":"budtender" },
        { "type":"assistant","assistantName":"escalation", "description":"return dispute / defective" } ]},
    { "assistantId": "<vendor id>", "assistantDestinations": [
        { "type":"assistant","assistantName":"escalation" } ]},
    { "assistantId": "<escalation id>", "assistantDestinations": [] }   // terminal; warm transferCall out
  ]
}
```

The three `…→escalation` edges (from entry/budtender/faq) are the inbound transitions the export lacked. `escalation` itself is **terminal** in the squad graph (no further assistant handoff) — it exits via the warm `transferCall` tool, not via a member destination.

### 6.3 Register the `end-of-call-report` server URL (already P0)

The eocr is delivered to the Squad/assistant `server.url = ${PUBLIC_BASE_URL}/api/voice/vapi` with `server.secret = ${VAPI_WEBHOOK_SECRET}` — set once on the assistants by P0's `build_assistant_payload` (`14-P4` §4.3). P2 adds no new server URL; it adds the **handler** behind the existing one (D1). Confirm at provision time that every member's `server.secret` is present (fail-closed: a member with no webhook secret is a provisioning error).

### 6.4 No new tool webhook module (no `voice/tools/` collision)

The warm transfer is a **Vapi-native `transferCall`** (Vapi places the PSTN call) — it is a *tool config on the assistant*, not a `POST /api/voice/vapi` webhook handler. So P2 adds **no** module under `voice/tools/` and cannot collide with P1 (`tools/suggest.py`) or P3 (`tools/vendor.py`) in the parallel worktrees (roadmap §6 shared-file hazard is avoided by construction). The only P2 webhook addition is the `end_of_call_report` *event branch* in `voice/webhooks.py` — a different file from `voice/tools/*`, and a different event (`end-of-call-report`, not `tool-calls`).

### 6.5 Idempotency / zero-drift

Running `python tools/provision_vapi.py` twice after the P2 edits produces **zero new Vapi objects** — only PATCHes, and a second no-edit run produces zero PATCHes (the `last_publish_hash` short-circuit, `14-P4` §5). This is an acceptance criterion (G2-analog, §7).

---

## 7. Acceptance criteria (testable, concrete)

Each is a concrete pass/fail assertion. They restate roadmap §5 "P2 — Escalation + transfer + email" with exact checks.

**A. Escalation reachable (fix #3, orphan + inbound edges)**
- A1. After provision, the squad payload (GET-back) contains all three inbound edges into `escalation`: `entry_router→escalation`, `budtender→escalation`, `faq→escalation`, each with its trigger `description`. (Assert against a mocked/GET-back squad — the export had **zero** such edges.)
- A2. The `escalation` member's assistant payload contains a `transferCall` tool with a **non-empty** `destinations` array (the export had `[]`). The destination `number` resolves from `HHT_TRANSFER_NUMBER_<KEY>`; with the env unset it is a documented placeholder + a `"transfer number not configured for <KEY>"` warning (not a crash).

**B. Warm transfer with summary (ADR-016)**
- B1. `build_escalation_transfer_tool` emits `transferPlan.mode == "warm-transfer-wait-for-operator"` and a `summaryPlan` whose system message string **contains the literal `{{transcript}}`** (the operator gets the context).
- B2. The transfer config sets voice/transcriber/model **nowhere** (it's member-level, ADR-011) — a unit test asserts the transfer tool block introduces no per-node voice/model keys.

**C. Trigger conditions (the de-escalation policy)**
- C1. `classify_outcome` on an eocr with `human_requested_count >= 2` → `outcome="escalation"`, `escalation_reason="repeated_request"`, `is_immediate_alert == True`.
- C2. `classify_outcome` on a defective-product eocr → `outcome="defective_return"`, `escalation_reason="defective_return"`, immediate. A return-dispute (no defect) → `outcome="escalation"`, `reason="dispute"`, immediate.
- C3. A single human request (count == 1) does **not** classify as escalation (the "ask twice" gate) → stays `faq_answered`/`suggested`/`other` as appropriate. (Guards against premature transfer.)

**D. Durable record (fix #9)**
- D1. The `end_of_call_report` handler writes a `VoiceCall` row with `vapi_call_id`, `store`, `caller_phone_hash` (hashed — **the raw number appears nowhere** in the row), `outcome`, `escalation_reason`, `ai_summary`, `transferred`/`transfer_disposition`. The write is **synchronous** and happens **before** the email dispatch.
- D2. **Idempotency:** re-delivering the same eocr (same `vapi_call_id`) does **not** create a second `VoiceCall` row and does **not** send a second email (assert one `VoiceCall`, one `AlertDelivery(email).status=="success"`).
- D3. **The record survives an email failure:** with the SMTP transport raising, the `VoiceCall` row is still written and the `AlertDelivery(email)` is recorded `failed` (logged, not fatal) — the webhook still returns 200. (ADR-017 durable-write-never-lost.)

**E. Staff email (fix #10 alerting + ADR-017)**
- E1. Every call fires a per-call digest email to `_recipients_for(store)` (shared `STAFF_ALERT_EMAIL` + any per-store override). An immediate-alert outcome (escalation/vendor/defective) gets the `— URGENT` subject prefix.
- E2. The email body contains the store, outcome, `escalation_reason` (when set), the hashed caller, the summary, and the call id — and **no raw phone number** and **no `cost`/`margin` substring** (Leak-Guard + PII).
- E3. With no recipient configured, the EmailSink is `skipped` (logged "disabled or not configured"), the `VoiceCall` is still written, and the webhook returns 200 (degrade-safe, O-9).
- E4. Slack is **off** by default (`SLACK_ALERTS_ENABLED=0`); with it enabled + a webhook URL, an immediate-alert outcome also POSTs the compact block; a 4xx from Slack is recorded `failed` and never blocks the email or the record.

**F. Security (ADR-019)**
- F1. A missing/bad Vapi signature on the eocr → **401 before** `end_of_call_report` runs: no `VoiceCall` written, no email sent (assert zero rows + zero emails). Valid signature → handler runs. (Mandatory gate — `03-CONVENTIONS.md` §5.)
- F2. Secrets (`VAPI_PRIVATE_KEY`, `VAPI_WEBHOOK_SECRET`, `HHT_BACKEND_TOKEN`, SMTP password, `SLACK_WEBHOOK_URL`) never appear in `raw_eocr`, the email body, a `PublishResult`, or any log line (payload logging redacts `server.secret` + the SMTP creds).

**G. Idempotent provisioning (ADR-003)**
- G1. `python tools/provision_vapi.py` after the P2 edits creates **zero** new Vapi objects (only PATCHes the escalation assistant + the squad). A second no-edit run issues **zero** PATCHes (zero drift). (Assert mock call counts.)

---

## 8. Test plan

Mirrors the four planes in `03-CONVENTIONS.md` §5. P2 touches the **webhook** and produces **serialized output (the email)** → the **HMAC-fail-closed** and **Leak-Guard** tests are mandatory gates.

### 8.1 Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network)
- `tests/test_outcomes.py` — `classify_outcome` over a hand-authored table (faq / suggested / 1×human / 2×human / dispute / defective / abandoned / other); precedence (defective beats repeated beats dispute); `escalation_reason_of`; `is_immediate_alert` (C1/C2/C3). Expected values hand-authored, not generated by the code under test.
- `tests/test_escalation_payload.py` — `build_escalation_transfer_tool` shape (B1: warm mode + `{{transcript}}` summaryPlan; A2: non-empty destinations; placeholder-degrade when env unset) + `build_squad_payload` inbound-edge assertion (A1) + no per-node voice/model dup (B2).
- `tests/test_sinks_email.py` (no-network: `send_mail` over `locmem` backend) — body contents (E2), recipient resolution shared/per-store (E1), `enabled()` False → `skipped` (E3), `dispatch` per-sink status + never-raises, idempotent `(voice_call, sink)` short-circuit (D2). **Leak-Guard assertion: no `cost`/`margin` substring in any email body.**
- `tests/test_eocr_write.py` (`@pytest.mark.django_db`) — synchronous write order (record before email, D1); idempotency on `vapi_call_id` (D2); record-survives-email-failure (D3, with the EmailSink monkeypatched to raise); `caller_phone_hash` is the hash, raw number absent (PII).

### 8.2 Contract (`pytest -m integration`, Vapi mocked, SMTP `locmem`/raising)
- `tests/test_hmac_fail_closed_p2.py` (**mandatory gate**) — bad/missing signature → 401 before the handler; no `VoiceCall`, no email (F1). Valid signature passes.
- `tests/test_eocr_to_email.py` — full path: a fixture eocr (defective-return) → POST `/api/voice/vapi` with a valid HMAC → one `VoiceCall(outcome="defective_return")`, one `[…— URGENT]` email queued (E1), `transfer_disposition` derived correctly (§4.3). Re-POST the same eocr → no dup (D2).
- `tests/test_provision_escalation.py` — against a mocked `core/services/vapi.py`: first provision PATCHes the escalation assistant (non-empty `destinations`) + the squad (3 inbound edges); a second no-edit run issues **zero** PATCHes (G1). Asserts no `/workflow` call ever.
- `tests/test_secrets_redacted.py` — `raw_eocr` + email + logs contain none of the secret values (F2).

### 8.3 Provisioning (`python tools/provision_vapi.py --dry-run` then live against a sandbox key)
- Dry-run diff shows PATCH-only for the escalation assistant + squad (no POST). Live run writes the ids back; a re-run is drift-free (G1). Paste the dry-run diff.

### 8.4 Manual call script (the per-phase definition of done — `03-CONVENTIONS.md` §5)
Dial `VAPI_PHONE_NUMBER_ID` (O-4 placeholder / the provisioned test number), run each, and paste the **transcript + the resulting `VoiceCall` row + the staff email**:
1. **Repeated human request:** "Can I talk to a person?" … (agent helps) … "No, I really want a human." → on the **2nd** request the agent warm-transfers (operator hears the `{{transcript}}` summary); the `VoiceCall` is `outcome="escalation"`, `reason="repeated_request"`, and an `— URGENT` email lands. (A single first request does NOT transfer — C3.)
2. **Defective return:** "My vape cart is defective and I want a refund." → the agent de-escalates, speaks the **WAC 314-55-079** path (exchange, no time limit, original packaging + lot ID + receipt — grounded in the KB, not invented), then warm-transfers. `VoiceCall` is `defective_return`, `— URGENT` email lands. Paste the spoken policy text and confirm it matches the KB row (Numbers-Guard).
3. **Return dispute:** "You charged me for two but I only got one." → `outcome="escalation"`, `reason="dispute"`, warm transfer + urgent email.
4. **Plain FAQ call (control):** "What time do you close?" → answered, `outcome="faq_answered"`, a **non-urgent** per-call digest email lands (every call gets an email — ADR-017), no transfer.
5. **Transfer no-answer (if a test number that won't pick up is available):** confirm `transfer_disposition="no_answer"` is recorded and the urgent email still fires (the record/alert never depend on the operator answering).

**Test-data discipline:** deterministic fixtures; expected values hand-authored. The HMAC-fail-closed (F1) and Leak-Guard (E2) tests are **non-negotiable gates** on this phase (it touches the webhook + emits serialized output). Coverage: ~90% diff coverage on `voice/outcomes.py`, `crm/sinks.py` (the voice port), the `end_of_call_report` handler; never lower an existing ratchet.

### 8.5 Hygiene (paste all four — `03-CONVENTIONS.md` §1.3)
`ruff check` + `ruff format --check` clean; `python manage.py check` clean; `makemigrations --check` exit 0 (the `VoiceCall`/`VoiceTurn`/`AlertDelivery` migrations committed); targeted `pytest` green. **Never claim passing without the pasted output.**

---

## 9. Risks / open questions

| Risk / open item | Impact | Mitigation / disposition |
|---|---|---|
| **Transfer numbers unset (O-4).** | The warm transfer can't actually connect a human in test. | The escalation script + durable record + urgent email all still run; provision substitutes a documented placeholder + a `"transfer number not configured for <KEY>"` warning; flip on when the owner supplies the numbers. The destination is **config, not code** (ADR-016 / §2). Not a blocker. |
| **eocr re-delivery (Vapi retries) → duplicate record/email.** | Double-alerts, noisy staff inbox. | `VoiceCall.vapi_call_id` UNIQUE + `AlertDelivery (voice_call, sink)` `get_or_create` with a `status=="success"` short-circuit (ported from swedish-bot `dispatch`). Idempotency is an acceptance criterion (D2). |
| **A slow Gemini summary or SMTP call stalls the webhook** → Vapi marks the callback failed and retries → more dupes. | Latency / retry storm. | The **durable write is synchronous and first**; the summary is fail-soft (deterministic fallback string on error, never raises); the email is fail-loud-but-recorded (never blocks the 200). P5 optionally moves summary/email onto Celery so the webhook returns immediately — but the sync write stays sync (ADR-017). |
| **Outcome mis-classification** (e.g. a defective claim read as plain FAQ) under-alerts staff. | A real defect/dispute doesn't get an urgent alert. | `classify_outcome` precedence puts defective/repeated/dispute **first** (§4.4); the entry/escalation prompts emit `structuredData.reason` so classification doesn't rely on transcript regex alone; the unit table (C1–C3) pins the matrix. Owner can tune the lexicon in the prompt (P4 dashboard) without a code change. |
| **The escalation prompt could "promise" a refund** (over-stepping). | Customer expectation the store can't honor + a compliance miss. | The prompt instructs: **transfer for resolution, do not process the exchange yourself**; the WAC terms come from the KB row (Numbers-Guard), and the agent's job is to de-escalate + hand off. A few-shot reinforces "I'll get a manager on" over "we'll refund you." |
| **Staff alert routing — shared vs per-location (O-9).** | Wrong inbox gets the alert. | `_recipients_for(store)` sends to the shared `STAFF_ALERT_EMAIL` by default and ALSO to `STAFF_ALERT_EMAIL_<STORE>` when set (per-store override is additive, not replacing). Slack stays off until `SLACK_WEBHOOK_URL` + `SLACK_ALERTS_ENABLED=1`. Owner flips config, no code change. |
| **Leak via the AI summary** (the summary echoes an earlier product turn). | cost/margin in the email. | The summary is built from the transcript, which only ever contained `public_product`-shaped fields (budtender's allowlist serializer never sent cost/margin); a contract test asserts no `cost`/`margin` substring in the email body (E2). |
| **PII in `raw_eocr`** (the raw payload carries the caller number). | A raw number persisted in the JSON blob. | `raw_eocr` is stored with the caller number field **redacted to the hash** on write (the redactor runs before save); the test (PII) asserts the raw number is absent from the stored row. |
| **`transfer_disposition` inference from `endedReason`** may be imperfect (Vapi's reason taxonomy can change). | "no_answer" vs "connected" mislabeled. | Map a known set of `endedReason` values to dispositions in `voice/outcomes.py` with an `else → "not_attempted"` default; keep the raw `ended_reason` on the row so a later correction is a data fix, not a re-call. Flag as a small follow-up to verify against live Vapi reason strings. |
| **Open: does every call email staff, or only the alert-worthy ones?** | Inbox volume vs. completeness. | ADR-017 says "an email per call" + immediate on escalation/vendor/defective. Default = email every call (digest) + `— URGENT` on the immediate set. If the owner wants alert-only, gate the non-urgent digest behind `STAFF_DIGEST_EVERY_CALL=1` (default 1) — a one-line config flip, documented in §9 as the lever. |

---

## 10. Definition of done (P2)

- All §7 acceptance criteria pass with pasted output (`ruff check`, `ruff format --check`, `python manage.py check`, `makemigrations --check`, targeted `pytest`).
- The export's **dead escalation orphan is fixed**: a real call where the caller asks for a human twice (or reports a defective product / a dispute) reaches a person via a **warm transfer** with a `{{transcript}}` summary to a **populated** per-location number (or a documented placeholder under O-4), demonstrated in the manual script (§8.4 steps 1–3) with pasted transcript.
- Every call writes a **durable `VoiceCall`** (never dropped) and fires a **staff email** to `happytimeyak509@gmail.com` (+ per-store), **immediate** on escalation/vendor/defective — demonstrated (§8.4 step 4 control + steps 1–3 urgent) with the pasted `VoiceCall` row + email.
- The **HMAC-fail-closed** (F1) and **Leak-Guard** (E2) gates pass.
- **Idempotent provisioning** proven (G1: a re-run is drift-free, zero new Vapi objects).
- Docs updated **in the same change** (`03-CONVENTIONS.md` §6): bump this doc's status to `DONE` with the live-verified note; check off `12-P2-ESCALATION-TRANSFER-EMAIL.md` in `00-MASTER-ROADMAP.md` §7; record the `VoiceCall`/`VoiceTurn`/`Outcome` + `AlertDelivery` model shapes in `01-ARCHITECTURE.md` §8; note any new env defaults (`STAFF_DIGEST_EVERY_CALL`) in `03-CONVENTIONS.md` §3; append an ADR only if a real new architectural decision was taken (none expected — P2 implements ADR-016/017 as already decided).

---

## 11. Source-file anchors (for the executor)

- **Vapi export (the dead wiring P2 fixes):** `C:\Users\vladi\Downloads\happy-time-voice-agent-(full-script)-(uploaded-via-json).json` — `escalation` node (line 3537, prose "ask twice" with **zero inbound edges**), `transfer_call` node (line 3610) with **`"destinations": []`** (line 3628), the lone inbound edge `escalation → transfer_call` (lines 3950–3951), the `globalPrompt` "Escalation and transfer" clause (line 3960, rule 6), and the `slack_summary` end-of-call intent (best-effort Slack-only record → #9).
- **swedish-bot sinks (port the sink set + idempotent dispatch):** `C:\Users\vladi\OneDrive\Desktop\swedish-bot\crm\sinks.py` (`LeadSink`~L21, `DBSink`~L31, `EmailSink`~L40 incl. `enabled()` + `send_mail(fail_silently=False)`, `dispatch`~L119 idempotent per `(request, sink)` + never-raises). **Swap** the lead body → the voice-call body (§4.5); **swap** `WordPressOffertSink` → `SlackSink`; **swap** `LeadDelivery` → `AlertDelivery` (§4.2).
- **swedish-bot models (port the durable-record + phone-hash + escalation-reason idiom):** `C:\Users\vladi\OneDrive\Desktop\swedish-bot\crm\models.py` (`phone_hash`~L17, `Caller`~L34/`phone_hash` field~L37/L59, `Session`~L86 with `ai_summary`~L119, `ServiceRequest`~L138 with `escalation_reason`~L138).
- **swedish-bot Gemini (post-call summary):** `C:\Users\vladi\OneDrive\Desktop\swedish-bot\core\services\gemini.py` (`generate`, `MODELS`, token accounting) — lifted verbatim by P0; used by `voice/summarize.py`.
- **swedish-bot webhook/channel-adapter shape:** `C:\Users\vladi\OneDrive\Desktop\swedish-bot\chat\views.py` (`@csrf_exempt` + middleware-gated endpoint — "exactly the shape a Vapi webhook handler takes", roadmap §2) — the model for `voice/webhooks.py::end_of_call_report`.
- **KB content (consumed, seeded by P0):** WAC 314-55-079 defective exception + store-facts — research `_research-education-blogs.md` §10 (WA limits/return) + the synthesis brief §2 "Return policy" (WAC 314-55-079: defective product, no time limit, original packaging + legible lot ID + receipt) + `02-DECISIONS.md` ADR-012.
- **House style for the de-escalation copy:** `_research-education-blogs.md` §8 (warm/family/conservative; cite the source, never invent a figure).
- **Foundation (binding):** `C:\happytime-voice\docs\plans\{00-MASTER-ROADMAP,01-ARCHITECTURE,02-DECISIONS,03-CONVENTIONS}.md` (ADR-016/017/018/019 are the spine of this phase).
- **Shared payload builders (one shape, two callers):** `tools/provision_vapi.py` (P0) + `dashboard/publish.py` (`14-P4-dashboard-publish.md` §4.3/§4.4 `build_assistant_payload`/`build_squad_payload`) — P2's escalation transfer-tool block plugs into `model.tools`, the squad inbound edges into `assistantDestinations`.
- **Dependencies authored by other phases:** P0 (`core/services/vapi.py`, `tools/provision_vapi.py`, `core/middleware.py` HMAC, `voice/webhooks.py` dispatcher + the 5-member Squad scaffold + `voice/models.py` skeleton + `crm/models.Caller` + `kb/seed.py` KB), P3 (`crm/models.VendorCallback` + the `vendor_callback` outcome label P2's classifier recognizes), P5 (`voice/tasks.py` Celery wrap of this handler + `VoiceTurn.latency_ms`).
