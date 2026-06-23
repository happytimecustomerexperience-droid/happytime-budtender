# 10 — P0 — CHASSIS + GROUNDED FAQ — Executable Plan

> **Status:** EXECUTABLE SPEC (authoritative for P0). Written 2026-06-22.
> **Subsystem:** S1 (Chassis + FAQ). **Implements ADRs (binding, never contradicted here):** ADR-001 (fork swedish-bot chassis), ADR-002 (Assistants + ONE Squad, never a Workflow), ADR-003 (idempotent code-provisioned via documented Vapi REST), ADR-008 (leak-safe), ADR-009 (speak OTD), ADR-010 (gpt-4.1-mini assistants / Gemini server-side), ADR-011 (voice/persona set ONCE per member), ADR-012 (seed ALL KB sources; canonical = Django `kb/`, mirrored to Vapi Files), ADR-013 (swedish-bot embeddings; pgvector swap-seam documented), ADR-017 (eocr → durable `VoiceCall` → email sink), ADR-018 (spoken 21+ confirm; drop "peek at ID"), ADR-019 (HMAC fail-closed, prod-fail-closed, per-store Dutchie keys ONLY in budtender), ADR-020 (`voice/tools/` package + registry).
> **Read order before executing (mandatory):** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → this file.
> **Ports from:** `swedish-bot` (`config/settings.py`, `Dockerfile`, `docker-compose*.yaml`, `Caddyfile*`, `Makefile`, `pyproject.toml`/`uv.lock`, `core/services/gemini.py` **VERBATIM**, `core/constants.py`, `core/middleware.py`, `core/views.py::healthz`, `kb/models.py` `AgentPrompt`~L226 / `FlowConfig`~L276 / `FAQEntry`~L150 / `PolicyDocument`~L312 / `SiteFAQ`~L368, `kb/semantic.py`, `kb/ingest.py`, `crm/models.py::phone_hash`~L17 + `EmailSink`/`dispatch`). **Net-new:** `core/services/vapi.py`, the whole `voice/` app, `kb/seed.py`, `kb/vapi_files.py`, `crm/sinks.py` adaptation, `tools/provision_vapi.py`.
>
> **One-line goal:** a real inbound phone call to the provisioned Vapi number is greeted as "Koptza," confirmed 21+, and answers **hours / specials / returns / payment / pickup / WA-limits / weights+types** grounded in the seeded KB (no hallucinated facts), while a durable `VoiceCall` + `VoiceTurn` log is written — proving the **HMAC-verified webhook contract, the idempotent Vapi REST provisioner, the `voice/tools/` registry, and the KB grounding pipeline** that every later phase consumes.

---

## 0. Why P0 is the fork gate (gap G-11 — read this first)

P0 is **serial and first** (roadmap §4/§6). It lays the four artifacts every later phase imports, and **nothing else forks until these land and are green**:

1. **The webhook contract** (`voice/webhooks.py` POST `/api/voice/vapi` — the 4 event kinds + the exact request/response JSON shapes in §4). This is THE shared contract P1 (`suggest_products`/`check_inventory`/`pair_upsell`), P2 (`end-of-call-report` enrichment), and P3 (`notify_vendor_callback`) all consume. If its shape moves after P1–P3 fork, three worktrees break.
2. **`core/services/vapi.py`** — the idempotent Vapi REST client. P4's "Publish to Vapi" and every provision run reuse it.
3. **The `voice/tools/` package + `TOOL_REGISTRY`** (ADR-020) — so P1/P2/P3 each add their own module (`suggest.py`/`vendor.py`) instead of all editing one file. P0 MUST ship the registry scaffold even though only `faq.py` is wired in P0.
4. **`voice/models.VoiceCall`/`VoiceTurn`/`Outcome`** — the durable call log P2/P3/P4 read and enrich.

**THE FORK GATE (binding):** P1, P2, and P3 worktrees do **not** fork until (a) `voice/webhooks.py` dispatches all 4 event kinds with §4's shapes, (b) `core/services/vapi.py` GET/POST/PATCH on `/assistant`,`/squad`,`/tool`,`/phone-number` is merged, (c) `voice/tools/__init__.py::TOOL_REGISTRY` + `register()` is merged, and (d) the §7 P0 acceptance criteria pass with pasted output. The orchestrator holds P1–P3 dispatch behind this gate. (See `00-MASTER-ROADMAP.md` §4 "Hard dependencies" + §6 "P0 is serial and first.")

---

## 1. Goal & scope

### 1.1 In scope (this phase ships all of)

A bootable Django service forked from swedish-bot that stands up the chassis, the KB-grounded FAQ surface, and exactly **ONE** permanent Vapi assistant (entry + FAQ merged) inside ONE Squad:

1. **Chassis fork** — `config/settings.py` (lean, single file, env-driven, **prod-fail-closed**), `Dockerfile`, `docker-compose.yaml` + `.prod`, `Caddyfile` + `.prod`, `Makefile`, `pyproject.toml` + `uv.lock`. `core/services/gemini.py` lifted **VERBATIM**; `core/constants.py` lifted (model/pricing single source of truth).
2. **`core/services/vapi.py`** — the idempotent Vapi REST client (GET/POST/PATCH/DELETE on `/assistant`, `/squad`, `/tool`, `/phone-number`; Bearer `VAPI_PRIVATE_KEY`; base `https://api.vapi.ai`; **never `/workflow`**). Methods are fully implemented enough for P0 provisioning; the deep CRUD detail + edge cases are specced in `20-SPEC-vapi-deploy.md` (referenced, not duplicated).
3. **`GET /healthz`** — liveness + dependency status: **DB reachable + Gemini auth configured + Vapi auth reachable** (a cheap `GET /assistant?limit=1`). 200 when all green, 503 degraded.
4. **The voice webhook** — `voice/webhooks.py` POST `/api/voice/vapi` handling **`assistant-request` | `tool-calls` | `status-update` | `end-of-call-report`**, HMAC/secret-verified and **fail-closed** before any handler runs (constant-time compare). This is the shared contract (§4).
5. **`voice/tools/` registry + `faq_lookup`** — the `TOOL_REGISTRY` dispatch scaffold (ADR-020) + the `faq.py` handler that reads `kb/` live (canonical) and answers grounded.
6. **`voice/models.py`** — `VoiceCall` + `VoiceTurn` + `Outcome` durable call log (the eocr handler writes them synchronously).
7. **`kb/` fork** — models (`FAQEntry`/`PolicyDocument`/`StoreFact`/`EducationDoc`/`BlogDoc` + the weights/types taxonomy + a weight/type taxonomy table), `ingest.py`, `semantic.py` (Gemini 768-dim Matryoshka + cached cosine — **gap G-6**, file-tasks + acceptance below), `seed.py` (**gap G-7**, every research/brief section → a concrete model row), `vapi_files.py` (mirror KB → Vapi Files + Query Tool).
8. **One permanent Vapi assistant** — `entry_faq` (entry + FAQ merged for P0), Koptza persona, Cartesia sonic-3 voice, Deepgram nova-3 + keyterms, the `faq_lookup` tool bound (`server.url → ${PUBLIC_BASE_URL}/api/voice/vapi`), inside ONE Squad. Provisioned by `tools/provision_vapi.py` (idempotent, re-runnable, zero drift).
9. **The durable-log + grounded-answer round trip** — a real inbound call answers an FAQ from KB content and writes a `VoiceCall` row.

> **P0 merges entry + FAQ into ONE assistant `entry_faq`.** The full 5-member Squad (`entry_router`/`budtender`/`faq`/`vendor`/`escalation`) is reached incrementally: P1 splits `budtender` off, P2 adds `escalation`, P3 adds `vendor`, and P3/P5 split `entry_router` from `faq`. P0 ships the Squad **container** with one member so the provisioner + publish path are proven on day one and later phases only ADD members (never restructure). The assistant is NAMED `entry_faq` and its `AgentPrompt.role` is `"faq"` so the later `faq` split is a rename, not a new row. (See §6.4.)

### 1.2 Out of scope (other phases / EXP)

- **Product suggestions / Dutchie / budtender** — P1 (`voice/budtender_client.py`, `voice/tools/suggest.py`, the `budtender` member). P0 does **not** touch budtender, has **no** Dutchie key, and quotes **no** product prices.
- **Escalation / transfer / staff email beyond the eocr durable write** — the `EmailSink` is ported and wired into `crm/sinks.py::dispatch`, but the escalation MEMBER, warm `transferCall`, and the immediate-alert routing are P2.
- **Vendor routing** — P3 (`voice/tools/vendor.py`, `crm/models.VendorCallback`, the `vendor` member).
- **The full dashboard + Publish-to-Vapi** — P4. P0 ships only `core/services/vapi.py` (the client) + `tools/provision_vapi.py` (the script); the editor UI is P4.
- **Cartridge entry from router, back-edge corrections, brand visuals, Celery, analytics** — P5.
- **pgvector swap, SMS, web-chat widget** — EXP (the swap-seam is documented in `kb/semantic.py`, not built).

