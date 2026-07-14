# Next-Level Audit — happytime-budtender vs swedish-bot v2 (Nordland)

Date: 2026-07-14
Scope: read-only audit. No code in this repo was changed to produce this document.
Companion repo: `C:\Users\vladi\OneDrive\Desktop\swedish-bot` (the "Nordland v2" build), used as the source of
patterns below. All swedish-bot file refs are relative to that repo; all happytime-budtender refs are relative
to this repo.

## 1. Executive summary

happytime-budtender is a mature, well-tested Django app (~5,300 lines of tests, a 100-conversation regression
suite, three-layer Dutchie POS client, local AAMVA ID-scan decode with cloud OCR fallback) with one standout
piece of engineering: the hash-based zero-drift Vapi publish pipeline in `voice/voice/provision.py`. That
pattern is good enough that swedish-bot should steal it back.

But budtender is missing almost every *operational safety-net* pattern that swedish-bot's v2 build added on
top of a similar first version: no live-eval harness with a simulated customer + judge, no deterministic
conversation FSM (routing is delegated entirely to Vapi's hosted Squad/Workflow, which is LLM-judged rather
than code-owned), a single-layer keyword guardrail with no mention-vs-instruction distinction, no no-clobber
prompt-sync discipline (nothing indicates prompt drift is even possible here, but there's also no export/diff
tooling to catch it if it starts), and — most seriously for a cannabis retailer holding driver's-license
scans — **no PII purge or retention mechanism at all**: `Customer.raw_scan` stores unredacted DOB/address/ID
number indefinitely with zero grep hits for purge/retention logic anywhere in the repo.

The single highest-leverage gap is the eval harness: without it, every prompt or flow change in this dispensary
bot ships on vibes, backed only by unit tests that don't simulate an adversarial or confused customer talking
to the live LLM. The single highest-leverage exposure is the PII retention gap: it's a live legal/compliance
liability, not a nice-to-have.

## 2. Side-by-side architecture comparison

| Area | swedish-bot v2 (Nordland) | happytime-budtender | Verdict |
|---|---|---|---|
| Conversation control | Deterministic FSM, code-owned (`chat/orchestrator.py`) | Vapi hosted Squad + Workflow graph, LLM-judged routing (`voice/voice/routing.py` is test-only, not runtime) | **A wins** — B has no code-owned flow control |
| Prompt ops | DB-as-source-of-truth, no-clobber seed + export/import/diff (`docs/plans/2026-07-14-prompt-sync.md`) | No seed-clobber tooling found; prompts live in `AgentPrompt` but no export/diff/reconcile commands | **A wins** |
| Eval / regression | Live-eval harness: sim customer, LLM judge, hard deterministic release metrics, `code_version` tagging (`tools/eval/`) | ~5,300 lines unit/integration tests incl. 100-conversation regression test; zero simulated-customer/judge harness | **A wins** — biggest gap |
| Safety/guardrails | 2-layer: keyword veto w/ mention-vs-instruction distinction (sv+en) + LLM second opinion, add-only (`chat/guardrails.py`) | 1-layer keyword regex (`voice/voice/guardrails.py`), English-only, no mention-vs-instruction split; but has a strong recursive `scrub_leak()` payload scrubber A lacks | **Mixed** — A's layering is better, B's leak-scrubbing is better |
| Knowledge corpus | Approval-gated FAQ/KB, per-category specialist prompts, HTMX CRUD dashboard (`kb/`, `dashboard/knowledge.py`) | No equivalent approval-gated product-knowledge corpus found | **A wins** |
| Customer profile | Deterministic problem descriptor, never LLM (`crm/profile.py::build_problem_descriptor`) | `Customer.raw_scan` holds raw unredacted scan data; no descriptor synthesis | **A wins** |
| PII/retention | `purge_pii` management command (dry-run default), `LeadDelivery` per-sink retry tracking | No purge/retention mechanism anywhere; raw ID-scan PII kept indefinitely | **A wins — B has a real gap** |
| Deploy ops | `post_deploy` idempotent chain + `selfcheck` PASS/FAIL/WARN report | Manual smoke scripts only (`login_smoke.py`, `pos_smoke.py`), no post-deploy/selfcheck | **A wins** |
| Provisioning / voice-platform publish | No equivalent — swedish-bot doesn't own a hosted voice platform object graph the way budtender does | Hash-based zero-drift publish (`voice/voice/provision.py::_reconcile`), zero-API-call short circuit, id-then-name fallback, `last_provision_hash`/`last_publish_hash` | **B wins — steal this for A** |
| POS/commerce integration | None (service-call domain, no POS) | Three-layer Dutchie client (backoffice/read/register), graceful GET degrade, raise-on-POST-failure | **B wins (domain-specific, not portable)** |
| Age/identity verification | N/A | Local AAMVA barcode decode + cloud OCR fallback (`idscan/`), but **not wired into the phone/voice flow** — phone age gate is pure self-attestation (`voice/voice/guardrails.py::age_gate_required`) | **Gap unique to B's domain** |
| Geofencing | Pure-Python polygon geofence, fail-closed validator (`crm/geo.py`) | Fixed 3-store enum, no geofence needed | N/A — domain difference, not a gap |

