# 03 — CONVENTIONS — Happy Time Voice Agent

> **Status:** FOUNDATION (authoritative, binding). Written 2026-06-22.
> Coding conventions, the full env-var catalog, git/commit conventions, and the testing planes. Every phase doc inherits these. Where a convention is lifted from swedish-bot, the source path is cited so agents match it exactly.

---

## 1. Coding conventions

### 1.1 Django app layout (fork swedish-bot)
- **Apps:** `core`, `voice` (new), `kb`, `crm`, `dashboard`. One responsibility per app. No `chat` app — telephony folds into `voice`.
- **Lean single settings file:** `config/settings.py` (ported from `swedish-bot/config/settings.py`) — NO `settings/{dev,prod}` split. All variance is **env-driven** via `os.environ` / `django-environ` reads inside the one file.
- **Services, not fat views:** integration logic lives in `core/services/*` (`gemini.py`, `vapi.py`) and `voice/*` (`budtender_client.py`, `tools/`, `summarize.py`); views/webhooks stay thin and dispatch to services.
- **Tools as a package:** `voice/tools/` is a package with one module per tool + a `TOOL_REGISTRY` dispatch in `__init__.py` (ADR-020). Never a single `voice/tools.py`.
- **Match the surrounding file** — naming, structure, comment density, idioms of swedish-bot when porting; of budtender when integrating. No drive-by refactors of ported code.

### 1.2 Env-driven settings + prod-fail-closed
- Every external dependency (DB, Gemini, Vapi, budtender, email, Slack) is configured by env vars (§3). Code reads env once at settings/module load; no hardcoded URLs/keys/numbers.
- **Prod-fail-closed (port from swedish-bot):** when `DJANGO_DEBUG=0` the app **refuses to boot** unless: `DJANGO_SECRET_KEY` is non-default AND `PHONE_HASH_PEPPER` differs from `DJANGO_SECRET_KEY` AND required prod secrets (`VAPI_PRIVATE_KEY`, `VAPI_WEBHOOK_SECRET`, `HHT_BACKEND_TOKEN`) are present. Mirror swedish-bot's pattern (it fails closed on default secret + non-EU residency).
- **Fail-closed at the edge:** the Vapi webhook rejects a missing/bad signature with 401 before any handler runs; the budtender client and email sink fail loud (log + raise), never silently swallow.

### 1.3 Tooling: uv + ruff
- **Dependency management = `uv`** (port `pyproject.toml` + `uv.lock` shape from swedish-bot). Add deps via `uv add`; never hand-edit the lock.
- **Lint/format = `ruff`.** Before "done": `ruff check` + `ruff format --check` clean. Match swedish-bot's ruff config.
- **Verify before done (binding):** paste real output of `ruff check`, `ruff format --check`, the targeted `pytest`, and (settings-affecting changes) `python manage.py check` + `makemigrations --check`. Never claim passing without the pasted output.

### 1.4 Naming
- **Python:** `snake_case` functions/modules, `PascalCase` classes/models, `UPPER_SNAKE` constants. Models singular (`VoiceCall`, `VendorCallback`, `Caller`).
- **Env vars:** `UPPER_SNAKE`, prefixed by domain — `VAPI_*` (Vapi surface), `HHT_*` (Happy Time internal services/config), `DUTCHIE_*` (POS, **budtender only**), `EMAIL_*`/`STAFF_ALERT_*` (alerts), `DJANGO_*` (framework), `GEMINI_*`/`GOOGLE_*` (LLM).
- **Vapi objects:** assistant names match member roles exactly: `entry_router`, `budtender`, `faq`, `vendor`, `escalation`. Tool names: `faq_lookup`, `suggest_products`, `check_inventory`, `pair_upsell`, `notify_vendor_callback`.
- **Tool webhook events** handled by name: `assistant-request`, `tool-calls`, `status-update`, `end-of-call-report`.

