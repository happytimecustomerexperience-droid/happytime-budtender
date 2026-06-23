# 20 — SPEC — AUTO-DEPLOY-VIA-VAPI-API — Executable Spec

> **Status:** EXECUTABLE SPEC (authoritative for the Vapi REST client + the idempotent provisioning module). Written 2026-06-22.
> **Subsystem:** cross-cutting control-plane primitive consumed by **P0** (first provision), **P1/P2/P3** (members + tools added incrementally), **P4** (Publish-to-Vapi reuses the payload builders + client), **P5** (re-publish persona/cartridge/correction copy). **Implements ADRs (binding, never contradicted here):** ADR-002 (Assistants + ONE Squad, NEVER a Workflow), ADR-003 (everything auto-deployable via the documented Vapi REST API as an **idempotent, re-runnable** provisioning script), ADR-010 (gpt-4.1-mini assistants / Gemini server-side), ADR-011 (voice/persona/transcriber set ONCE per member), ADR-012 (KB mirrored to Vapi Files + a Query Tool), ADR-015 (vendor flow), ADR-016 (escalation fixed + warm transfer + summaryPlan), ADR-019 (HMAC fail-closed, secrets server-side, per-store Dutchie keys ONLY in budtender), ADR-020 (`voice/tools/` registry).
> **Read order before executing (mandatory):** `00-MASTER-ROADMAP.md` → `01-ARCHITECTURE.md` → `02-DECISIONS.md` → `03-CONVENTIONS.md` → `10-P0-CHASSIS-FAQ.md` → this file. Cross-checks `14-P4-dashboard-publish.md` (the publish builders are SHARED with this spec) and `15-P5-polish-brand.md` (re-publish path).
> **Ports/seeds config from:** the Vapi export `C:\Users\vladi\Downloads\happy-time-voice-agent-(full-script)-(uploaded-via-json).json` (voice block lines 21–31; Deepgram nova-3 + 33-term keyterm list lines 32–72; the shadowed model conflict global `gpt-5.2-chat-latest` L3955 vs per-node `gpt-4.1-mini` maxTokens 250 temp 0.3 L15–20; the orphan `escalation` node L3537 + empty `transfer_call.destinations: []` L3628; the `globalPrompt` L3960 to distribute into per-member prompts). Reuses swedish-bot's `requests.Session` + fail-loud idioms and budtender's constant-time auth pattern.
>
> **One-line goal:** **one command — `python manage.py provision_vapi` — stands up the entire live Vapi stack (ONE Squad + 5 assistants + all tools + KB Files + the inbound phone-number attachment) from env, and re-running it is a guaranteed no-op (zero new Vapi objects, zero drift).** Every Vapi object is defined **as code** in `voice/provision.py`; the client `core/services/vapi.py` is the only thing that talks to `api.vapi.ai`; nothing is click-ops.

---

## 1. Goal & scope

### 1.1 In scope (this spec defines all of)

1. **`core/services/vapi.py` — the Vapi REST client.** A thin, fail-loud `requests.Session` wrapper over `https://api.vapi.ai`, `Authorization: Bearer ${VAPI_PRIVATE_KEY}`, with **every CRUD method** the stack needs:
   - **Assistants:** `list_assistants`, `get_assistant`, `create_assistant`, `patch_assistant`, `delete_assistant`, `find_assistant_by_name`.
   - **Squads:** `list_squads`, `get_squad`, `create_squad`, `patch_squad`, `delete_squad`, `find_squad_by_name`.
   - **Tools:** `list_tools`, `get_tool`, `create_tool`, `patch_tool`, `delete_tool`, `find_tool_by_name`.
   - **Phone numbers:** `list_phone_numbers`, `get_phone_number`, `patch_phone_number` (attach Squad), `find_phone_number_by_id`/`by_e164`.
   - **Files (KB mirror):** `list_files`, `upload_file`, `get_file`, `delete_file`, `find_file_by_name`.
   - **Health:** `auth_ok()` (cheap `GET /assistant?limit=1`).
   - Cross-cutting: Bearer auth header injected once; **retries with exponential backoff + jitter** on 429/5xx/transport errors (honoring `Retry-After`); **pagination** helper for list endpoints; **secret redaction** in every log line; a typed `VapiError(status, body, method, path)` raised on any non-2xx after retries (never a silent swallow); a `dry_run` mode that records intended calls instead of issuing them.
   - **Never** a `/workflow` path (ADR-002).
2. **`voice/provision.py` — the idempotent provisioning module (everything-as-code).** Defines the **whole Vapi stack declaratively** and reconciles it:
   - The **Squad** "Happy Time Voice."
   - The **5 assistants** `entry_router` / `budtender` / `faq` / `vendor` / `escalation`, each as a full payload (model gpt-4.1-mini, Cartesia sonic-3 Koptza voice, Deepgram nova-3 + the 33-term keyterm list, system prompt sourced from `AgentPrompt`, `server.url` + `serverMessages`, attached tool ids, transferPlan for vendor/escalation).
   - **All tools** `faq_lookup` / `suggest_products` / `check_inventory` / `pair_upsell` / `notify_vendor_callback` (custom/function tools, `server.url → ${PUBLIC_BASE_URL}/api/voice/vapi`).
   - The **KB Files + Query Tool** (delegates to `kb/vapi_files.py::mirror_all`, attaches the Query Tool to `faq`).
   - The **phone-number attachment** (`PATCH /phone-number/{id}` → `squadId`).
   - The reconcile contract: **create-or-PATCH by stored id, falling back to find-by-name** — safely re-runnable, zero drift.
3. **`voice/management/commands/provision_vapi.py` — the `manage.py provision_vapi` command.** The single operator entry point: `python manage.py provision_vapi [--dry-run] [--only assistant,squad,tool,file,phone] [--member <role>] [--prune] [--verbose]`. Loads context, runs `voice/provision.provision_all(...)`, prints a per-object reconcile report, exits 0 on success / non-zero on any hard error.
4. **The shared payload builders** (`build_assistant_payload(role)`, `build_squad_payload()`, `build_tool_payload(name)`) — the **single source of truth** for the Vapi JSON shapes, imported by BOTH this provisioner AND P4's `dashboard/publish.py` (one shape, two callers — `14-P4` §9 risk mitigation).
5. **The webhook signing/secret scheme contract** (`server.secret` set in the provision payload; verified in `voice/signing.py`) — pinned here because the provisioner is what *writes* the secret into every assistant/tool `server` block, so the deploy spec owns the contract the P0 webhook verifies against.

### 1.2 Out of scope (other docs / EXP)

- The **tool handler bodies** (`voice/tools/faq.py|suggest.py|vendor.py`) — owned by P0/P1/P3. This spec only references their **registered names** to attach the right tool ids.
- The **dashboard UI / Publish-to-Vapi views** — P4 (`14-P4-dashboard-publish.md`). This spec defines the builders P4 calls; P4 wires them to HTMX views.
- The **KB content/seed** (`kb/seed.py`) — P0. This spec only mirrors whatever `kb/vapi_files.py::mirror_all` renders.
- **budtender** — a separate service (ADR-004); never provisioned here.
- **Voice cloning / a second Vapi number per store** — EXP (`16-CAPABILITY-EXPANSIONS.md`).

### 1.3 Non-negotiable boundaries (binding)