## 3. Ranked improvement list

### 1. Eval harness + simulated-customer judge (biggest gap)
**What/why:** Budtender has strong unit tests but nothing that drives the live LLM through realistic,
adversarial, or confused customer conversations and scores outcomes deterministically. Prompt/flow changes to
a regulated cannabis-advice bot currently ship with no regression signal beyond hand-written test fixtures.
**Evidence:** No `tools/eval/`-equivalent directory found in happytime-budtender; contrast with swedish-bot's
`tools/eval/customer_sim.py`, `judge.py`, `personas.py`, `runner.py`, `report.py`, `drive_eval.py` and
`docs/evals/2026-07-14-v2-validation.md`.
**Pattern to port:** Crib `tools/eval/customer_sim.py` + `judge.py` + `personas.py` wholesale as the
scaffold; swap persona content for dispensary personas (first-time buyer, medical patient asking for dosage
advice, minor attempting to order, edge-case product question, price-sensitive haggler). Keep the
deterministic "hard release metrics" idea (false-resolution rate analog + a "gave medical/dosage advice"
detector) separate from noisy LLM-judge scoring, and keep `code_version` git-hash tagging so mixed-version
result files can be pruned.
**Size:** M-L (new harness + dispensary-specific personas + at least one hard deterministic metric).
**Risk:** Low — pure addition, read-only against the running bot in eval mode.

### 2. Compliance guardrails — 2-layer mention-vs-instruction upgrade
**What/why:** Budtender's single-layer keyword regex can't distinguish a customer mentioning a topic (e.g.
"my friend said edibles hit different") from an actual instruction/request for medical or dosage advice. A
2-layer design with an LLM second opinion that can only *add* safety (never remove it) closes false negatives
without needing per-keyword precision tuning for every phrasing.
**Evidence:** `voice/voice/guardrails.py` (single regex layer, English-only) vs swedish-bot's
`chat/guardrails.py::keyword_unsafe` (mention-vs-instruction window, `_NOUN_CUE_WINDOW=45` chars) and
`classify_unsafe` (LLM second opinion) composed via `is_unsafe()`.
**Pattern to port:** `chat/guardrails.py` — port the noun-cue-window technique and the add-only LLM
second-opinion composition. Cannabis-specific hard triggers: dosage/mg advice, drug-interaction claims,
"will this cure/treat X" medical claims, cross-state shipping requests, obvious minor indicators.
**Size:** M.
**Risk:** Low-medium — must be tuned carefully to avoid over-blocking legitimate product questions; ship
behind a shadow-mode logging period before enforcing.