### 1.5 Surgical-change + Numbers-Guard discipline
- Minimum code that solves the problem; reuse before writing new (search swedish-bot/budtender first).
- **Numbers-Guard:** the LLM NEVER originates a figure (price/limit/hours/quantity). Numbers come from KB rows or budtender responses; the model only phrases them. Prices spoken are **out-the-door** (ADR-009).
- **Leak-Guard:** cost/margin can never reach a response (ADR-008) — enforced by budtender's allowlist serializer AND a voice-repo contract test.

---

## 2. Vapi conventions

- **One Squad, 5 saved assistants.** Voice/transcriber/model set ONCE per member (ADR-011) — never per node.
- **Provisioning is idempotent code** (`tools/provision_vapi.py`): GET-then-PATCH; store returned ids on local rows; a re-run yields zero drift.
- **Never call `/workflow`** (undocumented/beta). Only documented CRUD on `/assistant`, `/squad`, `/tool`, `/phone-number`.
- **Tool `server.url`** = `${PUBLIC_BASE_URL}/api/voice/vapi` for every custom tool; the webhook routes by tool name via `TOOL_REGISTRY`.
- **Transfers** use `transferPlan` warm-transfer-wait-for-operator + a `summaryPlan` injecting `{{transcript}}`; destination from `HHT_TRANSFER_NUMBER_*`.
- **Hydrate `{{store_name}}`/hours/transfer-number** via per-phone-number assistant overrides — never leave a literal `{{store_name}}` (export bug #11).

---

## 3. Env var catalog

Every var with a description + example/placeholder. Source files: `swedish-bot/.env.example`, `happytime-budtender/.env.example`. **Per-store Dutchie keys live in budtender, NOT here** (ADR-004/019).

### 3.1 Django framework
| Var | Description | Example / placeholder |
|---|---|---|
| `DJANGO_SECRET_KEY` | Django secret. Non-default required in prod (fail-closed). | `change-me-to-a-50-char-random-string` |
| `DJANGO_DEBUG` | `1` dev / `0` prod (prod hardens + fails closed). | `0` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames. | `voice.happytimeweed.com,localhost,127.0.0.1` |
| `CSRF_TRUSTED_ORIGINS` | Dashboard origins (the API webhook is HMAC, not cookie). | `https://voice.happytimeweed.com` |
| `PUBLIC_BASE_URL` | Public HTTPS base Vapi tools call back to. | `https://voice.happytimeweed.com` |

### 3.2 Database (Postgres; matches docker-compose)
| Var | Description | Example |
|---|---|---|
| `POSTGRES_DB` | DB name. | `happytime_voice` |
| `POSTGRES_USER` | DB user. | `happytime_voice` |
| `POSTGRES_PASSWORD` | DB password. | `change-me` |
| `POSTGRES_HOST` | DB host. | `db` |
| `POSTGRES_PORT` | DB port. | `5432` |

### 3.3 Vapi surface (`VAPI_*`)
| Var | Description | Example / placeholder |
|---|---|---|
| `VAPI_PRIVATE_KEY` | Vapi private API key — `Authorization: Bearer`. Provision + publish. | `vapi-priv-xxxxxxxx` |
| `VAPI_WEBHOOK_SECRET` | Shared secret/HMAC key to verify inbound Vapi webhooks (fail-closed). | `change-me-long-random-webhook-secret` |
| `VAPI_SQUAD_ID` | Id of the provisioned Squad (written back by the provision script). | `(set by provision_vapi.py)` |
| `VAPI_PHONE_NUMBER_ID` | Inbound number id fronting the Squad. **O-4 placeholder.** | `(owner-supplied)` |
| `VAPI_VOICE_ID` | Cartesia sonic-3 voice id (Koptza). | `a3520a8f-226a-428d-9fcd-b0a4711a6829` |
| `VAPI_ASSISTANT_MODEL` | Single intentional assistant model (ADR-010). | `gpt-4.1-mini` |

### 3.4 budtender microservice (`HHT_*`)
| Var | Description | Example / placeholder |
|---|---|---|
| `HHT_BUDTENDER_BASE_URL` | Base URL of the happytime-budtender service. **O-1 placeholder.** | `https://budtender.internal` |
| `HHT_BACKEND_TOKEN` | Bearer service token shared with budtender (`auth.py`, constant-time). Must equal budtender's `HHT_BACKEND_TOKEN`. | `change-me-long-random-service-token` |
| `HHT_BUDTENDER_TIMEOUT` | HTTP timeout (s) for budtender calls. | `8` |

### 3.5 Transfer / phone routing (`HHT_TRANSFER_*`) — **O-4 placeholders**
| Var | Description | Example / placeholder |
|---|---|---|
| `HHT_TRANSFER_NUMBER_YAKIMA` | Yakima store transfer destination. | `+15095711106` |
| `HHT_TRANSFER_NUMBER_MTVERNON` | Mt Vernon store transfer destination. | `+13604882923` |
| `HHT_TRANSFER_NUMBER_PULLMAN` | Pullman store transfer destination. | `+15093342788` |
| `HHT_DEFAULT_STORE` | Store assumed when caller hasn't specified. | `yakima` |
| `HHT_VENDOR_CALLBACK_WINDOW` | Spoken callback window the vendor member states on a no-answer leg (Numbers-Guard source, P3). | `one business day` |
| `VENDOR_CALLBACK_WEBHOOK_URL` | Optional n8n/CRM secondary sink for vendor callbacks (O-6/O-9, P3). Off when unset. | `(owner-supplied)` |

> Public store lines (from store facts): Yakima (509) 571-1106 · Mt Vernon (360) 488-2923 · Pullman (509) 334-2788. Confirm whether these are also the *transfer* destinations or a different staff line (O-4).

### 3.6 Gemini / Vertex (server-side LLM; `GEMINI_*`/`GOOGLE_*`) — from swedish-bot
| Var | Description | Example |
|---|---|---|
| `GEMINI_USE_VERTEX` | Prefer Vertex AI (data residency) over the API-key path. | `True` |
| `GOOGLE_CLOUD_PROJECT` | GCP project for Vertex. | `(owner-supplied)` |
| `GOOGLE_CLOUD_LOCATION` | Vertex region. | `us-central1` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to SA JSON (or use ADC). | `/secrets/sa.json` |
| `GEMINI_API_KEY` | Dev fallback consumer key (AI Studio). | `(dev only)` |
| `ALLOW_NON_EU_RESIDENCY` | swedish-bot fail-closed flag; US store → `1`. | `1` |

### 3.7 Email / staff alerts (`EMAIL_*` / `STAFF_ALERT_*`) — **O-9 placeholders**
| Var | Description | Example / placeholder |
|---|---|---|
| `EMAIL_HOST` | SMTP host. | `smtp.gmail.com` |
| `EMAIL_PORT` | SMTP port. | `587` |
| `EMAIL_HOST_USER` | SMTP user. | `(owner-supplied)` |
| `EMAIL_HOST_PASSWORD` | SMTP app password. | `(owner-supplied)` |
| `EMAIL_USE_TLS` | TLS. | `True` |
| `LEAD_EMAIL_FROM` | From address for alerts (swedish-bot name). | `bot@happytimeweed.com` |
| `STAFF_ALERT_EMAIL` | Default staff alert sink (shared). | `happytimeyak509@gmail.com` |
| `STAFF_ALERT_EMAIL_YAKIMA` | Per-store override (optional). | `(owner-supplied)` |
| `STAFF_ALERT_EMAIL_MTVERNON` | Per-store override (optional). | `(owner-supplied)` |
| `STAFF_ALERT_EMAIL_PULLMAN` | Per-store override (optional). | `(owner-supplied)` |

### 3.8 Slack (optional secondary sink; `SLACK_*`) — **O-9 placeholder**
| Var | Description | Example / placeholder |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Incoming-webhook URL for the optional Slack sink. | `(owner-supplied)` |
| `SLACK_ALERTS_ENABLED` | `0` until a webhook is supplied. | `0` |

### 3.9 Security / PII (from swedish-bot)
| Var | Description | Example |
|---|---|---|
| `PHONE_HASH_PEPPER` | Pepper for the returning-caller phone-hash. **MUST differ from `DJANGO_SECRET_KEY`** (fail-closed). | `change-me-distinct-from-secret-key` |
| `HTTPS_ENABLED` | `1` in prod behind real TLS (HSTS + secure cookies). | `1` |
| `RATE_LIMIT_WINDOW` | Webhook/tool rate-limit window (s). | `300` |

### 3.10 Dutchie (reference ONLY — these live in the budtender service's env, NOT here)
> Listed so agents do NOT add them to the voice repo. In `happytime-budtender/.env`: `DUTCHIE_YAKIMA_POS_KEY`, `DUTCHIE_MTVERNON_POS_KEY`, `DUTCHIE_PULLMAN_POS_KEY` (HTTP Basic, key as username; read-only). Per ADR-004/019 the voice repo never holds a Dutchie key.

---

## 4. Git / commit conventions

- **Repo:** `C:\happytime-voice`. Default branch `main`. Branch per concern: `p0/chassis-faq`, `p1/dutchie-suggest`, `p2/escalation`, `p3/vendor`, `p4/dashboard-publish`, `p5/polish`.
- **Worktrees for parallel work:** P1/P2/P3 each in their own `git worktree` (see roadmap §6); never two agents mutating the same file in the same tree.
- **Conventional-commit-ish, terse (caveman OK for code/commit msgs only — never docs):** `feat(voice): bind suggest_products tool to budtender`, `fix(escalation): wire inbound transitions`, `chore(provision): idempotent squad upsert`.
- **Stage only your own files.** Never sweep unrelated changes into a commit. Commit only when asked.
- **End commit messages with:** `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **One ADR per real architectural decision** — append to `02-DECISIONS.md` (ADR-0XX), don't bury it in a commit.

---

## 5. Testing planes

Mirrors swedish-bot/budtender's discipline; voice adds a contract + manual-call plane.

| Plane | Scope | How to run | Notes |
|---|---|---|---|
| **Unit** | Pure functions — phone-hash, slot validation, guardrails, OTD formatting, `_clean_graph`, tool arg parsing. SQLite-OK, fast. | `pytest -m "not integration and not manual"` | No network. Deterministic. |
| **Contract** | The voice⇄budtender + voice⇄Vapi boundaries. Assert tool-response shape; **assert no "cost"/"margin" substring** in any tool response (Leak-Guard); assert webhook rejects a bad HMAC (fail-closed); assert provisioning is idempotent (second run = zero new Vapi objects, mocked). | `pytest -m integration` | budtender stubbed/recorded until O-1 confirmed; Vapi client mocked. |
| **Provisioning** | `tools/provision_vapi.py` against a Vapi sandbox key — squad+assistants+tools+phone created, re-run drift-free, ids written back. | `python tools/provision_vapi.py --dry-run` then live | Idempotency is an acceptance criterion (ADR-003). |
| **Manual call script** | A real inbound test call per phase per `00-MASTER-ROADMAP.md §5` success criteria (FAQ grounded answer; suggestion with why_this + gated upsell; escalation warm transfer; vendor no-answer→callback). | Dial `VAPI_PHONE_NUMBER_ID`; follow the per-phase script. | The definition of done for a phase — paste transcript + VoiceCall row. |

**Test data discipline:** deterministic fixtures; expected values hand-authored, not generated by the code under test. The Leak-Guard test and the HMAC-fail-closed test are **non-negotiable gates** on every phase that touches a tool or the webhook.

**Coverage:** target ~90% diff coverage on changed code; never lower a ratchet once set.

---

## 6. Documentation protocol (binding, meta)

- **Before any task:** read `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → this file → the relevant phase doc.
- **After any task, in the SAME change:** update the docs you touched (bump status, record new ADRs/invariants), and check off the roadmap checklist item.
- **Every executable phase/spec doc MUST contain:** Goal & scope; Dependencies (what must exist first); File-by-file task list (exact path → responsibility → key functions/shape → source file to port from with its path); Data contracts/JSON schemas; Vapi deploy steps (assistants/tools/PATCH calls); Acceptance criteria (testable, concrete); Test plan (unit + contract + manual call script); Risks/Open-questions. Cite real file paths. Be exhaustive — these docs drive autonomous multi-agent execution.
- **No task starts without context loaded; none completes without docs updated.** Sub-agents inherit this.