- **Idempotent, re-runnable, zero drift (ADR-003).** A second `provision_vapi` run with no local edits creates **zero** new Vapi objects and issues **zero** PATCH calls that change anything (every reconcile resolves to `nodrift`). This is the headline acceptance criterion (§7 A-IDEMP).
- **GET/find-then-PATCH, never blind double-POST.** Object creation only happens when neither a stored id nor a find-by-name match exists. A stored id that 404s falls back to find-by-name before a single create.
- **Voice / transcriber / model set ONCE per assistant (ADR-011).** The provisioner NEVER emits a per-node voice/transcriber/model block (the export's #7 bug). A unit test asserts each appears exactly once per assistant payload and the `DEEPGRAM_KEYTERMS` list is a single shared constant.
- **One intentional model (ADR-010).** Every assistant payload pins `model.provider="openai"`, `model.model="gpt-4.1-mini"`. The shadowed `gpt-5.2-chat-latest` from the export is NEVER emitted.
- **Secrets server-side + never logged (ADR-019).** `VAPI_PRIVATE_KEY`, `VAPI_WEBHOOK_SECRET`, `HHT_BACKEND_TOKEN` never appear in a log line, a reconcile report, or a dry-run dump (`server.secret` is redacted to `***`). The provisioner is the thing that *writes* `server.secret` into every assistant/tool `server` block from `VAPI_WEBHOOK_SECRET`.
- **Never `/workflow` (ADR-002).** Only the documented CRUD on `/assistant`, `/squad`, `/tool`, `/phone-number`, `/file`.
- **Per-store Dutchie keys are never touched here (ADR-004/019).** They live only in budtender; this repo's only secrets are Vapi + the budtender Bearer token + email/Slack.
- **Graceful degradation, never a hard crash on a placeholder.** A missing `VAPI_PHONE_NUMBER_ID` (O-4) / `HHT_TRANSFER_NUMBER_*` (O-4) / Vapi Files API access is *read at call time*, never required at import: the run completes the parts it can, and reports `skipped — <placeholder> not configured` per object.

---

## 2. Dependencies (what MUST exist first)

This spec is **authored alongside P0** (P0's `10-…` §3.3 + §11 explicitly defer the deep CRUD, the Files API, retry/pagination, and the Vapi signature scheme to this file). The provisioner reconciles whatever members/tools the *current* phase has defined as `AgentPrompt`/tool rows — so it grows P0→P3 without restructuring.

| # | Dependency | Where it comes from | What this spec consumes |
|---|---|---|---|
| D1 | `VAPI_PRIVATE_KEY` + `VAPI_WEBHOOK_SECRET` env | owner (Vapi dashboard) → `03-CONVENTIONS.md` §3.3 | Bearer auth on every call; `server.secret` written into every assistant/tool payload. Absent → the client's `auth_ok()` returns `False`, the command exits with a clear "VAPI_PRIVATE_KEY not configured" message (no crash). |
| D2 | `PUBLIC_BASE_URL` env | `03-CONVENTIONS.md` §3.1 | Every tool's `server.url = ${PUBLIC_BASE_URL}/api/voice/vapi`; every assistant's `server.url` (for `assistant-request`/`status-update`/`end-of-call-report`). |
| D3 | `kb/models.AgentPrompt` (role-keyed, with `vapi_assistant_id`/`vapi_model`/`voice_id`/`tool_names`/`transfer_number_key`) | **P0** forks it (swedish-bot `kb/models.py` `AgentPrompt`~L226); **P4** extends the voice fields (`14-P4` §4.1) | The system-prompt body + per-role tool list + transfer key for `build_assistant_payload`. The provisioner writes `vapi_assistant_id` back. |
| D4 | A `VapiObject` registry table (or columns) mapping `(kind, name) → vapi_id` + `last_provision_hash` | **this spec** defines it (§4.6); migration in `voice/migrations` | Idempotency: the stored-id-first reconcile + the zero-drift hash. |
| D5 | `voice/tools/__init__.py::TOOL_REGISTRY` (the registered tool names) | **P0** ships the registry (ADR-020); P1/P3 append `suggest_products`/`pair_upsell`/`check_inventory`/`notify_vendor_callback` | The tool name → `build_tool_payload` set; only registered tools are provisioned (a `tool_name` on an `AgentPrompt` with no registered handler is reported `skipped — tool not registered`, never provisioned dangling). |
| D6 | `kb/vapi_files.py::mirror_all()` | **P0** (net-new) | The Files + Query Tool mirror step (`--only file`); the provisioner calls it and attaches the returned Query Tool id to the `faq` assistant. |
| D7 | `voice/constants.py::DEEPGRAM_KEYTERMS`, `CARTESIA_VOICE`, `ASSISTANT_MODEL`, `SERVER_MESSAGES`, `SQUAD_SHAPE` | **this spec** defines them (§4.1/§4.7); P0 ships the file | The single shared config constants every payload builder reads (no per-node dup). |
| D8 | `VAPI_PHONE_NUMBER_ID` (O-4) + `HHT_TRANSFER_NUMBER_{YAKIMA,MTVERNON,PULLMAN}` (O-4) env | owner placeholders → `03-CONVENTIONS.md` §3.3/§3.5 | The phone-number attach step + the vendor/escalation `transferPlan.destinations`. Absent → that step reports `skipped — not configured`; the rest provisions. |

**Reconciliation with `tools/provision_vapi.py` (P0's name):** `10-P0` referenced a `tools/provision_vapi.py`. **This spec makes `voice/provision.py` + `manage.py provision_vapi` the authoritative module.** `tools/provision_vapi.py` (if kept) is a **thin shim** that calls `from voice.provision import provision_all; provision_all()` (or the Makefile target `make provision` simply runs `python manage.py provision_vapi`). The deep logic lives in the Django app (`voice/provision.py`) so it shares models, settings, `AgentPrompt` rows, and the `VapiObject` table — a bare `tools/` script could not. P0 may keep its single-member `entry_faq` bootstrap calling `provision_all(only_members=["faq"])`; this spec's full version simply reconciles the additional members as later phases add their `AgentPrompt` rows.

---

## 3. File-by-file task list

Each entry: **exact path → responsibility → key functions/shape → source file to port from (with path)**. New files marked ★; ported/extended files cite the origin.

### 3.1 The client

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `core/services/vapi.py` ★ | The Vapi REST client (the ONLY code that talks to `api.vapi.ai`). Bearer auth, retries/backoff, pagination, secret redaction, `VapiError`, `dry_run`. Full CRUD for assistant/squad/tool/phone-number/file + `auth_ok`. | `class VapiClient` (see §5 for every method signature + behavior). Module-level `vapi = VapiClient.from_settings()` singleton. `class VapiError(Exception)`. | net-new. Pattern: swedish-bot's `requests.Session` + `raise_for_status` idiom (`swedish-bot/core/services/gemini.py` error-handling shape); budtender `auth.py` constant-time/fail-closed posture for the Bearer header. |
| `core/services/vapi.py` (cont.) | Retry/backoff helper. | `_request(method, path, *, params, json, idempotency_key)` → handles 429/5xx/`ConnectionError`/`Timeout` with `min(BASE*2**n, CAP)+jitter`, honors `Retry-After`, max `VAPI_MAX_RETRIES` (default 4). | net-new. |
| `core/services/vapi.py` (cont.) | Pagination. | `_paginated(path, params)` yields items across Vapi's cursor/limit pages until exhausted (cap `VAPI_LIST_CAP` to avoid runaway). | net-new. |

### 3.2 The provisioning module

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `voice/provision.py` ★ | **Everything-as-code + the reconcile engine.** Declares the Squad + 5 assistants + all tools + Files/Query-tool + phone attach; reconciles each create-or-PATCH-by-id/by-name; writes ids + hashes back; returns a `ProvisionReport`. | `provision_all(*, dry_run, only, members, prune) -> ProvisionReport`; `ensure_tool(name)`, `ensure_assistant(role)`, `ensure_squad()`, `ensure_files()`, `ensure_phone_number()`; `build_tool_payload(name)`, `build_assistant_payload(role)`, `build_squad_payload()`; `_reconcile(kind, name, payload, get/create/patch)`; `_payload_hash(payload)`. See §4/§5/§6. | net-new; uses `core/services/vapi.py` + `kb/vapi_files.py`. The 3 `build_*_payload` builders are SHARED with P4 `dashboard/publish.py` (`14-P4` §3.1/§5). |
| `voice/constants.py` ★ (P0 ships; this spec defines the Vapi-deploy constants) | Single source of truth for the shared Vapi config blocks (no per-node dup — ADR-011). | `CARTESIA_VOICE` (dict, §4.1), `DEEPGRAM_TRANSCRIBER` + `DEEPGRAM_KEYTERMS` (the 33 terms, §4.1), `ASSISTANT_MODEL="gpt-4.1-mini"`, `ASSISTANT_PROVIDER="openai"`, `SERVER_MESSAGES` (the `serverMessages` list, §4.4), `SQUAD_SHAPE` (the code-defined destinations graph, §4.7), `TOOL_SPECS` (name→param-schema, §4.5), `MEMBER_TOOLS` (role→tool names, §4.2). | net-new; the voice block + keyterm list are lifted from the export (`...Downloads\happy-time-voice-agent-(full-script)-(uploaded-via-json).json` L21–72). |
| `voice/models.py` EDIT (P0 owns the file) | Add the `VapiObject` idempotency registry (§4.6) + ensure `AgentPrompt.vapi_assistant_id` exists (P4 adds it; this spec depends on it). | `class VapiObject(kind, name, vapi_id, last_provision_hash, updated_at)` — unique on `(kind, name)`. Migration. | net-new model; mirror swedish-bot model idioms. |

### 3.3 The management command

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `voice/management/commands/provision_vapi.py` ★ | The single operator entry point. Parses flags, calls `voice.provision.provision_all`, renders the report, sets the exit code. | `class Command(BaseCommand)`: `add_arguments` (`--dry-run`, `--only`, `--member`, `--prune`, `--verbose`); `handle(...)` → `report = provision_all(...)`; print the per-object table; `raise CommandError` (non-zero exit) on any `action="error"`. | net-new; standard Django `BaseCommand` shape. (P0's `tools/provision_vapi.py`, if retained, is a 3-line shim calling this command or `provision_all`.) |
| `Makefile` EDIT | `make provision` → `python manage.py provision_vapi`; `make provision-dry` → `... --dry-run`. | add the two targets. | `swedish-bot/Makefile` target style. |

### 3.4 The webhook-secret seam (contract this spec owns; verifier lives in P0)

| Path | Responsibility | Key functions / shape | Port from |
|---|---|---|---|
| `voice/signing.py` (P0 owns the verifier; this spec owns the *contract* it verifies) | The provisioner writes `server.secret = ${VAPI_WEBHOOK_SECRET}` into every assistant + tool `server` block; `voice/signing.verify_signature(request)` must accept exactly that scheme, fail-closed. | `verify_signature(request) -> bool` (constant-time `hmac.compare_digest`); the provision payload's `server.secret`/`server.headers` is the matching half. See §8. | net-new verifier (P0); the **payload half** is owned here so both ends agree. |

---

## 4. Data contracts / JSON schemas

All payload shapes below are the **exact** bodies the client sends. They are produced by the `build_*_payload` builders in `voice/provision.py` and are the single shape P4 publish reuses. Config blocks marked “shared constant” are emitted **once per assistant** (ADR-011), never per node.

### 4.1 Shared voice + transcriber constants (`voice/constants.py`) — lifted from the export

```python
# Cartesia sonic-3 "Koptza" — export L21–31 (voiceId verbatim; ADR-011).
CARTESIA_VOICE = {
    "provider": "cartesia",
    "voiceId": "a3520a8f-226a-428d-9fcd-b0a4711a6829",  # env VAPI_VOICE_ID override
    "model": "sonic-3",
    "language": "en",
    "experimentalControls": {"emotion": ["positivity:highest"]},
}

# Deepgram nova-3 + the EXACT 33-term cannabis keyterm boost list — export L32–72.
# ONE shared constant; appears exactly once per assistant (no per-node dup — export bug #7).
DEEPGRAM_KEYTERMS = [
    "flower", "bud", "pre-roll", "pre-rolls", "joint", "joints",
    "concentrate", "concentrates", "dabs", "wax", "shatter", "resin",
    "live resin", "rosin", "cartridge", "cartridges", "cart", "carts",
    "vape", "vapes", "vape pen", "510", "disposable", "all-in-one",
    "edible", "edibles", "gummies", "chocolate", "drink", "drinks",
    "tincture", "tinctures", "oil", "oils",
]
DEEPGRAM_TRANSCRIBER = {
    "provider": "deepgram",
    "model": "nova-3",
    "numerals": True,                 # export L70 — spoken digits transcribed as numerals
    "keyterm": DEEPGRAM_KEYTERMS,     # Vapi/Deepgram field name "keyterm" (export uses "keyterm")
}

ASSISTANT_PROVIDER = "openai"
ASSISTANT_MODEL = "gpt-4.1-mini"      # ADR-010 single intentional model (NOT the shadowed gpt-5.2-chat-latest)
ASSISTANT_MAX_TOKENS = 250            # export per-node value (L18); router can be 200
```

> The export's `"all‑in‑one"` used a non-breaking hyphen (U+2011); the constant uses a plain `-` (`all-in-one`) so the keyterm matches transcripts. This is the one deliberate normalization of the lifted list.

### 4.2 Per-member tool attachment (`MEMBER_TOOLS`)

```python
MEMBER_TOOLS = {
    "entry_router": ["faq_lookup"],                                  # a trivial one-liner before handoff
    "budtender":    ["suggest_products", "check_inventory", "pair_upsell"],
    "faq":          ["faq_lookup"],                                  # + the KB Query Tool (attached by ensure_files)
    "vendor":       ["notify_vendor_callback"],                      # + transferCall (built from transfer_number_key)
    "escalation":   [],                                              # transferCall only (no custom tool)
}
```

A member's resolved `toolIds` = `[VapiObject(kind="tool", name=n).vapi_id for n in tool_names]`. Any name without a provisioned id → that assistant is reported `skipped — tool not provisioned: <name>` and its PATCH is NOT sent (never PATCH a dangling toolId — `14-P4` §5 invariant).

### 4.3 `build_assistant_payload(role)` → `POST/PATCH /assistant` body

The full shape, per member. (`entry_router` shown; the others differ only in `name`, `messages[0].content` (the system prompt from `AgentPrompt.body`), `model.toolIds`, `model.maxTokens`, and the presence of the `transferCall` tool for `vendor`/`escalation`.)

```json
{
  "name": "entry_router",
  "model": {
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "temperature": 0.3,
    "maxTokens": 200,
    "messages": [
      { "role": "system",
        "content": "<AgentPrompt(role='entry_router').body — Koptza greeting + spoken 21+ confirm (NO 'peek at your ID' — ADR-018) + one-turn intent classifier; {{store_name}}/hours hydrated via the phone-number assistant override, never a literal>" }
    ],
    "toolIds": ["<vapi tool id for faq_lookup>"]
  },
  "voice": {
    "provider": "cartesia",
    "voiceId": "a3520a8f-226a-428d-9fcd-b0a4711a6829",
    "model": "sonic-3",
    "language": "en",
    "experimentalControls": { "emotion": ["positivity:highest"] }
  },
  "transcriber": {
    "provider": "deepgram",
    "model": "nova-3",
    "numerals": true,
    "keyterm": ["flower","bud","pre-roll","pre-rolls","joint","joints","concentrate","concentrates","dabs","wax","shatter","resin","live resin","rosin","cartridge","cartridges","cart","carts","vape","vapes","vape pen","510","disposable","all-in-one","edible","edibles","gummies","chocolate","drink","drinks","tincture","tinctures","oil","oils"]
  },
  "server": {
    "url": "${PUBLIC_BASE_URL}/api/voice/vapi",
    "secret": "${VAPI_WEBHOOK_SECRET}"
  },
  "serverMessages": ["assistant-request", "tool-calls", "status-update", "end-of-call-report"],
  "firstMessageMode": "assistant-speaks-first"
}
```

**Per-member deltas (all else identical — voice/transcriber/server emitted ONCE each):**

| role | `maxTokens` | `toolIds` (resolved) | extra `model.tools` |
|---|---|---|---|
| `entry_router` | 200 | `[faq_lookup]` | — |
| `budtender` | 250 | `[suggest_products, check_inventory, pair_upsell]` | — |
| `faq` | 250 | `[faq_lookup, <KB Query Tool id>]` | — |
| `vendor` | 250 | `[notify_vendor_callback]` | `[transferCall → HHT_TRANSFER_NUMBER_<key>, warm, summaryPlan]` (§4.8) |
| `escalation` | 250 | `[]` | `[transferCall → HHT_TRANSFER_NUMBER_<key>, warm, summaryPlan]` (§4.8) |

> The system-prompt bodies are distributed from the export's `globalPrompt` (L3960) + the per-node prompts, split per member (entry/classify, budtender slot-fill, faq grounding, vendor, escalation) — the `AgentPrompt` rows P0/P3 author. This spec only *wires* them; it does not write the prose.

### 4.4 `serverMessages` (shared constant `SERVER_MESSAGES`)

```python
SERVER_MESSAGES = ["assistant-request", "tool-calls", "status-update", "end-of-call-report"]
```
These are the four webhook events `voice/webhooks.py` handles (`03-CONVENTIONS.md` §1.4). Every assistant subscribes to all four so the durable `VoiceCall` log + tool dispatch + eocr email fire (ADR-017).

### 4.5 `build_tool_payload(name)` → `POST/PATCH /tool` body (custom/function tool)

Each custom tool is a Vapi `function` tool whose `server.url` is our webhook; the webhook routes by tool name via `TOOL_REGISTRY` (ADR-020). `TOOL_SPECS` holds the JSON-Schema `parameters` per tool.

```json
{
  "type": "function",
  "function": {
    "name": "suggest_products",
    "description": "Return up to 3 in-stock, leak-safe product picks for the caller's slots, each with a speakable why_this and an out-the-door price. NEVER returns cost or margin.",
    "parameters": {
      "type": "object",
      "properties": {
        "store":          { "type": "string", "enum": ["yakima","mount-vernon","pullman"] },
        "category":       { "type": "string", "enum": ["flower","concentrate","cartridge","edible","tincture"] },
        "subcategory":    { "type": "string", "description": "granular subtype (rosin/gummies/disposable…), optional HARD filter" },
        "size":           { "type": "string", "description": "e.g. 1g, 3.5g, 7g, 10mg, single, 5pk — optional" },
        "price_tier":     { "type": "string", "enum": ["value","mid","top"], "description": "optional; or use price_min/price_max" },
        "price_min":      { "type": "number" },
        "price_max":      { "type": "number" },
        "effect_desired": { "type": "string", "enum": ["relaxed","uplifted","middle"] },
        "doh_only":       { "type": "boolean" }
      },
      "required": ["store","category"]
    }
  },
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" },
  "async": false
}
```

**`TOOL_SPECS` (name → schema summary; full schemas in `voice/constants.py`):**

| tool | required params | optional params | `async` | notes |
|---|---|---|---|---|
| `faq_lookup` | `query` | `store` | `false` | reads `kb/` live (canonical); the KB Query Tool is the fast fallback. |
| `suggest_products` | `store`, `category` | `subcategory`, `size`, `price_tier`/`price_min`/`price_max`, `effect_desired`, `doh_only` | `false` | → budtender `/products/search/`; ≤3 leak-safe picks (`_research-suggestion-engine.md` §5.2). |
| `check_inventory` | `store`, `sku` | — | `false` | → budtender purchasability gate; `{in_stock, qty_band, price_otd}`. |
| `pair_upsell` | `store`, `anchor_sku` | `session_token` | `false` | → budtender `/pairing/for-sku`; surface only if `strength` clears the gate (ADR-007). |
| `notify_vendor_callback` | `store`, `reason` | `caller_phone_hash`, `summary` | **`true`** | async (ADR-015) → logs `VendorCallback` + staff alert + states a callback window. |

> `transferCall` and `endCall` are Vapi **built-in** tool types declared inline on the assistant `model.tools` (not separate `/tool` objects) — see §4.8. Only the 5 custom function tools above are `/tool` CRUD objects.

### 4.6 `VapiObject` idempotency registry (`voice/models.py`)

```python
class VapiObject(models.Model):
    KIND = [("tool","tool"), ("assistant","assistant"), ("squad","squad"),
            ("phone_number","phone_number"), ("file","file")]
    kind = CharField(max_length=16, choices=KIND)
    name = CharField(max_length=128)                  # tool/assistant/squad name or file name or e164/id
    vapi_id = CharField(max_length=64, blank=True)    # the returned Vapi object id
    last_provision_hash = CharField(max_length=64, blank=True)  # sha256 of last-sent payload → zero-drift
    updated_at = DateTimeField(auto_now=True)
    class Meta:
        unique_together = [("kind", "name")]
```

- Assistants ALSO write back onto `AgentPrompt.vapi_assistant_id` (P4's publish reads it). `VapiObject` is the kind-agnostic map for tools/squad/files/phone.
- `last_provision_hash` is the **zero-drift oracle**: if `sha256(canonical_json(payload)) == last_provision_hash`, the reconcile is `nodrift` and **no PATCH is issued**.

### 4.7 `build_squad_payload()` → `POST/PATCH /squad` body (code-defined shape)

The destinations come from the **code-defined** `SQUAD_SHAPE` (`01-ARCHITECTURE.md` §1.6), never freely from the canvas. The canvas documents; code owns the runtime topology (`14-P4` §4.4 / B3).

```python
SQUAD_SHAPE = {
    "entry_router": [
        ("budtender",   "retail intent — looking for / recommend / what's good for…"),
        ("faq",         "info intent — hours / specials / returns / payment / pickup / location"),
        ("vendor",      "vendor / wholesale / delivery / manifest / dropping off"),
        ("escalation",  ">=2 human requests OR return dispute OR defective product"),
    ],
    "budtender":  [("escalation", "human request mid-flow")],
    "faq":        [("budtender", "cross-sell"), ("escalation", "dispute / human request")],
    "vendor":     [("escalation", "dispute / human request")],
    "escalation": [],   # terminal; warm transferCall out
}
```

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

> **The export's escalation orphan + empty transfer (bugs #3) is fixed HERE by construction:** `escalation` has real inbound edges (from entry_router/budtender/faq/vendor) in `SQUAD_SHAPE`, and its `transferCall.destinations` is populated from `HHT_TRANSFER_NUMBER_*` (§4.8). The provisioner literally cannot emit the orphan shape.

### 4.8 `transferCall` (built-in tool, inline on `vendor`/`escalation` `model.tools`) — warm + summaryPlan

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
              "content": "Summarize this call for the store operator before connecting. Include: store, what the caller wants, any defective-product/return details (WAC 314-55-079), and the disposition. Transcript: {{transcript}}" }
          ]
        }
      }
    }
  ]
}
```

- `number` is resolved from `AgentPrompt.transfer_number_key` → `settings.HHT_TRANSFER_NUMBER_<KEY>` (env). Unset (O-4) → the builder substitutes a documented placeholder `"+10000000000"` and the reconcile report carries `warnings=["transfer number not configured for <KEY>"]` — does NOT block the run (`14-P4` §4.3 behavior).
- `mode: "warm-transfer-wait-for-operator"` + `summaryPlan` injecting `{{transcript}}` (ADR-016) — the operator hears context before connecting.
- For the **vendor no-answer return-to-AI** flow (ADR-015), the `transferPlan` also carries the Vapi "on no answer / busy / failed → return to assistant" disposition (`fallbackPlan`/`destinations[].failureMessage`) so control returns to the `vendor` member, which then calls `notify_vendor_callback`. The exact Vapi field name for the no-answer fallback is pinned by the contract test in §8 (P3 owns the prose; this spec owns that the transfer payload includes the fallback-to-assistant path).

### 4.9 Phone-number attachment → `PATCH /phone-number/{id}`

```json
{ "squadId": "<Happy Time Voice squad id>",
  "assistantId": null,
  "name": "Happy Time inbound",
  "server": { "url": "${PUBLIC_BASE_URL}/api/voice/vapi", "secret": "${VAPI_WEBHOOK_SECRET}" } }