### 1.3 Non-negotiable boundaries (binding)

- **HMAC fail-closed at the edge.** The Vapi webhook rejects a missing/bad signature with **401 before any handler runs**, constant-time `hmac.compare_digest` (ADR-019). This is a non-negotiable test gate (`03-CONVENTIONS.md` §5).
- **Numbers-Guard.** The LLM (gpt-4.1-mini on the assistant, Gemini server-side) **never originates a figure** — hours/limits/prices/quantities come from KB rows; the model only phrases them (ADR-012, `_research-education-blogs.md` §1 house rule). The FAQ assistant answers ONLY from `faq_lookup` results.
- **Leak-Guard (defensive, even with no products in P0).** No tool response may contain a `"cost"`/`"margin"` substring (ADR-008). P0 ships the contract test even though `faq_lookup` returns no product fields — it guards the surface before P1 adds products.
- **Prod-fail-closed.** With `DJANGO_DEBUG=0` the app refuses to boot unless `DJANGO_SECRET_KEY` is non-default AND `PHONE_HASH_PEPPER` differs from `DJANGO_SECRET_KEY` AND `VAPI_PRIVATE_KEY` + `VAPI_WEBHOOK_SECRET` are present (`03-CONVENTIONS.md` §1.2, extending swedish-bot `config/settings.py`~L153).
- **Idempotent provisioning, GET-then-PATCH, zero drift.** A second `provision_vapi.py` run creates **zero** new Vapi objects (ADR-003 acceptance criterion).
- **Voice/transcriber/model set ONCE per member** (ADR-011) — never per node (the export's #7 bloat). A unit test asserts the keyterm list appears exactly once per assistant payload.
- **No `/workflow` call, ever** (ADR-002). Only documented CRUD on `/assistant`, `/squad`, `/tool`, `/phone-number`.

---

## 2. Dependencies (what MUST exist first)

P0 is the first phase — its only external dependencies are **owner-supplied env placeholders** (which are READ, never required at import, so P0 ships against stubs) and the swedish-bot source tree to fork.

| # | Dependency | Where it comes from | Graceful-degradation if absent |
|---|---|---|---|
| D1 | swedish-bot chassis source | `C:\Users\vladi\OneDrive\Desktop\swedish-bot` (confirmed present, §11) | n/a — the fork source. |
| D2 | `VAPI_PRIVATE_KEY` | owner (Vapi dashboard) | Provision + healthz Vapi-check degrade to "vapi: not configured"; the rest of the app boots. The provisioning test runs against a **mocked** `vapi.py`; a live run needs a real key. |
| D3 | `VAPI_WEBHOOK_SECRET` | owner (chosen secret, also set on the Vapi assistant `server.secret`) | In `DEBUG=1` a dev default is allowed; in `DEBUG=0` its absence **fails the boot** (prod-fail-closed). |
| D4 | `VAPI_PHONE_NUMBER_ID` / inbound number | owner (**O-4 placeholder**) | Provision creates the Squad + assistant + tool regardless; the phone-number attach step is **skipped with a logged "phone id not configured"** when unset. The manual call script needs a real number. |
| D5 | `VAPI_VOICE_ID` (Cartesia sonic-3 Koptza) | known constant `a3520a8f-226a-428d-9fcd-b0a4711a6829` (ADR-011) | Hard-defaulted; no blocker. |
| D6 | Gemini/Vertex auth (`GOOGLE_CLOUD_PROJECT` or `GEMINI_API_KEY`) | owner / swedish-bot `.env` pattern | `embed()` falls back gracefully; with no auth, `kb/semantic.py` cosine is disabled and `faq_lookup` falls back to a deterministic keyword/trigram match over KB rows (still grounded). healthz reports `gemini: not ready` → 503. |
| D7 | Postgres (docker-compose `db` service) | the ported `docker-compose.yaml` | n/a — provided by the fork. |
| D8 | Mt Vernon hours (**O-8 placeholder**) | owner | `kb/seed.py` seeds a "call to confirm" stub StoreFact for Mt Vernon hours; Yakima/Pullman hours seed real (ADR / O-8). |
| D9 | `STAFF_ALERT_EMAIL` / SMTP (**O-9 placeholders**) | owner | `EmailSink` reads env; with no SMTP it logs the would-send and the durable `VoiceCall` write still happens (the record is never lost — ADR-017). |

**Graceful-degradation rule:** every owner-supplied placeholder is read at call time, never required at import. The app boots, `healthz` reports each dependency's status, and provisioning/answering degrade with a clear logged reason instead of crashing.

---

## 3. File-by-file task list

Each entry: **exact path → responsibility → key functions/shape → source file to port from (with its path)**. New files marked ★; verbatim lifts marked ⎘; ported-and-adapted cite the swedish-bot original.

### 3.1 Chassis (fork the deploy skeleton)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `config/settings.py` | Lean single settings file, env-driven, **prod-fail-closed**. Apps = `core,voice,kb,crm,dashboard`. Adds the voice/Vapi/budtender env reads (§4 of `03-CONVENTIONS.md`). | Extend the `if not DEBUG:` block (swedish-bot ~L153) to ALSO require `VAPI_PRIVATE_KEY` + `VAPI_WEBHOOK_SECRET` present AND `PHONE_HASH_PEPPER != SECRET_KEY` → else `ImproperlyConfigured`. Keep HSTS/SSL/secure-cookie hardening. `INSTALLED_APPS` drops swedish-bot's HVAC apps; adds `voice`. | `swedish-bot/config/settings.py` (whole file; the fail-closed block ~L153-176, `PHONE_HASH_PEPPER` ~L127). |
| `config/urls.py` | Route map: `admin/`, `/api/voice/` (→ `voice.urls`), `/dashboard/` (P4 stub now), `/healthz`. | `path("api/voice/", include("voice.urls"))`, `path("healthz", core_views.healthz)`. | `swedish-bot/config/urls.py`. |
| `config/wsgi.py` / `config/asgi.py` | WSGI/ASGI entrypoints. | unchanged shape. | `swedish-bot/config/{wsgi,asgi}.py`. |
| `Dockerfile` | uv-based Python-only image (committed Tailwind, no node). | unchanged; rename app refs. | `swedish-bot/Dockerfile`. |
| `docker-compose.yaml` | Dev: `web` + `db` (Postgres) + `caddy`. | rename service/db names to `happytime_voice`. | `swedish-bot/docker-compose.yaml`. |
| `docker-compose.prod.yaml` | Prod override: forces safe flags, real env. | unchanged shape. | `swedish-bot/docker-compose.prod.yaml`. |
| `Caddyfile` / `Caddyfile.prod` | Auto-HTTPS reverse proxy. `Caddyfile.prod` fronts `PUBLIC_BASE_URL`. | unchanged; swap hostname to `voice.happytimeweed.com`. | `swedish-bot/Caddyfile`, `Caddyfile.prod`. |
| `Makefile` | `make` (build), `make up`, `make test`, `make lint`, `make prod-*`. | add `make provision` → `python tools/provision_vapi.py`. | `swedish-bot/Makefile`. |
| `pyproject.toml` / `uv.lock` | uv deps. Add `requests` (or `httpx`) for `vapi.py`/budtender client. Keep ruff config. | `uv add requests`; never hand-edit the lock. | `swedish-bot/pyproject.toml`, `uv.lock`. |
| `.env.example` | Full env catalog from `03-CONVENTIONS.md` §3 (Django, DB, `VAPI_*`, `HHT_*`, `HHT_TRANSFER_*`, `GEMINI_*`/`GOOGLE_*`, `EMAIL_*`/`STAFF_ALERT_*`, `SLACK_*`, `PHONE_HASH_PEPPER`). **No Dutchie keys here** (ADR-004/019). | one documented line per var. | `swedish-bot/.env.example` shape + `03-CONVENTIONS.md` §3. |

### 3.2 `core/` (services + healthz + middleware)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `core/services/gemini.py` ⎘ | **VERBATIM lift.** Vertex-preferred + API-key fallback; `generate`/`generate_stream`; `embed(text, task_type=...)` (768-dim Matryoshka, `EMBED_FALLBACKS`); `active_embedding_model()`; `health_check()`; clock-drift correction; token accounting via `constants`. | Do NOT modify. `embed(...)~L284`, `health_check()~L339`, `active_embedding_model()~L274`. | `swedish-bot/core/services/gemini.py` (whole file). |
| `core/constants.py` ⎘ | Model/pricing single source of truth: `MODELS` dict (`flash`/`embedding`/…), `EMBED_DIM=45`→**768** (it's `EMBED_DIM=768`~L45), `PRICING_PER_1K`, `UnknownModelError`, `price_per_1k`, `cost_usd`. | Lift as-is. | `swedish-bot/core/constants.py`. |
| `core/services/vapi.py` ★ | **Vapi REST client.** Base `https://api.vapi.ai`, `Authorization: Bearer ${VAPI_PRIVATE_KEY}`. Methods (STUB bodies in P0, full impl per `20-SPEC-vapi-deploy.md`): `get(path,params)`, `post(path,json)`, `patch(path,json)`, `delete(path)`; typed helpers `list_assistants()`, `get_assistant(id)`, `create_assistant(body)`, `patch_assistant(id,body)`, `find_assistant_by_name(name)`; same for `*_squad`, `*_tool`, `*_phone_number`; `auth_ok()` (cheap `GET /assistant?limit=1` for healthz). **Never** a `/workflow` path. Secrets never logged (redact `server.secret`/Bearer in any debug log). Raises `VapiError` (status + body) — fail-loud, never silent. | net-new. Pattern: a thin `requests.Session` wrapper, constant base + auth header, `raise_for_status` → `VapiError`. The full CRUD contract + retry/pagination is in `20-SPEC-vapi-deploy.md`; P0 ships the surface + working `find_*_by_name` (the idempotency primitive) + `auth_ok`. |
| `core/middleware.py` | CORS (for the P4 dashboard widget, ported) **+ the Vapi webhook HMAC verify** is NOT here — keep verification INSIDE `voice/webhooks.py` so a bad signature returns a Vapi-shaped 401 (a middleware 401 confuses Vapi's retry). Port the CORS middleware as-is; add nothing else. | `WidgetCorsMiddleware` (~L11). | `swedish-bot/core/middleware.py`. |
| `core/views.py` | `healthz(request)` extended: DB (`connection.cursor`) + Gemini (`gemini.health_check()`) **+ Vapi (`vapi.auth_ok()`)**. Returns `{status, db:{ok,error}, gemini:{...}, vapi:{ok,error}}`, 200 all-green else 503. | extend `healthz`~L27 with the `vapi` block; degrade to `vapi:{ok:false}` (not a crash) when `VAPI_PRIVATE_KEY` unset. | `swedish-bot/core/views.py` (`healthz`~L27). |
| `core/urls.py` | core routes (healthz wired in `config/urls.py`). | as-is. | `swedish-bot/core/urls.py`. |

### 3.3 `voice/` (★ NEW app — the telephony adapter + tool webhooks)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `voice/__init__.py` / `voice/apps.py` ★ | App registration (`name="voice"`). | standard. | swedish-bot app pattern. |
| `voice/urls.py` ★ | `path("vapi", webhooks.vapi_webhook)` → `/api/voice/vapi`. (P1+ add nothing here — all tools route through the ONE webhook by name.) | one route. | swedish-bot `chat/urls.py` shape. |
| `voice/webhooks.py` ★ | **THE shared contract.** `vapi_webhook(request)`: (1) `verify_signature(request)` fail-closed 401 (constant-time); (2) parse `message.type`; (3) dispatch: `assistant-request` → `handle_assistant_request`, `tool-calls` → `handle_tool_calls` (→ `TOOL_REGISTRY`), `status-update` → `handle_status_update`, `end-of-call-report` → `handle_end_of_call_report`. Each returns the §4 Vapi-shaped JSON. `@csrf_exempt` (HMAC-authed, non-cookie — ADR-019). | `verify_signature`, `vapi_webhook`, `handle_assistant_request`, `handle_tool_calls`, `handle_status_update`, `handle_end_of_call_report`. See §4 for each shape. | swedish-bot `chat/views.py` (the `@csrf_exempt` + rate-limit + parse-JSON channel-adapter shape) — "exactly the shape a Vapi webhook takes." HMAC = `hmac.compare_digest` per `crm/models.phone_hash` constant-time idiom. |
| `voice/signing.py` ★ | `compute_signature(raw_body, secret)` + `verify_signature(request)` — reads the Vapi signature header, recomputes HMAC-SHA256 over the raw body with `VAPI_WEBHOOK_SECRET`, constant-time compares, fail-closed (missing header/secret → reject). Reused by `tools/loadtest_voice.py` (P5) so the load test signs identically. | `compute_signature`, `verify_signature`. | net-new; `hmac`/`hashlib` stdlib; constant-time idiom from `crm/models.phone_hash`~L17. **Vapi's exact header name/secret scheme is pinned in `20-SPEC-vapi-deploy.md`; P0 implements against the documented `X-Vapi-Signature` / `server.secret` shared-secret first, HMAC-over-body second — both behind this one verify function.** |
| `voice/tools/__init__.py` ★ | **The registry scaffold (ADR-020).** `TOOL_REGISTRY: dict[str, Callable]`; `register(name)` decorator; `dispatch(name, args, ctx) -> dict`; unknown tool → a structured `{"error":"unknown_tool"}` (never a 500). Imports `from . import faq` so P0's handler self-registers; P1 adds `from . import suggest`, P3 `from . import vendor` — **each phase appends ONE import line; no shared-file body edits.** | `register`, `dispatch`, `TOOL_REGISTRY`. | net-new (the parallel-safe pattern roadmap §6 mandates). |
| `voice/tools/faq.py` ★ | `faq_lookup(args, ctx)` — args `{query, store?}`. Calls `kb/semantic.py::rank_faq(query, store)` (grounded retrieval) → returns `{answer, sources:[{kind,id,title}], store, grounded:true}`. **Numbers-Guard:** returns KB row text only; never composes a figure. On no match → `{answer:null, grounded:false, fallback:"I'll get a team member"}` (the assistant offers a human, never invents). | `faq_lookup`. | net-new; reads `kb/` via `semantic.py`. |
| `voice/guardrails.py` ★ | Code-owned safety (version-controlled, NOT UI-editable — ADR-014): `assert_no_leak(payload)` (no `"cost"`/`"margin"` substring — the defensive Leak-Guard), `age_gate_required(ctx)`, `in_scope(intent)`. P0 wires `assert_no_leak` into the tool-response path. | `assert_no_leak`, `age_gate_required`, `in_scope`. | swedish-bot `chat/guardrails.py` (the pattern: code-owned, fail-closed). |
| `voice/summarize.py` ★ | `summarize_call(voice_call) -> str` — a short call summary via `core/services/gemini.py` (`MODELS["flash"]`, low temp). Called inline by the eocr handler in P0 (moved to Celery in P5 — gated). | `summarize_call`. | net-new; uses `gemini.generate`. |
| `voice/models.py` ★ | **The durable call log.** See §4.6 for full field shapes. `Outcome` = `TextChoices` (`faq_answered`/`suggested`/`escalation`/`vendor_callback`/`abandoned`/`error`). `VoiceCall` (one per call, keyed on Vapi `call_id`, idempotent). `VoiceTurn` (one per turn; `latency_ms` field stamped by the webhook so P5's p95 is computable from durable rows). | `Outcome`, `VoiceCall`, `VoiceTurn`. | net-new; mirrors swedish-bot `crm/models.Session`/`ServiceRequest` durability + idempotency idioms. |

### 3.4 `kb/` (fork + seed — the FAQ knowledge plane)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `kb/models.py` | KB models, **slimmed to the voice domain.** Keep+fork: `AgentPrompt`(~L226 — role/body/model_id + the P4 voice fields per `14-…` §4.1, added now so no later migration churn), `FlowConfig`(~L276 singleton JSON graph), `FAQEntry`(~L150), `PolicyDocument`(~L312). Drop HVAC models (`Machine`/`Vendor`/`MachineDocument`/…). **Net-new voice models:** `StoreFact` (store/key/value, `kind∈{hours,phone,address,email,payment,pickup,special,limit}`, `confirmed` bool for O-8), `EducationDoc` (slug/title/body/source_url/`topic`), `BlogDoc` (slug/title/body/source_url), `WeightTypeTaxonomy` (the weights/types reference rows — `axis∈{weight,edible_dose,cart_size,preroll,concentrate_subtype,strain_type,ratio}`, `term`, `value`, `notes`). Each KB text model carries `weight` (int, retrieval priority) + `kind`/`topic` (the taxonomy). | model classes above + a shared `kb_text()` accessor. | `swedish-bot/kb/models.py` (`AgentPrompt`~L226, `FlowConfig`~L276, `FAQEntry`~L150, `PolicyDocument`~L312, `SiteFAQ`~L368 as the `StoreFact` shape inspiration). |
| `kb/ingest.py` | Idempotent text/PDF ingest (sha256 dedup, magic-byte + size cap) for any future education-PDF upload. Used by `seed.py` for any file-sourced rows; the dashboard upload (P4) reuses it. | `parse_pdf_text`~L24, `ingest_pdf_bytes`~L39 (`MAX_PDF_BYTES`~L15, sha256~L58). | `swedish-bot/kb/ingest.py` (whole file). |
| `kb/semantic.py` | **EMBEDDINGS pipeline (gap G-6).** Gemini 768-dim Matryoshka + **cached in-memory cosine**, content-hash keyed so it self-invalidates on edit. New voice entrypoints: `rank_faq(query, store=None, top_k=3) -> list[(row, cosine)]` over `FAQEntry`+`StoreFact`+`PolicyDocument`+`EducationDoc`+`WeightTypeTaxonomy` (store-scoped where the row is store-specific); `reindex() -> int` (rebuild the cache, return chunk count). Documents the **pgvector swap-seam** ("swap past a few thousand rows") in a module docstring (ADR-013). | `rank_faq`, `reindex`, `_corpus_vectors` (content-hash cache), `_cos`. | `swedish-bot/kb/semantic.py` (whole file: `_corpus_vectors`~L52 content-hash cache, `rank_guides`~L103 → adapt to `rank_faq`, `_cos`~L44, `RETRIEVAL_DOCUMENT`/`RETRIEVAL_QUERY` task-type split). |
| `kb/seed.py` ★ | **The seed source of truth (gap G-7).** Idempotent (`get_or_create` by natural key) — maps EACH research/brief section to a concrete model row. See §4.7 for the full row map. `seed_all()` runs every block; `manage.py seed_kb` calls it. | `seed_faq()`, `seed_store_facts()`, `seed_return_policy()`, `seed_wa_limits()`, `seed_weights_types()`, `seed_education()`, `seed_blogs()`, `seed_agent_prompts()`, `seed_all()`. | net-new; rows authored from `_research-education-blogs.md` + the synthesis brief §2 (FAQ/return-policy/store-facts). |
| `kb/vapi_files.py` ★ | **Mirror KB → Vapi Files + a Query Tool** (the low-latency grounded fallback, path 1). `mirror_all() -> {files, tool_id}` renders the curated KB into ≤300KB markdown files (FAQ.md / return-policy.md / store-facts.md / wa-law.md / weights-types.md / education.md), uploads via `core/services/vapi.py` Files CRUD, attaches/updates a Vapi **Query Tool** on the `entry_faq` assistant. Idempotent (re-upload replaces by name). Degrades to "Vapi mirror skipped (not configured)" when `VAPI_PRIVATE_KEY` unset. | `mirror_all`, `_render_file(kind)`, `ensure_query_tool()`. | net-new; uses `vapi.py` Files endpoints (detailed in `20-SPEC-vapi-deploy.md`). |
| `kb/management/commands/seed_kb.py` ★ | `python manage.py seed_kb` → `kb.seed.seed_all()`; `--reindex` also calls `semantic.reindex()` + `vapi_files.mirror_all()`. | `handle`. | swedish-bot `kb/management/commands/*` pattern. |

### 3.5 `crm/` (phone-hash + sinks — the durable-record plumbing)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `crm/models.py` | `phone_hash(phone)` (peppered SHA-256, `PHONE_HASH_PEPPER` ≠ `SECRET_KEY`) — the returning-caller key (used by P1; P0 stores `caller_phone_hash` on `VoiceCall`, raw number NEVER persisted). Keep a minimal `Caller`/`CallSession` shell; `VendorCallback` is P3. | `phone_hash`~L17. | `swedish-bot/crm/models.py` (`phone_hash`~L17, `Customer.phone_hash`~L37, `save`~L58). |
| `crm/sinks.py` | `EmailSink` + `dispatch(record)` adapted for `VoiceCall` (was `service_request`). P0 wires the eocr handler → `dispatch` → email to `STAFF_ALERT_EMAIL` (per-call digest). `DBSink` is the durable `VoiceCall` write (already done synchronously in the handler — sink is idempotent per `(call_id, sink)`). Slack behind `SLACK_ALERTS_ENABLED` (O-9, default off). The immediate-alert escalation/vendor routing is P2/P3. | `EmailSink`~L40, `dispatch`~L119 (idempotent per `(record,sink)`~L127). | `swedish-bot/crm/sinks.py` (`EmailSink`~L40, `dispatch`~L119; drop `WordPressOffertSink`). |
| `crm/profile.py` | (P1 returning-caller helper) — port the shell now, wire in P1. | as-is shell. | `swedish-bot/crm/profile.py`. |

### 3.6 `tools/` (the provisioner)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `tools/provision_vapi.py` ★ | **Idempotent, re-runnable provisioner.** Ensures: the `faq_lookup` Tool (server.url + secret), the `entry_faq` Assistant (Koptza prompt, sonic-3 voice, nova-3 + keyterms, toolIds=[faq_lookup id], server block), the ONE Squad ("Happy Time Voice", member=[entry_faq]), and (if `VAPI_PHONE_NUMBER_ID` set) the phone-number → Squad attach. **GET-then-PATCH / find-by-name-then-POST-once**; writes each `assistantId`/`squadId`/`toolId`/`phoneNumberId` back onto local rows (`AgentPrompt.vapi_assistant_id`, a `VapiObject` map, `settings`/env note). `--dry-run` prints the planned calls. A 2nd run = zero new objects (all PATCH/no-op). | `ensure_tool`, `ensure_assistant`, `ensure_squad`, `ensure_phone_number`, `main(dry_run)`. The deep payload shapes (§4.3/§4.4 of `14-…` for assistant/squad) live in §6 here + `20-SPEC-vapi-deploy.md`. | net-new; uses `core/services/vapi.py`. The assistant/squad payload builders are SHARED with P4's `dashboard/publish.py::build_assistant_payload` (one shape, two callers — `14-…` §9 risk). |

### 3.7 Templates / static (minimal in P0)

- `templates/dashboard/base.html` — ported shell (nav stub, toast stack, HTMX, vendored Alpine, neutral theme) so P4 forks it, not P0. P0 ships only a `healthz`-style status page + the admin. **Full dashboard is P4.**
- `static/` — vendored Alpine (`alpine.min.js`), Tailwind output (committed). Port as-is from swedish-bot.

---

## 4. Data contracts / JSON schemas (THE shared contract every later phase consumes)

This is the load-bearing section. The webhook event shapes, the tool-call envelope, and the `VoiceCall` model are frozen here so P1–P3 can fork against them.

### 4.1 Inbound webhook envelope (Vapi → `POST /api/voice/vapi`)

Every Vapi server message arrives as `{"message": {...}}`. P0 dispatches on `message.type`:

```json
{ "message": {
    "type": "assistant-request | tool-calls | status-update | end-of-call-report",
    "call": { "id": "vapi-call-uuid", "customer": { "number": "+1509…" }, "assistantId": "asst_…" },
    "timestamp": 1718900000000
}}
```

**Signature:** Vapi sends the shared `server.secret` (header `X-Vapi-Secret`) and/or an HMAC header `X-Vapi-Signature` over the raw body. `voice/signing.verify_signature` checks **both modes fail-closed**: if a configured `VAPI_WEBHOOK_SECRET` exists, at least one valid proof is required; a missing/wrong header → **401, no handler runs** (the exact header scheme is pinned in `20-SPEC-vapi-deploy.md`; P0 implements the documented secret-header first, HMAC-over-body second).

### 4.2 `assistant-request` (Vapi asks which assistant/overrides to use for an inbound call)

P0 returns the Squad-fronting assistant + hydrated variables (fixes export #11 unhydrated `{{store_name}}`):

**Response:**
```json
{ "assistantId": "<entry_faq id>",
  "assistantOverrides": {
    "variableValues": { "store_name": "Happy Time Yakima",
                        "store_hours": "9 AM–11 PM daily",
                        "transfer_number": "${HHT_TRANSFER_NUMBER_YAKIMA or placeholder}" } } }
```
(Store is inferred from the inbound number if one-per-store; for the single-number P0 default it uses `HHT_DEFAULT_STORE=yakima`. O-4.)

### 4.3 `tool-calls` (the assistant invoked `faq_lookup`)

**Request `message`:**
```json
{ "type": "tool-calls",
  "toolCalls": [ { "id": "call_abc", "function": { "name": "faq_lookup",
                   "arguments": { "query": "what time do you close", "store": "yakima" } } } ],
  "call": { "id": "vapi-call-uuid", "customer": { "number": "+1509…" } } }
```

**Response (Vapi tool-result envelope — the frozen shape):**
```json
{ "results": [ { "toolCallId": "call_abc",
                 "result": { "answer": "Our Yakima store is open until 11 PM tonight.",
                             "grounded": true,
                             "sources": [ { "kind": "store_fact", "id": 12, "title": "Yakima hours" } ],
                             "store": "yakima" } } ] }
```
- `dispatch(name, args, ctx)` routes by `function.name` through `TOOL_REGISTRY`. Unknown name → `{"result":{"error":"unknown_tool"}}` (never a 500).
- **Leak-Guard:** `guardrails.assert_no_leak(result)` runs before return — any `"cost"`/`"margin"` substring raises (P0 guards the path before P1 products exist).
- **Numbers-Guard:** `faq_lookup` returns KB row text verbatim-ish; `grounded:false` when no KB row matches → the assistant offers a human, never invents.

### 4.4 `status-update` (in-flight call state; P0 records turns)

**Request `message`:** `{ "type":"status-update", "status":"in-progress|forwarding|ended", "call":{...}, "transcript?":"…", "role?":"user|assistant" }`
**Response:** `200 {}` (ack). P0 appends a `VoiceTurn` (role, text, `latency_ms` = server-side handler time) when a transcript fragment is present; the live-monitor (P4) reads these.

### 4.5 `end-of-call-report` (the durable record + email — ADR-017)

**Request `message`:**
```json
{ "type": "end-of-call-report",
  "call": { "id": "vapi-call-uuid", "customer": { "number": "+1509…" } },
  "endedReason": "customer-ended-call",
  "durationSeconds": 73,
  "transcript": "full transcript text…",
  "messages": [ { "role":"user|assistant|tool", "message":"…", "time": 1718… } ],
  "summary": "Vapi's own summary (optional)" }
```

**Handler (`handle_end_of_call_report`) — order is binding:**
1. **Synchronous durable write** (idempotent on `call.id`): upsert `VoiceCall` (transcript, duration, `outcome` classified from the transcript/messages — P0 outcomes are `faq_answered` / `abandoned` / `error`), append remaining `VoiceTurn` rows, store `caller_phone_hash = phone_hash(customer.number)` (raw number NEVER persisted — PII discipline). **The record is never lost** even if the next steps fail.
2. `voice/summarize.summarize_call(voice_call)` (inline in P0; Celery in P5) → write `VoiceCall.ai_summary`.
3. `crm/sinks.dispatch(voice_call)` → `EmailSink` per-call digest to `STAFF_ALERT_EMAIL` (degrades to a logged no-op with no SMTP). Slack behind `SLACK_ALERTS_ENABLED`.
4. Return `200 {}` to Vapi.

**Response:** `200 {}`.

### 4.6 `voice/models.py` (the durable-log shapes — frozen for P2/P3/P4)

```python
class Outcome(models.TextChoices):
    FAQ_ANSWERED   = "faq_answered"
    SUGGESTED      = "suggested"        # set by P1
    ESCALATION     = "escalation"       # set by P2
    VENDOR_CALLBACK= "vendor_callback"  # set by P3
    ABANDONED      = "abandoned"
    ERROR          = "error"

class VoiceCall(models.Model):
    call_id        = CharField(max_length=64, unique=True, db_index=True)  # Vapi call.id — idempotency key
    store          = CharField(max_length=32, blank=True)                  # yakima|mount-vernon|pullman
    caller_phone_hash = CharField(max_length=64, blank=True, db_index=True)# peppered; raw number NEVER stored
    outcome        = CharField(max_length=32, choices=Outcome.choices, blank=True)
    escalated      = BooleanField(default=False)                           # P2 sets
    reason         = CharField(max_length=64, blank=True)                  # defective_return|repeated_human|dispute|vendor|…
    duration_s     = IntegerField(null=True, blank=True)
    transcript     = TextField(blank=True)
    ai_summary     = TextField(blank=True)
    assistant_id   = CharField(max_length=64, blank=True)
    suggested_skus = JSONField(default=list)                               # P1 appends
    created_at     = DateTimeField(auto_now_add=True)
    updated_at     = DateTimeField(auto_now=True)

class VoiceTurn(models.Model):
    call       = ForeignKey(VoiceCall, related_name="turns", on_delete=CASCADE)
    seq        = IntegerField()
    role       = CharField(max_length=16)        # user|assistant|tool
    text       = TextField(blank=True)
    tool_name  = CharField(max_length=64, blank=True)
    latency_ms = IntegerField(null=True, blank=True)  # server-side handler time (P5 p95 reads this)
    created_at = DateTimeField(auto_now_add=True)
    class Meta: unique_together = [("call", "seq")]   # idempotent re-delivery
```

### 4.7 `kb/seed.py` — the row map (gap G-7: every research/brief section → a concrete row)

Idempotent `get_or_create` by natural key. **This is the seed source of truth.**

**FAQ (`FAQEntry`)** — from synthesis brief §2 FAQ + `_research-education-blogs.md`:
| Q | A (grounded — Numbers-Guard) |
|---|---|
| "Do I need to be 21?" | "Yes — 21+ with a valid government photo ID for recreational purchase." (`StoreFact kind=limit`) |
| "Do you take cards / how do I pay?" | "Cash and debit only, and there's an on-site ATM." |
| "Do you deliver?" | "No delivery — it's pickup only (WA law). Order online and pick up; usually ready in ~15 minutes." |
| "What are the purchase limits?" | "Per visit: 1 ounce of flower, 7 grams of concentrate, 16 oz solid edibles, 72 oz liquid edibles." |
| "Can I return a product?" | "All sales are final, but WA law (WAC 314-55-079) allows a defective-product exchange — bring the original packaging with a legible lot ID and your receipt; a team member handles it." |
| "What are this week's specials?" | weekly specials rows (below). |

**Return policy (`PolicyDocument`)** — ONE row, `kind="return_policy"`, body = "all sales final + WAC 314-55-079 defective exception (no time limit; original packaging + legible lot ID + receipt; escalate disputes to a human)." This is the **WAC-314-55-079 return-policy row** the prompt cited.

**Store facts (`StoreFact`) — the 3 stores:**
| store | kind | value |
|---|---|---|
| yakima | address/phone/hours/email | 1315 N 1st St, Yakima WA 98901 / (509) 571-1106 / 9 AM–11 PM daily / happytimeyak509@gmail.com |
| mount-vernon | address/phone | 200 Suzanne Ln / (360) 488-2923 ; **hours = `confirmed=False` "call to confirm"** (O-8) |
| pullman | address/phone/hours | 5602 WA-270 / (509) 334-2788 / hours (seed if known, else confirm stub) |
| (all) | payment/pickup/email | cash+debit+ATM / pickup-only ~15 min / happytimeyak509@gmail.com |
| (all) | special | Flower Monday 30% · Cyber Tuesday 30% online · Wax Wednesday 25% · Self-Care Thursday 25% · Happy Friday 30% online |

**WA limits (`StoreFact kind=limit` / `WeightTypeTaxonomy axis=…`)** — 1 oz flower / 7 g concentrate / 16 oz solid edibles / 72 oz liquid edibles (`_research-education-blogs.md` §10).

**Weights/types taxonomy (`WeightTypeTaxonomy`) — the FULL table (`_research-education-blogs.md` §9):**
- `axis=weight`: half-gram 0.5g, gram 1g, 2g, eighth 3.5g, 4g, quarter 7g, 8g, 10g, half-ounce 14g, ounce 28g (cap).
- `axis=cart_size`: 0.5g, 1g.
- `axis=preroll`: single, 5-pack, 10-pack (per-joint 0.5g/1g).
- `axis=edible_dose`: microdose 1–2.5mg, beginner 2.5mg, standard 5/10mg, WA max pack 10×10mg=100mg, onset 30–90min (beverages 15–30min), peak ≈3h, "wait 2h before re-dosing."
- `axis=concentrate_subtype`: rosin/live rosin (solventless), live/cured resin, RSO/FECO, distillate, diamonds, sauce, badder/budder, shatter, crumble, sugar, wax, bubble hash, kief.
- `axis=strain_type`: indica/sativa/hybrid (label is a general classification; terpene + physiology shape the experience — house rule, never over-promise "indica=couch-lock").
- `axis=ratio`: THC:CBD 1:1 / 2:1 / 5:1 / 20:1; CBN = sleepy minor.

**Education (`EducationDoc`)** — one row per confirmed URL (`_research-education-blogs.md` Provenance table), body = the distilled `[SITE]` content (edibles dosing §2, microdosing §3, THC vs CBD §4, strain types §5, concentrates/vapes §6, storage §7), `source_url` set, marked `provisional` until the Vercel wall lifts for verbatim copy (`_research-education-blogs.md` §11 TODO 1).

**Blogs (`BlogDoc`)** — `how-to-use-disposable-vape`, `best-dispensary-yakima-wa`, `recreational-marijuana-yakima-wa` (slug/title/source_url; body distilled).

**Agent prompts (`AgentPrompt`)** — one `entry_faq` row, `role="faq"`, body = the Koptza persona + the FAQ system prompt (warm/family/no-pressure/conservative-on-dosing — `_research-education-blogs.md` §8 house style; ADR-018 spoken 21+ confirm, NO "peek at ID"; Numbers-Guard "answer only from `faq_lookup`"). `vapi_model="gpt-4.1-mini"`, `voice_id=a3520a8f-…`, `tool_names=["faq_lookup"]`.

---

## 5. KB grounding pipeline (gap G-6 detail — file-tasks + acceptance)

`kb/semantic.py` is the embeddings engine the FAQ answer is grounded on. Lifted from swedish-bot and retargeted to the voice KB.

**Pipeline (per `faq_lookup` call):**
1. `rank_faq(query, store, top_k=3)` builds the corpus = `FAQEntry` + store-scoped `StoreFact` + `PolicyDocument` + `EducationDoc` + `WeightTypeTaxonomy` rows (each row → one `(id, text)` chunk; store-specific rows filtered to the caller's store).
2. `_corpus_vectors(prefix, items)` embeds the chunks with `gemini.embed(texts, task_type="RETRIEVAL_DOCUMENT")` (768-dim Matryoshka), **cached in the Django cache keyed by `{prefix}:{embedding_model}:{EMBED_DIM}:{content_sha}`** — self-invalidates the instant any KB row text changes (swedish-bot `_corpus_vectors`~L52).
3. The query is embedded `task_type="RETRIEVAL_QUERY"`; in-memory cosine (`_cos`~L44) ranks chunks; top-`k` returned with scores.
4. The `faq.py` handler assembles `answer` from the top chunk(s) (KB text, never composed numbers) + `sources`.
5. **Degrade-safe:** any embedding error / no Gemini auth → fall back to a deterministic keyword/substring match over the same KB rows (still grounded, lower recall) — never break the answer (swedish-bot "embedding error → empty result, caller keeps the trigram answer" pattern, `semantic.py` docstring).

**pgvector swap-seam (ADR-013):** documented in the `kb/semantic.py` module docstring — "the cached-cosine corpus is the seam; past a few thousand rows, swap `_corpus_vectors` for a pgvector ANN query, same `rank_faq` signature." Not built in P0.

**Acceptance (G-6, restated in §7 C):** a KB edit changes the content-hash cache key (re-embed on next call, no redeploy); `rank_faq("what time do you close","yakima")` returns the Yakima-hours `StoreFact` as top chunk; with Gemini unavailable the keyword fallback still returns it; `reindex()` returns the chunk count and `vapi_files.mirror_all()` re-mirrors.

---

## 6. Vapi deploy steps (P0 provisioning — idempotent, ADR-003)

`tools/provision_vapi.py` (run via `make provision` or `python tools/provision_vapi.py [--dry-run]`). **GET/find-by-name-then-PATCH; never blind POST twice.** The deep CRUD/pagination/retry contract is in `20-SPEC-vapi-deploy.md`; this is the P0 call sequence.

1. **`ensure_tool("faq_lookup")`** — find tool by name; if absent `POST /tool` with:
```json
{ "type": "function",
  "function": { "name": "faq_lookup",
    "description": "Answer hours/specials/returns/payment/pickup/limits/weights-types from the knowledge base.",
    "parameters": { "type":"object",
      "properties": { "query": {"type":"string"}, "store": {"type":"string","enum":["yakima","mount-vernon","pullman"]} },
      "required": ["query"] } },
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" } }
```
   Write the returned `toolId` to the local tool-id map.

2. **`ensure_assistant("entry_faq")`** — find by name; if absent `POST /assistant`, else `PATCH /assistant/{id}`, with the payload (voice/transcriber/model set ONCE — ADR-011):
```json
{ "name": "entry_faq",
  "model": { "provider":"openai", "model":"gpt-4.1-mini", "temperature":0.3, "maxTokens":250,
             "messages":[{"role":"system","content":"<AgentPrompt.body, vars hydrated>"}],
             "toolIds":["<faq_lookup id>"] },
  "voice": { "provider":"cartesia", "voiceId":"a3520a8f-226a-428d-9fcd-b0a4711a6829", "model":"sonic-3",
             "experimentalControls": { "emotion":["positivity:highest"] } },
  "transcriber": { "provider":"deepgram", "model":"nova-3",
                   "keyterms": ["flower","dabs","wax","shatter","resin","carts","510","disposable","gummies","tincture", "…33 terms (voice/constants.DEEPGRAM_KEYTERMS)…"] },
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" },
  "firstMessage": "Happy Time, this is Koptza! Are you 21 or older?" }
```
   Write `assistantId` → `AgentPrompt.vapi_assistant_id`. **`DEEPGRAM_KEYTERMS` is ONE shared constant in `voice/constants.py`** — a unit test asserts the transcriber block + keyterm list appears exactly once in the payload (ADR-011, no per-node dup).

3. **`ensure_squad("Happy Time Voice")`** — find by name; if absent `POST /squad`, else `PATCH /squad/{id}`:
```json
{ "name": "Happy Time Voice",
  "members": [ { "assistantId":"<entry_faq id>", "assistantDestinations": [] } ] }
```
   (P1–P3 add members + destinations; P0 ships the single-member container so the shape is proven.) Write `squadId` → settings/env note.

4. **`ensure_phone_number()`** — if `VAPI_PHONE_NUMBER_ID` set, `PATCH /phone-number/{id}` to point at the Squad (`squadId`) + per-store `assistantOverrides`. If unset (O-4) → **skip with a logged "phone id not configured; attach later"** (does NOT block the rest).

5. **`kb/vapi_files.mirror_all()`** — render the KB → ≤300KB files, upload via Files CRUD, attach a Query Tool to `entry_faq` (the low-latency grounded fallback). Skips cleanly when `VAPI_PRIVATE_KEY` unset.

6. **Idempotency proof:** re-run → every `ensure_*` finds the object by name and PATCHes (or no-ops on identical payload) → **zero new Vapi objects** (acceptance G-2, §7).

---

## 7. Acceptance criteria (testable, concrete — lettered)

**A. Chassis + boot**
- A1. `make` builds the image; `docker compose up` brings up `web` + `db` + `caddy`; `GET /healthz` returns **200** with keys `db.ok`, `gemini.ready`, `vapi.ok` all true (with creds) — and **503** with the offending key false when a dependency is down. (Paste the JSON.)
- A2. **Prod-fail-closed:** with `DJANGO_DEBUG=0` and a default `DJANGO_SECRET_KEY` → boot raises `ImproperlyConfigured`. With `PHONE_HASH_PEPPER == DJANGO_SECRET_KEY` → raises. With `VAPI_PRIVATE_KEY`/`VAPI_WEBHOOK_SECRET` unset → raises. With all set → boots. (Unit test on the settings guard.)
- A3. `core/services/gemini.py` is byte-identical to swedish-bot's (a checksum/diff test) — verbatim lift (ADR-001).

**B. Webhook contract (THE shared contract — the fork gate)**
- B1. **HMAC fail-closed:** a POST to `/api/voice/vapi` with a missing/bad `VAPI_WEBHOOK_SECRET` proof → **401, no handler invoked** (assert no `VoiceCall`/`VoiceTurn` written, no tool dispatched). A valid proof → handler runs. Constant-time compare (`hmac.compare_digest`). **(Non-negotiable gate.)**
- B2. `assistant-request` → returns `{assistantId, assistantOverrides.variableValues}` with `store_name`/hours/transfer hydrated (no literal `{{store_name}}` — export #11 fixed). (Unit test on the handler.)
- B3. `tool-calls` for `faq_lookup` → returns the §4.3 envelope `{results:[{toolCallId, result:{answer, grounded, sources, store}}]}`; `dispatch` routes by `function.name`; an unknown tool name → `{result:{error:"unknown_tool"}}` (never 500).
- B4. `status-update` with a transcript fragment → appends a `VoiceTurn` (role, text, `latency_ms` stamped); returns `200 {}`.
- B5. `end-of-call-report` → (1) writes a `VoiceCall` (transcript, duration, outcome, `caller_phone_hash`, **raw number absent** from the DB), (2) writes `ai_summary`, (3) calls `crm/sinks.dispatch` (email or logged no-op), (4) returns `200 {}`. **Re-delivering the same `call.id` does NOT duplicate the `VoiceCall`** (idempotent on `call_id`) and does NOT re-send the email (idempotent per `(call_id,sink)`).

**C. KB grounding (gap G-6)**
- C1. `rank_faq("what time do you close","yakima")` returns the Yakima-hours `StoreFact` as the top chunk; `rank_faq("can I return a dead vape")` returns the WAC-314-55-079 `PolicyDocument` row.
- C2. Editing a KB row's text changes the `_corpus_vectors` content-hash cache key → the next `faq_lookup` reflects the edit **with no redeploy** (assert the cache key differs + the answer changes).
- C3. With Gemini auth unavailable (mock `embed` to raise), `rank_faq` falls back to the deterministic keyword match and STILL returns the correct row (degrade-safe).
- C4. `semantic.reindex()` returns the chunk count; `vapi_files.mirror_all()` returns `{files, tool_id}` or, with `VAPI_PRIVATE_KEY` unset, `{skipped:"not configured"}`.

**D. Seed (gap G-7)**
- D1. `manage.py seed_kb` is **idempotent** (run twice → no duplicate rows; `get_or_create` natural keys).
- D2. After seeding, every §4.7 row exists: the 6 FAQ Q&As, the WAC-314-55-079 `PolicyDocument`, the 3 store-facts (Yakima/Mt Vernon-stub/Pullman), the WA-limit rows, the FULL weights/types `WeightTypeTaxonomy` (every axis), the education + blog rows, and the `entry_faq` `AgentPrompt`. (A parametrized existence test.)
- D3. Mt Vernon hours seed with `confirmed=False` ("call to confirm") — O-8 honored; Yakima/Pullman hours real.

**E. Numbers-Guard + Leak-Guard**
- E1. `faq_lookup` returns only KB row text; a query with no KB match → `grounded:false` + a human-offer, **never an invented number** (assert no figure not present in any KB row appears in the answer for a fabricated-fact query).
- E2. **Leak-Guard:** no `faq_lookup` response contains a `"cost"`/`"margin"` substring (`guardrails.assert_no_leak` test) — guards the surface before P1 products. **(Non-negotiable gate.)**

**F. Provisioning (ADR-003)**
- F1. `provision_vapi.py` (mocked `vapi.py`) creates exactly: 1 tool + 1 assistant + 1 squad (+ phone-attach iff `VAPI_PHONE_NUMBER_ID` set); writes `assistantId`/`squadId`/`toolId` to local rows.
- F2. **Zero drift:** an immediate second run issues **zero** `create_*` calls (all `find_*_by_name` hits → PATCH or no-op); assert the mock `post` call count == 0 on the second run.
- F3. The assistant payload sets voice/transcriber/model **once**; the `DEEPGRAM_KEYTERMS` list appears **exactly once** (ADR-011 no-dup test). No `/workflow` path is ever called (assert the mock saw no `/workflow`).
- F4. With `VAPI_PRIVATE_KEY` unset, `--dry-run` prints the planned calls and the live run logs "vapi not configured" without crashing.

**G. The deliverable (definition of done — a real grounded call)**
- G1. A real inbound call to the provisioned number: greeted "Happy Time, this is Koptza! Are you 21 or older?" → caller asks **hours / specials / returns / payment / a weight** → the agent answers **from KB content** (Yakima/Mt Vernon/Pullman facts; cash/debit+ATM; pickup-only ~15 min; WAC-314-55-079 defective exception; the correct weight/limit) — **no hallucinated facts**, no "peek at your ID" (ADR-018), no literal `{{store_name}}`.
- G2. A `VoiceCall` row is written for the call (transcript + outcome=`faq_answered` + `caller_phone_hash`, raw number absent) with `ai_summary`; a staff email (or logged no-op) fired.
- G3. Editing the answer to one FAQ in the KB and placing the next call reflects the change **with no redeploy** (C2 proven on a live call).

**H. Hygiene**
- H1. `ruff check` + `ruff format --check` clean; `python manage.py check` clean; `makemigrations --check` exit 0 (the `voice`/`kb`/`crm` migrations committed); targeted `pytest` green. **Paste all four outputs** (`03-CONVENTIONS.md` §1.3 — never claim passing without pasted output).

---

## 8. Test plan

Mirrors the four planes in `03-CONVENTIONS.md` §5. P0 touches the webhook + a tool path + serialized output → the **Leak-Guard** and **HMAC-fail-closed** tests are mandatory gates.

### 8.1 Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network)
- `tests/test_settings_failclosed.py` — A2: the `if not DEBUG` guard raises on default secret / pepper==secret / missing Vapi secrets; boots when all set.
- `tests/test_signing.py` — B1: `verify_signature` accepts a valid secret/HMAC, rejects missing/wrong (constant-time path exercised). The load-test signer (P5) uses the same helper.
- `tests/test_webhook_dispatch.py` — B2/B3/B4: each event kind routes to its handler and returns the §4 shape; unknown tool → `unknown_tool`; `assistant-request` hydrates vars.
- `tests/test_models_idempotent.py` — B5: re-delivering an `end-of-call-report` with the same `call_id` upserts (no dup `VoiceCall`); `VoiceTurn` `unique_together(call,seq)`.
- `tests/test_semantic_faq.py` — C1/C2/C3: `rank_faq` top-chunk correctness; content-hash cache invalidation on edit; Gemini-down keyword fallback.
- `tests/test_seed.py` — D1/D2/D3: idempotent seed; every §4.7 row exists; Mt Vernon hours `confirmed=False`.
- `tests/test_guardrails.py` — E1/E2: Numbers-Guard (no invented figure on a fabricated-fact query); `assert_no_leak` rejects a `"cost"`/`"margin"` substring.
- `tests/test_provision_payload.py` — F3: the assistant payload sets voice/transcriber/model once; `DEEPGRAM_KEYTERMS` appears exactly once; no `/workflow` path.
- `tests/test_healthz.py` — A1: 200 all-green, 503 with a stubbed-down dependency.

### 8.2 Contract (`pytest -m integration`, Vapi client mocked, Gemini stubbed/recorded)
- `tests/test_leak_guard_p0.py` (**mandatory**) — no `"cost"`/`"margin"` substring in any `faq_lookup` response (ADR-008 / E2).
- `tests/test_hmac_fail_closed_p0.py` (**mandatory**) — bad/missing signature → 401 before any handler; valid → passes (B1 / ADR-019).
- `tests/test_provision_idempotent.py` — F1/F2: first run creates 1 tool+1 assistant+1 squad; second run issues 0 `create_*` calls (zero drift). Per-object error isolation (a Vapi 4xx on the tool doesn't abort the assistant) → fail-loud per object.
- `tests/test_eocr_durable.py` — B5/G2: the `VoiceCall` write happens even when `summarize`/`EmailSink` raise (durable record never lost — ADR-017); raw number absent from the DB.
- `tests/test_vapi_files_mirror.py` — C4: `mirror_all` against a mocked Files API; degrades to `{skipped}` with no key.

### 8.3 Provisioning (`python tools/provision_vapi.py --dry-run` then live against a sandbox key)
- Dry-run prints the planned tool/assistant/squad/phone calls. Live run (sandbox `VAPI_PRIVATE_KEY`) creates them; the GET-back confirms the assistant has the Koptza prompt + sonic-3 voice + nova-3 keyterms + the `faq_lookup` toolId. A re-run is drift-free (paste the PATCH-only / no-new-object output).

### 8.4 Manual call script (the phase's definition of done — paste evidence)
Dial `VAPI_PHONE_NUMBER_ID` (O-4 placeholder; use the provisioned test number) and run, pasting transcript + the resulting `VoiceCall` row:
1. **Greeting/age:** confirm "Happy Time, this is Koptza! Are you 21 or older?" — no "peek at your ID" (ADR-018), no literal `{{store_name}}` (export #11). (G1)
2. **Hours:** "what time do you close in Yakima?" → the seeded Yakima hours, spoken from KB. (G1)
3. **Payment/pickup:** "do you take cards / do you deliver?" → cash+debit+ATM / pickup-only ~15 min. (G1)
4. **Returns:** "can I return a vape cart that died?" → the WAC-314-55-079 defective exception (original packaging + legible lot ID + receipt; a human handles disputes). (G1)
5. **A weight/limit:** "how many grams in an eighth / what's the flower limit?" → 3.5g / 1 oz, from the taxonomy. (G1)
6. **Durable log:** confirm the `VoiceCall` row (transcript + outcome + `caller_phone_hash`, no raw number) + `ai_summary` + the staff email landed. (G2)
7. **Live edit:** change one FAQ answer in the admin/KB, place the call again → the new answer is spoken, no redeploy. (G3)

**Test-data discipline:** deterministic fixtures; expected values hand-authored, not generated by the code under test. The Leak-Guard and HMAC tests are non-negotiable gates. Coverage: ~90% diff coverage on the new `voice/`, `kb/semantic.py rank_faq`, `kb/seed.py`, `core/services/vapi.py`.

---

## 9. Risks / open questions

| Risk / question | Impact | Mitigation / owner item |
|---|---|---|
| **Vapi webhook signature scheme** (header name, secret vs HMAC-over-body) may differ from the documented shape at build time. | The fail-closed gate could reject valid calls or accept bad ones. | All verification is in ONE `voice/signing.verify_signature`; implement the documented `server.secret` header first + HMAC-over-body second (both behind the one function). The exact scheme is pinned in `20-SPEC-vapi-deploy.md`; a contract test fixes whatever lands. The Vapi `server.secret` is set in the provision payload so both ends share it. |
| **Vapi tool-result envelope shape** (`results:[{toolCallId,result}]`) is the contract P1–P3 fork against. | A late shape change breaks 3 worktrees. | Frozen in §4.3 + a contract test; the fork gate (§0) holds P1–P3 until this is merged + green. P1's `suggest_products` reuses the SAME envelope. |
| **`core/services/vapi.py` deep CRUD** (pagination, retries, Files API) is only stubbed in P0. | A live provision could hit an un-stubbed edge. | P0 implements the idempotency primitive (`find_*_by_name`) + `auth_ok` fully; the rest is specced in `20-SPEC-vapi-deploy.md` and exercised by the provisioning test. The assistant/squad payload builder is SHARED with P4 publish (one shape, two callers). |
| **Gemini/Vertex auth** may be absent in the build env (the marketing_dashboard had a 403'd Generative Language API). | `kb/semantic.py` cosine disabled. | `embed` degrades gracefully; `rank_faq` falls back to deterministic keyword match (still grounded); `healthz` reports `gemini: not ready`. The FAQ answer is correct either way (C3). |
| **Mt Vernon hours conflict (O-8).** | Wrong hours spoken. | Seed `confirmed=False` "call to confirm" stub; never speak a guessed Mt Vernon close time (D3). Owner confirms → flip `confirmed=True` with the real value. |
| **Transfer / inbound numbers unset (O-4).** | Phone attach + transfer hydration use placeholders. | Provision skips the phone attach with a logged reason; `assistant-request` hydrates a placeholder transfer number; everything else ships. Read from `HHT_TRANSFER_NUMBER_*` / `VAPI_PHONE_NUMBER_ID` env. |
| **Staff email / SMTP unset (O-9).** | No email on a call. | `EmailSink` logs the would-send; the **durable `VoiceCall` write still happens** (the record is never lost — ADR-017). Slack off until a webhook is supplied. |
| **Verbatim house education copy** is blocked by the Vercel wall (`_research-education-blogs.md` Provenance). | Education rows are distilled, not verbatim. | Seed the `[SITE]`-distilled content marked `provisional`; `kb/seed.py` re-runnable so a later verbatim paste (browser/computer-use MCP) updates the rows. Does NOT block the FAQ deliverable (hours/returns/payment/limits are confirmed facts). |
| **Single-number multi-store routing.** | A single inbound number can't auto-know the caller's store. | P0 defaults to `HHT_DEFAULT_STORE=yakima` and lets the caller name their store (the `store` arg flows into `faq_lookup`). One-number-per-store routing is an O-4 config choice, not a P0 blocker. |
| **Per-node config creeping back in later phases** (export #7). | Token bloat + drift. | `DEEPGRAM_KEYTERMS`/voice/model are member-level constants from P0; the no-dup test (F3) is re-run on every later publish. |

---

## 10. Definition of done (P0)

- All §7 acceptance criteria pass with pasted output (`ruff check`, `ruff format --check`, `pytest`, `manage.py check`, `makemigrations --check`).
- A real inbound call answers **hours / specials / returns / payment / pickup / a weight** grounded in the KB, with a durable `VoiceCall` log written and a staff email (or logged no-op) fired (manual §8.4, evidence pasted).
- A KB edit is reflected on the next call with no redeploy (G3).
- **The fork gate (§0) is satisfied:** `voice/webhooks.py` (the §4 contract), `core/services/vapi.py`, the `voice/tools/` registry, and `voice/models.VoiceCall/VoiceTurn` are merged + green — so P1/P2/P3 worktrees may now fork.
- Provisioning is idempotent (zero drift on re-run, F2) and never touches `/workflow` (F3).
- Docs updated in the SAME change (`03-CONVENTIONS.md` §6): tick `10-P0-CHASSIS-FAQ.md` in `00-MASTER-ROADMAP.md` §7 "Phase specs" + the P0 build checklist; record the `voice/` app + `VoiceCall`/`VoiceTurn` models + the `kb/` voice models in `01-ARCHITECTURE.md` §8; append any new ADR (e.g. the webhook-signature scheme once pinned) to `02-DECISIONS.md`; append the day's note to `brain/Daily/` (if that vault is mirrored here) / the project log.

---

## 11. Source-file anchors (for the executor — all verified present)

**swedish-bot chassis (fork; `C:\Users\vladi\OneDrive\Desktop\swedish-bot`):**
- Deploy: `config/settings.py` (prod-fail-closed `if not DEBUG`~L153-176, `PHONE_HASH_PEPPER`~L127), `config/urls.py`, `config/{wsgi,asgi}.py`, `Dockerfile`, `docker-compose.yaml`, `docker-compose.prod.yaml`, `Caddyfile`, `Caddyfile.prod`, `Makefile`, `pyproject.toml`, `uv.lock`, `.env.example`.
- Services: `core/services/gemini.py` (**verbatim** — `embed`~L284, `health_check`~L339, `active_embedding_model`~L274), `core/constants.py` (`EMBED_DIM=768`~L45, `MODELS`~L21, `UnknownModelError`~L61), `core/middleware.py` (`WidgetCorsMiddleware`~L11), `core/views.py` (`healthz`~L27).
- KB: `kb/models.py` (`AgentPrompt`~L226, `FlowConfig`~L276, `FAQEntry`~L150, `PolicyDocument`~L312, `SiteFAQ`~L368), `kb/semantic.py` (`_corpus_vectors`~L52, `rank_guides`~L103→`rank_faq`, `_cos`~L44), `kb/ingest.py` (`parse_pdf_text`~L24, `ingest_pdf_bytes`~L39, `MAX_PDF_BYTES`~L15).
- CRM: `crm/models.py` (`phone_hash`~L17, `Customer.save`~L58), `crm/sinks.py` (`EmailSink`~L40, `dispatch`~L119), `crm/profile.py`.
- Channel-adapter shape: `chat/views.py` (`@csrf_exempt` + rate-limit + parse-JSON webhook shape), `chat/guardrails.py` (code-owned safety pattern), `chat/orchestrator.py` (deterministic FSM, if any server-side turn logic is needed).

**Net-new (write in P0):** `core/services/vapi.py`, `voice/` (whole app: `webhooks.py`, `signing.py`, `tools/__init__.py`+`tools/faq.py`, `guardrails.py`, `summarize.py`, `models.py`, `constants.py`, `urls.py`), `kb/seed.py`, `kb/vapi_files.py`, `kb/management/commands/seed_kb.py`, `tools/provision_vapi.py`, the P0 test suite.

**Foundation + research (read):** `C:\happytime-voice\docs\plans\{00-MASTER-ROADMAP,01-ARCHITECTURE,02-DECISIONS,03-CONVENTIONS}.md`; `_research-education-blogs.md` (KB seed source: FAQ/§2 edibles/§3 microdosing/§4 THC-CBD/§5 strains/§6 concentrates/§7 storage/§8 house style/§9 weights/§10 WA limits); `_research-suggestion-engine.md` (the leak-safe contract P1 consumes — P0 only needs §5.4 leak guarantee + §0 for the `phone`/phone-hash seam); the synthesis brief (`…/wp427jhrt.output`, §2 reusable assets + §4 target architecture + §5 P0 slice).

**Dependencies authored by LATER phases (so the executor knows the seams P0 must leave clean):** `20-SPEC-vapi-deploy.md` (the full `core/services/vapi.py` CRUD + the Vapi signature scheme — P0 stubs the deep methods, implements the idempotency primitive + `auth_ok`), P1 (`voice/budtender_client.py`, `voice/tools/suggest.py` — append-only to the registry), P2 (`escalation` member + immediate-alert routing in `crm/sinks.py`), P3 (`voice/tools/vendor.py`, `crm/models.VendorCallback`), P4 (`dashboard/` + Publish-to-Vapi — reuses the shared assistant/squad payload builder).