### 3. PII / retention — purge mechanism for raw ID-scan data
**What/why:** `Customer.raw_scan` retains full unredacted driver's-license data (DOB, address, ID number)
indefinitely with no purge path found anywhere in the codebase. For a licensed cannabis retailer this is a
live compliance and breach-blast-radius problem, not a style issue.
**Evidence:** grep across happytime-budtender returns zero hits for "purge"/"retention"; contrast with
swedish-bot's `crm/management/commands/purge_pii.py` (dry-run default, `--days`/`--yes` flags, scrubs
`payload_json["customer"]` on FK-nulled rows) and `LeadDelivery` model tracking per-sink delivery so purge
doesn't destroy undelivered records.
**Pattern to port:** `crm/management/commands/purge_pii.py` structure — dry-run-by-default management command,
explicit `--days`/`--yes` gate, and a redaction pass that keeps the *fact* a scan occurred (age-verified: yes,
date) while dropping the raw AAMVA payload after some short retention window tied to state cannabis
recordkeeping requirements (check WA/whatever-state minimum retention before choosing the window — do not
just copy swedish-bot's default days value).
**Size:** M (command + a decision on legally-required minimum retention).
**Risk:** Medium — must confirm state recordkeeping law before deleting anything; get this reviewed, don't
just ship the eng pattern.

### 4. Deterministic FSM for conversation flow, layered under the Vapi Squad/Workflow
**What/why:** Budtender's flow control is delegated to Vapi's hosted Squad + Workflow graph — an LLM judges
routing/edge conditions at runtime, with `voice/voice/routing.py`'s classifier only exercised in tests, not in
production. That means there's no code-owned confirm-fix loop, no question budget, no "have we already asked
this" memory — behavior can drift with model updates on Vapi's side, invisibly.
**Evidence:** swedish-bot `chat/orchestrator.py` — explicit states (`STATE_INTAKE/ROUTING/SPECIALIST/...`),
`_confirm_verdict()` for yes/no/question classification, `cs["report"]["checks"]` tracking helped/no_help/
refused per suggested action injected into every prompt, `REPLY_BUDGET=5`/`GENERAL_REPLY_BUDGET=3`,
`MAX_TOTAL_TURNS=25`.
**Pattern to port:** Don't replace Vapi's Squad wholesale (Vapi's phone/voice orchestration has real value
budtender already paid for) — instead port the *budget and memory* concepts as a thin deterministic layer
that Vapi tool-calls into: a per-call turn counter with an escalate-to-human ceiling, and a "have we already
told this customer X" set so specialist tools don't repeat themselves. This is the least risky FSM slice to
port because it doesn't require rearchitecting Vapi's routing, just adding guardrails around it.
**Size:** L (touches the live voice runtime).
**Risk:** Medium-high — changes call-flow behavior; needs the eval harness (#1) built first so this can be
measured before/after.

### 5. Approval-gated product-knowledge corpus
**What/why:** No equivalent to swedish-bot's approval-gated FAQ/KB with per-category specialist prompts was
found. Budtender presumably answers product questions from Dutchie catalog data + LLM knowledge, with no
curated, human-approved corpus of vetted answers to recurring questions (e.g. "what's the difference between
indica/sativa/hybrid", "what does terpene X do", state-specific purchase-limit questions).
**Evidence:** swedish-bot `kb/` (`FAQEntry.is_approved`/`SiteFAQ.is_approved` gates retrieval in
`kb/semantic.py::rank_general_knowledge`), `chat/orchestrator.py::_GENERAL_ROLE_BY_FAMILY` per-category
specialist roles, plus the new (uncommitted at time of writing) `dashboard/knowledge.py` HTMX CRUD pages.
**Pattern to port:** `kb/` approval-gate model + `dashboard/knowledge.py` CRUD pattern, populated with
dispensary-specific categories (product education, legal limits, house policies) instead of heat-pump/water
categories.
**Size:** M.
**Risk:** Low.

### 6. Customer profile problem/interest descriptor
**What/why:** Budtender stores raw scan and transaction data but has no deterministic, human-readable synthesis
of "what does this customer typically want" the way swedish-bot's profile page shows a one-line problem
descriptor.
**Evidence:** swedish-bot `crm/profile.py::build_problem_descriptor` (deterministic, never LLM, e.g. `"IVT Geo
412C — larm H01 5252, otillräckligt varmvatten (2026-07-14)"`).
**Pattern to port:** Same function shape, deterministic (no LLM) synthesis from last N orders/interactions —
e.g. `"Prefers indica flower, avg ticket $45, last visit 3d ago, asked about CBD topicals"`. Keep it
non-LLM so it's cheap, fast, and doesn't hallucinate purchase history.
**Size:** S.
**Risk:** Low.

### 7. Question budget / conversation efficiency tracking
**What/why:** Without a budget, phone calls can loop asking the same clarifying question. swedish-bot's
`REPLY_BUDGET`/`MAX_TOTAL_TURNS` concept caps this and forces escalation.
**Evidence:** see item 4 evidence.
**Pattern to port:** Same constants pattern, tuned for phone-call length norms in a retail dispensary context
(likely shorter budget than a troubleshooting call).
**Size:** S (if built as a counter alongside item 4; not worth doing standalone before the FSM work).
**Risk:** Low.

### 8. Prompt-sync ops (export/import/diff) — preventative, not yet urgent
**What/why:** No evidence budtender has hit prompt-seed-clobber yet, but there's also no tooling to catch it
if a `get_or_create`-vs-`update_or_create` mistake creeps in later, or to diff dashboard-edited prompts against
code defaults.
**Evidence:** swedish-bot `docs/plans/2026-07-14-prompt-sync.md`, `kb/management/commands/export_agent_config.py`
/ `import_agent_config.py` / `agent_config_diff.py`.
**Pattern to port:** `agent_config_diff.py` alone is worth adding now (cheap, read-only, catches drift before
it's ever a problem) even before deciding whether the fuller export/import machinery is needed.
**Size:** S (diff tool only) to M (full export/import).
**Risk:** Low.

### 9. Deploy selfcheck
**What/why:** Budtender has manual smoke scripts (`login_smoke.py`, `pos_smoke.py`) but no single idempotent
post-deploy chain or PASS/FAIL/WARN health report.
**Evidence:** swedish-bot `core/management/commands/post_deploy.py` (idempotent chain) + `selfcheck.py`
(migrations, prompt completeness, vendor data, postcode data, static file presence checks).
**Pattern to port:** `selfcheck.py` structure, checking: migrations applied, `AgentPrompt` completeness per
category, Vapi provision hash matches live (tie into item 10 below), Dutchie API reachability, ID-scan OCR
fallback credentials present.
**Size:** S-M.
**Risk:** Low.

### 10. (Reverse direction) Port budtender's zero-drift Vapi publish into swedish-bot
**What/why:** This is the one pattern flowing the other way. Budtender's `voice/voice/provision.py::_reconcile`
hashes the canonical redacted payload, short-circuits to zero API calls on no-drift, and does id-then-name
fallback for create-or-update — a genuinely elegant way to make voice-platform publishing idempotent and safe
to run on every deploy or every dashboard save.
**Evidence:** `voice/voice/provision.py` (~L376-432), `voice/dashboard/publish.py` (instant-publish on save
mirrors the same hash gate against `AgentPrompt.last_publish_hash`).
**Action:** Flag this as a follow-up for the swedish-bot repo (out of scope here since this document lives in
budtender and must stay read-only there) — worth a dedicated small task in swedish-bot once it needs to publish
to a hosted voice platform (e.g. if Nordland's Vapi/telephony work in
`docs/plans/2026-07-12-design-telephony-vapi-whatsapp.md` goes live).
**Size:** M (in the other repo).
**Risk:** Low.

## 4. "Steal this week" — top 5, exact steps

1. **Eval harness skeleton** (item 1): Copy `swedish-bot/tools/eval/customer_sim.py`, `judge.py`,
   `personas.py`, `runner.py` into `happytime-budtender/tools/eval/`. Replace persona content with 5-10
   dispensary personas. Wire `runner.py` to call budtender's actual chat/voice entry point instead of
   swedish-bot's. Define one hard deterministic metric first: "bot gave dosage/medical advice" detector
   (regex + keyword list), run it against a first batch of 20 sim conversations, and read the transcript
   output before trusting the detector.
2. **`agent_config_diff.py` port** (item 8): Copy the read-only diff tool, point it at `AgentPrompt` in this
   repo, run it once now to get a baseline (should show zero drift today) and add it to a weekly cron/CI check.
3. **PII audit note + purge command stub** (item 3): Do NOT delete anything yet. Write the dry-run command
   structure (`purge_pii.py` port) but leave it disabled/dry-run-only until state recordkeeping minimums are
   confirmed. This week's deliverable is the dry-run report showing how much raw scan PII currently exists and
   how old it is — that number alone is useful for a compliance conversation.
4. **Guardrail shadow-mode logging** (item 2): Port `keyword_unsafe`'s mention-vs-instruction window logic
   into `voice/voice/guardrails.py` in shadow mode (log-only, don't block) for one week, then review false
   positive/negative rate before flipping to enforce.
5. **`selfcheck.py` port** (item 9): Smallest, safest, highest immediate value — a single command that
   verifies migrations/prompts/Dutchie reachability/Vapi provision-hash-match in one PASS/FAIL/WARN table,
   runnable manually today and pluggable into deploy later.

## 5. Do-NOT-port list

- **Geofence/polygon service-area logic (`crm/geo.py`)** — swedish-bot needs this because it's a mobile
  service-call business with a drivable radius; budtender has 3 fixed dispensary storefronts, a simple enum
  is correct and simpler. Do not add geo-polygon machinery here.
- **Prefill token pattern (`chat/prefill.py`)** — built for handing chat context to an external
  booking-install form. Budtender has no equivalent external form to hand off to (Dutchie cart/checkout is
  its own POS surface, not a form this app owns). Skip.
- **Swedish-language dual regex in guardrails** — obviously US-English-only domain; port the *mechanism*
  (mention-vs-instruction window) from item 2, not the sv+en bilingual pattern.
- **`LeadDelivery` per-sink retry model as-is** — swedish-bot's model is shaped around delivering a service
  lead to multiple downstream sinks (email/SMS/dashboard). Budtender's analogous need (if any) is order/cart
  handoff to Dutchie POS, which already has its own three-layer client (`dutchie/`) with its own retry
  semantics — don't bolt on a parallel delivery-tracking model; if delivery reliability to Dutchie is a real
  problem, extend the existing `dutchie/` client's error handling instead of importing swedish-bot's CRM
  concept wholesale.
- **Full deterministic FSM replacement of Vapi Squad/Workflow** — see item 4: port budget/memory concepts as
  a thin layer, do not rip out and replace Vapi's hosted routing. That's a large, risky rearchitecture with
  no evidence it's needed versus tuning the existing Vapi Workflow.