```
- `VAPI_PHONE_NUMBER_ID` (O-4) absent → step `skipped — VAPI_PHONE_NUMBER_ID not configured`; the Squad + assistants still provision (the number is attached later when the owner supplies it). One number fronts all 3 stores (intent-routed at `entry_router`); the per-store variant (one number per store) is an EXP item — the attach step is parameterized so a future per-store map is a config change, not code.

### 4.10 KB Files + Query Tool (delegated to `kb/vapi_files.py`)

```
ensure_files(): result = kb_vapi_files.mirror_all()
  → uploads ≤300KB markdown files (FAQ.md / return-policy.md / store-facts.md / wa-law.md /
    weights-types.md / education.md) via vapi.upload_file (idempotent: find_file_by_name → replace),
    creates/updates a Vapi Query Tool over those file ids,
    returns {"file_ids": [...], "query_tool_id": "<id>"}.
  → the provisioner records each file as a VapiObject(kind="file") and attaches query_tool_id to the faq assistant's toolIds.
  → VAPI Files API unavailable / VAPI_PRIVATE_KEY unset → mirror_all returns {"skipped": "..."}; reconcile reports "Vapi mirror skipped (not configured)".
```

### 4.11 `ProvisionReport` / per-object `ReconcileResult` (returned to the command + tests)

```json
{
  "ok": true,
  "dry_run": false,
  "results": [
    { "kind": "tool",      "name": "suggest_products", "id": "tool_...",  "action": "created|patched|nodrift|skipped|error",
      "changed_fields": ["function.parameters"], "warnings": [], "error": null },
    { "kind": "assistant", "name": "budtender",        "id": "asst_...",  "action": "nodrift", "changed_fields": [], "warnings": [], "error": null },
    { "kind": "squad",     "name": "Happy Time Voice", "id": "squad_...", "action": "patched",  "changed_fields": ["members"], "warnings": [], "error": null },
    { "kind": "phone_number", "name": "Happy Time inbound", "id": "", "action": "skipped", "warnings": ["VAPI_PHONE_NUMBER_ID not configured"], "error": null }
  ],
  "created": 0, "patched": 1, "nodrift": 4, "skipped": 1, "errors": 0
}
```
- `action="nodrift"` ⇔ payload hash == `last_provision_hash` ⇒ **no Vapi write issued.** A run with `created==0 and patched==0` is the **zero-drift no-op** (the headline acceptance criterion).
- `ok=false` iff any `action="error"`; the command exits non-zero. A single object's error is isolated (does NOT abort the others) — fail-loud per object (`14-P4` §5).

---

## 5. `core/services/vapi.py` — every method (the client contract)

`class VapiClient`. Constructed `VapiClient.from_settings()` (reads `VAPI_PRIVATE_KEY`, `VAPI_BASE_URL="https://api.vapi.ai"`, `VAPI_MAX_RETRIES`, `VAPI_TIMEOUT`, `dry_run`). One module-level `vapi = VapiClient.from_settings()`.

### 5.1 Auth + transport (cross-cutting)

- **Bearer auth:** a persistent `requests.Session` with `Authorization: Bearer <VAPI_PRIVATE_KEY>` + `Accept: application/json` + `User-Agent: happytime-voice/0.1` set once on the session (matches budtender's required-header discipline). The key is read from settings at construction; never logged.
- **`_request(method, path, *, params=None, json=None, idempotency_key=None)`** — the one funnel:
  1. If `dry_run` and `method != GET`: record `(method, path, redacted(json))` to `self.recorded_calls`, return a synthetic `{"id": f"dryrun-{path}"}` (so reconcile logic runs without writing). GETs still execute (read-only) unless a `dry_run_reads=False` is set.
  2. Issue the request with `timeout=VAPI_TIMEOUT` (default 20s).
  3. On `429`/`5xx`/`requests.ConnectionError`/`requests.Timeout`: back off `min(BASE * 2**attempt, CAP) + uniform_jitter`, honoring a `Retry-After` header if present; retry up to `VAPI_MAX_RETRIES` (default 4). `BASE=0.5s`, `CAP=8s`.
  4. On a final non-2xx: raise `VapiError(status, body, method, path)` (body truncated + **secret-redacted**).
  5. Return parsed JSON.
- **`idempotency_key`** — when Vapi supports an `Idempotency-Key` header on create, pass a stable key (`sha256(kind|name)`) so a retried POST after a network blip does not double-create. (If Vapi ignores it, the find-by-name reconcile is the backstop.)
- **Secret redaction:** `_redact(obj)` deep-copies any payload/body and replaces `server.secret`, any `Authorization`, `VAPI_PRIVATE_KEY`, `VAPI_WEBHOOK_SECRET`, `HHT_BACKEND_TOKEN` values with `"***"` before logging.
- **`_paginated(path, params)`** — yields items across pages (Vapi list endpoints; cap `VAPI_LIST_CAP=2000`).

### 5.2 Method inventory (signatures + behavior)

| Method | HTTP | Behavior |
|---|---|---|
| `auth_ok() -> bool` | `GET /assistant?limit=1` | `True` on 2xx; `False` on 401/403/transport (used by `core/views.healthz`). Never raises. |
| `list_assistants(**filters) -> list[dict]` | `GET /assistant` | Paginated. |
| `get_assistant(id) -> dict` | `GET /assistant/{id}` | Raises `VapiError(404)` if gone (reconcile catches → falls back to find-by-name). |
| `create_assistant(body) -> dict` | `POST /assistant` | Returns the created object (with `id`). |
| `patch_assistant(id, body) -> dict` | `PATCH /assistant/{id}` | Partial update; returns the updated object. |
| `delete_assistant(id) -> None` | `DELETE /assistant/{id}` | Used only by `--prune`. |
| `find_assistant_by_name(name) -> dict\|None` | `GET /assistant` + filter | The **idempotency primitive**: exact-name match; `None` if absent. |
| `list_squads` / `get_squad` / `create_squad` / `patch_squad` / `delete_squad` / `find_squad_by_name` | `/squad…` | Same contract as assistants. |
| `list_tools` / `get_tool` / `create_tool` / `patch_tool` / `delete_tool` / `find_tool_by_name` | `/tool…` | Same contract; `find_tool_by_name` matches `function.name`. |
| `list_phone_numbers` / `get_phone_number` / `patch_phone_number` / `find_phone_number(id_or_e164)` | `/phone-number…` | No create (the number is owner-provisioned in Vapi); only `PATCH` to attach `squadId`. |
| `list_files` / `upload_file(name, content, mime)` / `get_file` / `delete_file` / `find_file_by_name` | `/file…` | `upload_file` is multipart; idempotent via `find_file_by_name` → `delete_file` + re-upload (replace-by-name). Used by `kb/vapi_files.py`. |

- **No `/workflow` method exists on the client** (ADR-002) — a guard test asserts the string `"/workflow"` never appears in `core/services/vapi.py`.
- **`VapiError`** carries `.status`, `.body` (redacted), `.method`, `.path`; `str()` is a one-line redacted summary safe to log.

---

## 6. The reconcile engine + the deploy steps (`voice/provision.py`)

### 6.1 The generic reconcile (create-or-PATCH, by id then by name, zero-drift)

```
_reconcile(kind, name, payload, *, find_by_name, get_by_id, create, patch) -> ReconcileResult:
    h = sha256(canonical_json(redact_for_hash(payload)))   # exclude volatile fields from the hash
    rec = VapiObject.objects.filter(kind=kind, name=name).first()

    # 1) resolve the existing object
    obj = None
    if rec and rec.vapi_id:
        try:    obj = get_by_id(rec.vapi_id)               # stored id first
        except VapiError as e:
            if e.status == 404: obj = None                 # stale id → fall through to find-by-name
            else: raise
    if obj is None:
        obj = find_by_name(name)                           # find-by-name fallback

    # 2) zero-drift short-circuit
    if obj and rec and rec.last_provision_hash == h:
        return ReconcileResult(kind, name, obj["id"], action="nodrift")

    # 3) create or patch
    if obj is None:
        obj = create(payload)                              # the ONLY create path (no blind double-POST)
        action = "created"
    else:
        obj = patch(obj["id"], payload)
        action = "patched"

    # 4) write back the id + hash (idempotency memory)
    VapiObject.objects.update_or_create(
        kind=kind, name=name,
        defaults={"vapi_id": obj["id"], "last_provision_hash": h})
    return ReconcileResult(kind, name, obj["id"], action, changed_fields=diff(obj, payload))
