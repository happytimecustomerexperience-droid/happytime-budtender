# CLAUDE.md — Happy Time Voice (repo guide for AI sessions)

Read this first, then [`docs/plans/00-MASTER-ROADMAP.md`](docs/plans/00-MASTER-ROADMAP.md) +
[`docs/plans/02-DECISIONS.md`](docs/plans/02-DECISIONS.md) before changing anything.

## What this is
Django control plane that drives a **Vapi Squad** voice agent for Happy Time Weed (cannabis, WA). It answers
FAQ/return-policy, recommends products via the `happytime-budtender` service, routes vendor calls, escalates
to humans, emails staff, and is editable from a branded dashboard + auto-deployable via the Vapi REST API.
Forked from the `swedish-bot` chassis.

## Layout
- `config/` Django settings (lean, env-driven, prod-fail-closed) · `core/` gemini client + Vapi REST client +
  constants + healthz · `voice/` webhook + tools + provisioning + models (VoiceCall/VoiceTurn) + guardrails ·
  `kb/` models + embeddings + seed + Vapi-files mirror · `crm/` phone-hash + sinks + VendorCallback ·
  `dashboard/` agents/flow-canvas/KB/monitor/queues/analytics/publish · `docs/plans/` the full spec suite.

## Non-negotiable invariants
- **Vapi = Assistants + ONE Squad** for the squad provisioner (`provision.py` never touches `/workflow` — a test guards this). **Exception (ADR-023):** the guided product questionnaire runs as an owner-authorized Vapi **Workflow**, built from its own module (`voice/workflow.py` + `provision_workflow`); the squad stays live as fallback until cutover.
- **Provisioning is idempotent + zero-drift** (`provision_vapi`); re-runs issue 0 creates.
- **Webhooks fail closed** — HMAC/secret verified first, constant-time; no handler runs on bad proof.
- **Leak-guard is code-owned** — `faq_lookup`/suggestions can never emit `cost`/`margin` (central scrub +
  budtender allowlist + tests). Do not weaken.
- **Numbers-Guard** — the agent never invents a price/stock/number; un-grounded → offer a human.
- **Dutchie keys live only in the budtender service.** This repo calls budtender over HTTP (Bearer).
- **Voice/model/transcriber set once per assistant** (no per-node duplication — the export's bug).
- Dashboard flow canvas is **config+docs only**; safety guardrails stay in Python, `_clean_graph` fail-closed.

## Working here
- `uv` for everything. Tests MUST stay **offline/key-free** — mock Gemini/Vapi/budtender/SMTP (see `conftest.py`).
- Verify before "done": `uv run ruff check` + `uv run python manage.py makemigrations --check` + `uv run pytest -q`.
- Build ON TOP of committed phases; match existing style; surgical changes.

## Phase status (2026-06-22)
P0 chassis+FAQ · P1 suggestions+personalization · P2 escalation+email · P3 vendor · P4 dashboard+publish ·
P5 polish/brand — all code-complete + green (363 tests). Owner-gated remaining: real Vapi/Dutchie keys,
Vapi phone number, brand assets (`brand/CAPTURE.md`), budtender-side endpoint TODOs (`docs/plans/21-SPEC`),
live-call smoke. See README "Owner checklist".