```

**Why this is zero-drift on a re-run:** after run #1 every `VapiObject.last_provision_hash` == the current payload hash, so run #2 short-circuits at step 2 for every object → `created==0, patched==0` (A-IDEMP). An edit to an `AgentPrompt` body changes that member's hash → exactly one PATCH on the next run, nothing else.

### 6.2 `provision_all(...)` — the ordered deploy steps

```
provision_all(*, dry_run=False, only=None, members=None, prune=False) -> ProvisionReport:
    if not vapi.auth_ok():  return ProvisionReport(ok=False, error="VAPI_PRIVATE_KEY not configured")
    results = []

    # (1) TOOLS first — assistants reference tool ids.
    for name in _registered_tool_names(members):           # from TOOL_REGISTRY ∩ MEMBER_TOOLS
        results.append(ensure_tool(name))                  # build_tool_payload(name) → _reconcile(kind="tool")

    # (2) KB FILES + Query Tool (faq grounding fallback).
    if only in (None, "file"):
        results.append(ensure_files())                     # kb_vapi_files.mirror_all(); record files; capture query_tool_id

    # (3) ASSISTANTS — toolIds resolved from step 1 (+ the Query Tool id for faq).
    for role in _members_to_provision(members):            # subset of the 5; AgentPrompt rows that exist
        results.append(ensure_assistant(role))             # build_assistant_payload(role) → _reconcile(kind="assistant")
                                                           #   writes AgentPrompt.vapi_assistant_id too

    # (4) SQUAD — assistantDestinations from SQUAD_SHAPE (code), member ids from step 3.
    if only in (None, "squad"):
        results.append(ensure_squad())                     # build_squad_payload() → _reconcile(kind="squad"); writes VAPI_SQUAD_ID note

    # (5) PHONE NUMBER — attach squadId (graceful skip if O-4 unset).
    if only in (None, "phone"):
        results.append(ensure_phone_number())              # PATCH /phone-number/{VAPI_PHONE_NUMBER_ID} → squadId

    # (6) optional PRUNE — delete Vapi objects we own but no longer define (dangling members/tools).
    if prune:
        results += _prune_orphans(members)                 # delete_* only for VapiObjects no longer in the desired set

    return ProvisionReport.from_results(results, dry_run)
```

- **Ordering is mandatory:** tools → files → assistants → squad → phone. An assistant cannot resolve `toolIds` before the tools exist; the squad cannot reference `assistantId`s before the assistants exist; the phone cannot attach a `squadId` before the squad exists.
- **`--member <role>`** provisions just that assistant (+ its tools) and re-PATCHes the squad (cheap, idempotent) so its destinations stay consistent — the per-member publish path P4 uses.
- **`--prune`** is the only path that DELETEs; it deletes only Vapi objects recorded in `VapiObject` that are no longer in the desired set (never a foreign object). Default OFF (safe).
- **`--dry-run`** routes every write through the client's `dry_run` recorder; the report shows the planned `created`/`patched` actions and the recorded call list, issues zero real writes.

### 6.3 The single command — `python manage.py provision_vapi`

```
$ python manage.py provision_vapi
Provisioning Vapi stack "Happy Time Voice" …
  tool  faq_lookup ............... created   tool_a1
  tool  suggest_products ......... created   tool_b2
  tool  check_inventory .......... created   tool_c3
  tool  pair_upsell .............. created   tool_d4
  tool  notify_vendor_callback ... created   tool_e5
  file  (6 KB files) ............. mirrored  + query tool qt_f6
  asst  entry_router ............. created   asst_11
  asst  budtender ................ created   asst_22
  asst  faq ...................... created   asst_33
  asst  vendor ................... created   asst_44   (warn: HHT_TRANSFER_NUMBER_* uses placeholders)
  asst  escalation ............... created   asst_55   (warn: HHT_TRANSFER_NUMBER_* uses placeholders)
  squad Happy Time Voice ......... created   squad_99
  phone Happy Time inbound ....... skipped   (VAPI_PHONE_NUMBER_ID not configured)
Done: created 12, patched 0, nodrift 0, skipped 1, errors 0.

$ python manage.py provision_vapi          # immediate re-run
…
  asst  budtender ................ nodrift   asst_22
  squad Happy Time Voice ......... nodrift   squad_99
Done: created 0, patched 0, nodrift 12, skipped 1, errors 0.   ← ZERO DRIFT (acceptance A-IDEMP)
```

---

## 7. Acceptance criteria (testable, concrete)

Each is a concrete assertion. Tests run against a **mocked** `core/services/vapi.py` (record-and-replay) for unit/contract; a live sandbox key for the provisioning plane.

**A-IDEMP — one command, re-run is a no-op (the headline, ADR-003).**
- A1. A single `python manage.py provision_vapi` against an empty Vapi account creates exactly: 5 tools + N assistants (N = members defined, 5 in the full stack) + 1 squad + (the KB files + 1 query tool) + (phone attach iff `VAPI_PHONE_NUMBER_ID` set). Every `vapi_id` is written to `VapiObject` and each assistant's id to `AgentPrompt.vapi_assistant_id`.
- A2. **Re-running immediately issues ZERO `create_*` and ZERO `patch_*` calls** (assert the mock client's create+patch call count == 0 on the second run); the report is all `nodrift`/`skipped`. (Zero drift.)
- A3. Editing one `AgentPrompt.body` and re-running issues **exactly one** `patch_assistant` (that member) and zero creates; all other objects `nodrift`.

**B — client correctness (`core/services/vapi.py`).**
- B1. The client injects the Bearer header on every request; `auth_ok()` returns `False` (never raises) when `VAPI_PRIVATE_KEY` is unset or 401.
- B2. A 429 with `Retry-After: 1` is retried after ≥1s; a persistent 500 raises `VapiError` after `VAPI_MAX_RETRIES` attempts; a 404 on `get_assistant` raises `VapiError(status=404)` (so reconcile can catch it).
- B3. `VapiError.__str__()` and every log line are **secret-redacted** — no `VAPI_PRIVATE_KEY`/`VAPI_WEBHOOK_SECRET`/`server.secret` substring appears (grep-style assertion over captured logs).
- B4. The string `"/workflow"` does not appear anywhere in `core/services/vapi.py` (ADR-002 guard).
- B5. `dry_run=True` records create/patch/delete calls without issuing them; GETs still execute (or are stubbed); the recorded list matches the planned actions.

**C — payload shape (ADR-010/011).**
- C1. `build_assistant_payload(role)` for each of the 5 members emits `voice` (Cartesia sonic-3, `voiceId a3520a8f-226a-428d-9fcd-b0a4711a6829`, `emotion:["positivity:highest"]`), `transcriber` (Deepgram nova-3, `numerals:true`, the **33-term** keyterm list), and `model` (`openai`/`gpt-4.1-mini`) — **each exactly once** (no per-node dup). The keyterm list equals `DEEPGRAM_KEYTERMS` (33 entries) and appears once.
- C2. The shadowed `gpt-5.2-chat-latest` is NEVER emitted; every assistant `model.model == "gpt-4.1-mini"`.
- C3. `model.toolIds` resolves from `MEMBER_TOOLS[role]` → provisioned tool ids; a missing tool id makes the member `skipped` with `warnings=["tool not provisioned: <name>"]` and **no PATCH is sent** (no dangling toolId).
- C4. `vendor`/`escalation` payloads carry a `transferCall` with `transferPlan.mode == "warm-transfer-wait-for-operator"` + a `summaryPlan` injecting `{{transcript}}`, destination from `HHT_TRANSFER_NUMBER_<key>`; an unset number yields a placeholder + a `warnings` entry (not a block).
- C5. `server` = `${PUBLIC_BASE_URL}/api/voice/vapi` + `server.secret` from `VAPI_WEBHOOK_SECRET`; `serverMessages` == the 4-event list.

**D — squad shape (escalation orphan fixed by construction).**
- D1. `build_squad_payload()` `assistantDestinations` exactly equal `SQUAD_SHAPE` (code-defined): `entry_router → {budtender,faq,vendor,escalation}`, `budtender → {escalation}`, `faq → {budtender,escalation}`, `vendor → {escalation}`, `escalation → {}`.
- D2. `escalation` has **real inbound edges** (it is a destination of entry_router/budtender/faq/vendor) and a **populated** `transferCall.destinations` — the export's orphan + empty-destinations bug (#3) cannot be reproduced (assert no empty `destinations: []` on escalation/vendor when a transfer number is configured).

**E — tools + KB files.**
- E1. Each of the 5 custom tools provisions as a Vapi `function` tool with `server.url`/`secret` and its `TOOL_SPECS` parameter schema; `notify_vendor_callback` is `async:true`, the others `async:false`.
- E2. `ensure_files()` mirrors the KB (delegates to `kb/vapi_files.mirror_all`), records each file as a `VapiObject(kind="file")`, and attaches the returned Query Tool id to the `faq` assistant's `toolIds`. With Files unconfigured, it reports `skipped — Vapi mirror not configured` and the rest provisions.

**F — phone attach + graceful degradation.**
- F1. With `VAPI_PHONE_NUMBER_ID` set, `ensure_phone_number()` issues `PATCH /phone-number/{id}` with `{squadId}`; absent, it reports `skipped` and the run still succeeds (`ok=true`).
- F2. No owner placeholder (O-1/O-4) is required at import; the command runs and degrades per object (every placeholder read at call time).

**G — error isolation + exit code.**
- G1. A Vapi 4xx on tool N is captured in that `ReconcileResult.error`, the other tools/assistants still provision, and the command exits non-zero (because `errors>0`) with the failing object named — the run never partially crashes the whole stack silently.
- G2. `--dry-run` exits 0, writes nothing to Vapi, and prints the planned actions.

**H — security.**
- H1. No secret appears in the command output, the `ProvisionReport`, or any log (H3-equivalent of `14-P4`); `--verbose` redacts too.
- H2. The provisioner writes `server.secret = ${VAPI_WEBHOOK_SECRET}` into every assistant + tool `server` block, and `voice/signing.verify_signature` accepts exactly that scheme (round-trip contract test §8.2).

---

## 8. Test plan

Mirrors `03-CONVENTIONS.md` §5 planes (Unit · Contract · Provisioning · Manual). The **secret-redaction**, **zero-drift**, and **no-`/workflow`** tests are non-negotiable gates.

### 8.1 Unit (`pytest -m "not integration and not manual"`, SQLite-OK, no network — `core/services/vapi.py` HTTP layer mocked with `responses`/`respx`)

- `tests/test_vapi_client.py` — Bearer header injected (B1); retry/backoff on 429 (`Retry-After` honored) + 5xx → `VapiError` after max retries (B2); 404 surfaces as `VapiError(404)`; `auth_ok` never raises (B1); `dry_run` records-not-issues (B5); `_redact` strips secrets from bodies/logs (B3); **`"/workflow"` not present in the module** (B4).
- `tests/test_provision_payload.py` — `build_assistant_payload`/`build_squad_payload`/`build_tool_payload` exact shapes (§4.3/§4.5/§4.7) for all 5 members; voice/transcriber/model emitted ONCE each; `DEEPGRAM_KEYTERMS` == 33 terms and appears once (C1); no `gpt-5.2-chat-latest` (C2); transfer payload warm + summaryPlan (C4); `serverMessages` (C5); squad destinations == `SQUAD_SHAPE` and escalation is non-orphan with populated transfer (D1/D2).
- `tests/test_provision_idempotent.py` — `_reconcile` create→patch→nodrift transitions on the payload hash (A2/A3); a stale stored id (404) falls back to find-by-name then patches, never double-creates; the `nodrift` short-circuit issues no write.
- `tests/test_tool_resolution.py` — `MEMBER_TOOLS` → toolIds resolution; a missing tool id → member `skipped` + warning, no PATCH (C3); `notify_vendor_callback` is `async:true` (E1).

### 8.2 Contract (`pytest -m integration`, mocked `core/services/vapi.py` record/replay)

- `tests/test_provision_full_stack.py` — `provision_all()` on an empty mock account creates exactly 5 tools + 5 assistants + 1 squad + files/query-tool (+ phone iff configured); writes all ids back (A1). **Immediate re-run → 0 creates, 0 patches** (A2 — assert the mock's `create_*`+`patch_*` call count == 0). One `AgentPrompt` edit → exactly 1 `patch_assistant` (A3).
- `tests/test_provision_error_isolation.py` — a Vapi 4xx on one tool is isolated; the other objects provision; `ProvisionReport.ok == False`; the command exits non-zero (G1).
- `tests/test_secret_redaction.py` (**mandatory gate**) — capture all logs + the `ProvisionReport` JSON over a full run; assert no `VAPI_PRIVATE_KEY`/`VAPI_WEBHOOK_SECRET`/`server.secret` value substring anywhere (H1/B3).
- `tests/test_webhook_secret_roundtrip.py` (**mandatory gate**) — the `server.secret` the provisioner writes (from `VAPI_WEBHOOK_SECRET`) is exactly what `voice/signing.verify_signature` accepts; a tampered secret → 401 fail-closed (H2; pins the §3.4 / §4.3 contract the P0 webhook depends on).
- `tests/test_no_workflow.py` (**mandatory gate**) — neither `core/services/vapi.py` nor `voice/provision.py` references `/workflow` (ADR-002).

### 8.3 Provisioning (`python manage.py provision_vapi --dry-run`, then live against a sandbox `VAPI_PRIVATE_KEY`)

- `--dry-run` prints the planned create/patch list, issues zero writes, exits 0 (G2).
- Live sandbox run: the full stack stands up; **paste the per-object report**. Re-run live: **paste the all-`nodrift` report proving zero drift** (A-IDEMP). GET-back one assistant from Vapi and confirm voice=sonic-3 / transcriber=nova-3+keyterms / model=gpt-4.1-mini / toolIds resolved / `server.url`+`secret` set — **paste the GET-back JSON**.

### 8.4 Manual call script (the deploy's definition of done — paste evidence)

After a live `provision_vapi`, dial `VAPI_PHONE_NUMBER_ID` (O-4 placeholder; use the provisioned test number) and confirm:
1. The call is answered by `entry_router` (Koptza greeting, spoken 21+ confirm, **no** "peek at your ID", **no** literal `{{store_name}}`).
2. An FAQ question routes to `faq` and is grounded (proves the tool `server.url` + secret the provisioner wrote are live).
3. "Recommend something for sleep under $40" routes to `budtender` and the `suggest_products` tool fires (proves the toolIds resolved).
4. "Let me talk to a human" twice → `escalation` warm transfer with a summary (proves the populated transferPlan — the export's dead path, fixed).
5. Re-run `provision_vapi` → zero drift; the live call behavior is unchanged.

**Test-data discipline:** deterministic fixtures; expected payload shapes hand-authored, not generated by the code under test. The redaction, zero-drift, webhook-secret-roundtrip, and no-`/workflow` tests are mandatory gates. Coverage: ~90% diff coverage on `core/services/vapi.py` + `voice/provision.py` + the command.

---

## 9. Risks / open questions

| Risk / question | Impact | Mitigation / disposition |
|---|---|---|
| **Vapi payload schema drift** — the exact `voice`/`transcriber`/`transferPlan`/`server`/`serverMessages` key names may differ from the documented schema at build time. | Provision POST/PATCH 400s. | All shapes are centralized in `voice/provision.py::build_*_payload` + `voice/constants.py` (one place to fix); the §8.1 payload test pins them; a live `--dry-run`/sandbox run surfaces a 400 early. The builders are SHARED with P4 publish → one shape, two callers (`14-P4` §9). |
| **Vapi pagination/cursor shape** for list endpoints unknown until live. | `find_*_by_name` could miss an object on a large account → a double-create. | `_paginated` is the one place to adapt; `find_*_by_name` is exercised against a multi-page mock; the `VapiObject` stored-id path is the primary reconcile (find-by-name is the fallback), so pagination only matters on a first run with pre-existing objects. |
| **Idempotency key support** — Vapi may not honor `Idempotency-Key` on create. | A retried POST after a network blip could double-create. | The find-by-name reconcile is the backstop (a second create is preceded by a find that now matches → patch instead). The `idempotency_key` header is best-effort, not load-bearing. |
| **Webhook signature scheme** (header name; `server.secret` constant vs HMAC-over-body) may differ from the documented shape. | The fail-closed gate could reject valid calls. | The provisioner writes `server.secret`; `voice/signing.verify_signature` is the ONE verifier; §8.2 round-trips them. Implement the documented `server.secret` header first, HMAC-over-body second, both behind the one function — whatever Vapi sends, the contract test fixes it. |
| **Transfer-on-no-answer field name** (vendor return-to-AI, ADR-015) is Vapi-version-specific. | The vendor no-answer→callback path could not return control to the AI. | The transfer payload includes the documented fallback-to-assistant disposition; the exact field is pinned by a P3 contract test; this spec guarantees the transfer payload *carries* a no-answer fallback, P3 owns the prose/flow. |
| **`--prune` could delete a live object** if `VapiObject` drifts from reality. | Accidental teardown. | `--prune` is OFF by default, deletes ONLY objects recorded in `VapiObject` and no longer in the desired set, never a foreign object; a `--dry-run --prune` previews the deletes. |
| **Two provisioner names** (`tools/provision_vapi.py` from P0 vs `voice/provision.py` here). | Confusion / divergence. | This spec is authoritative: `voice/provision.py` + `manage.py provision_vapi` is THE module; `tools/provision_vapi.py` (if kept) is a 3-line shim to `provision_all()`; `make provision` runs the management command. Documented in §2. |
| **O-4 phone number / transfer numbers unset.** | Phone attach + transfers use placeholders. | Read at call time; per-object `skipped`/`warnings`; the Squad + assistants still stand up; flip on when the owner supplies the env (no code change). |
| **Per-node config could creep back** if a future edit copies a voice block into a prompt. | Token bloat + drift (export #7). | Voice/transcriber/model are member-level constants in `voice/constants.py`; the §8.1 "appears once" test is a gate on every change. |

---

## 10. Definition of done (this spec)

- `core/services/vapi.py` implements every §5 method (assistant/squad/tool/phone-number/file CRUD + `auth_ok`) with Bearer auth, retry/backoff, pagination, secret redaction, `VapiError`, and `dry_run`; no `/workflow` path.
- `voice/provision.py` defines the Squad + 5 assistants + all tools + KB Files/Query-tool + phone attach **as code**, reconciled by the §6.1 create-or-PATCH-by-id/by-name engine; `manage.py provision_vapi` is the single command.
- **`python manage.py provision_vapi` stands up the entire live Vapi stack from env, and an immediate re-run is a proven no-op (zero new objects, zero drift)** — demonstrated live (§8.3) with pasted reports + a GET-back of one assistant confirming sonic-3 / nova-3+keyterms / gpt-4.1-mini / resolved toolIds / server block.
- All §7 acceptance criteria pass with pasted output (`ruff check`, `ruff format --check`, the targeted `pytest`, `python manage.py check`, `makemigrations --check` for the `VapiObject` migration).
- The mandatory gates pass: secret-redaction, zero-drift, webhook-secret-roundtrip, no-`/workflow`.
- Docs updated in the SAME change: note `core/services/vapi.py` (full) + `voice/provision.py` + `VapiObject` + the `provision_vapi` command in `01-ARCHITECTURE.md §8`; confirm this file in any roadmap checklist that references the provisioner; record any new env (`VAPI_BASE_URL`, `VAPI_MAX_RETRIES`, `VAPI_TIMEOUT`, `VAPI_LIST_CAP`) in `03-CONVENTIONS.md §3.3`.

---

## 11. Source-file anchors (for the executor)

- **Vapi export (config to lift):** `C:\Users\vladi\Downloads\happy-time-voice-agent-(full-script)-(uploaded-via-json).json` — voice block L21–31; Deepgram nova-3 + 33-term keyterm list + `numerals:true` L32–72; per-node model `gpt-4.1-mini`/maxTokens 250/temp 0.3 L15–20; shadowed global `gpt-5.2-chat-latest` L3955; orphan `escalation` node L3537 + empty `transfer_call.destinations: []` L3628; `escalation → transfer_call` edge L3950; `globalPrompt` (distribute into per-member prompts) L3960.
- **swedish-bot (port idioms):** `C:\Users\vladi\OneDrive\Desktop\swedish-bot\core\services\gemini.py` (Session + fail-loud error shape), `core\constants.py` (model SoT), `dashboard\views.py` (`_clean_graph` fail-closed pattern the squad-shape re-assertion mirrors).
- **happytime-budtender (auth posture only):** `C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender\budtender\auth.py` (constant-time Bearer, fail-closed) — the same posture the Vapi client + webhook secret use; `serializers.py` `PUBLIC_PRODUCT_FIELDS` (the leak-safe shape the `suggest_products` tool schema documents, no cost/margin).
- **Foundation (binding):** `C:\happytime-voice\docs\plans\{00-MASTER-ROADMAP,01-ARCHITECTURE,02-DECISIONS,03-CONVENTIONS}.md`.
- **Phase docs this spec serves:** `10-P0-CHASSIS-FAQ.md` (defers the deep CRUD + Files API + signature scheme here; first provision run), `11-P1-DUTCHIE-SUGGESTIONS.md` / `12-P2-ESCALATION-TRANSFER-EMAIL.md` / `13-P3-VENDOR-ROUTING.md` (add members/tools the provisioner reconciles), `14-P4-dashboard-publish.md` (Publish-to-Vapi reuses `build_*_payload` + the client), `15-P5-polish-brand.md` (re-publish persona/cartridge/correction copy via the same idempotent path).
- **Research (contracts cited):** `_research-suggestion-engine.md` §5 (the budtender tool schemas behind `suggest_products`/`check_inventory`/`pair_upsell`), `_research-education-blogs.md` §10 (WA limits / WAC the KB Files carry).
