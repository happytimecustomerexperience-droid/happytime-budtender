# Happy Time Voice

Production voice-agent stack for **Happy Time Weed** (cannabis retail — Yakima / Mt Vernon / Pullman, WA).
A Django control plane that drives a **Vapi Squad** of focused assistants: it answers FAQ/return-policy
questions grounded in a knowledge base, recommends in-stock products via the `happytime-budtender` ranking
engine (margin-first for unknown callers, taste-first for recognized ones), routes vendor calls, escalates
to a human with a warm transfer, and emails staff a durable record of every call — all editable from a
branded dashboard and auto-deployable to Vapi over the REST API.

> Built from the executable plan suite in [`docs/plans/`](docs/plans/) — start with
> [`00-MASTER-ROADMAP.md`](docs/plans/00-MASTER-ROADMAP.md) and [`02-DECISIONS.md`](docs/plans/02-DECISIONS.md).

## Architecture (one Squad, five assistants)

```
Vapi Squad "Happy Time Voice"
 ├─ entry_router   greets as "Koptza", classifies intent
 │    ├─ budtender   slot-fill + suggest_products / check_inventory / pair_upsell  → budtender service
 │    ├─ faq         faq_lookup over the KB (embeddings + keyword fallback)
 │    ├─ vendor      detect → warm transfer → on no-answer collect reason → VendorCallback + staff email
 │    └─ escalation  ≥2 human asks / dispute / defective-return → warm transfer + transcript summary
```

- **Control plane:** Django (`config/`, `core/`, `voice/`, `kb/`, `crm/`, `dashboard/`), forked from the
  `swedish-bot` chassis; `core/services/gemini.py` lifted verbatim (Vertex-preferred).
- **Data plane:** product suggestions call the separate **`happytime-budtender`** microservice over HTTP
  (Bearer); cost/margin can never be spoken (leak-guard + allowlist).
- **KB:** `kb/` models (FAQ / policy / store-facts / weights-types taxonomy / education / blog) with
  `gemini-embedding-2` 768-dim embeddings + cosine retrieval and a deterministic keyword fallback;
  mirrored to Vapi Files.
- **Deploy:** Vapi assistants/tools/squad are provisioned **as code** (`provision_vapi`, idempotent,
  zero-drift). Web + webhook served by gunicorn behind Caddy (`docker-compose.prod.yaml`).

## Quickstart (dev)

```bash
uv sync
cp .env.example .env            # fill secrets (see "Owner checklist")
uv run python manage.py migrate
uv run python manage.py seed_kb        # FAQ / returns / store-facts / WA limits / weights-types
uv run python manage.py runserver
# dashboard at http://localhost:8000/dashboard/  (admin-only)
```

Verify:

```bash
uv run ruff check
uv run python manage.py makemigrations --check
uv run pytest -q                # 363 tests, offline / key-free
```

## Provision the Vapi stack

```bash
uv run python manage.py provision_vapi --dry-run   # prints payloads, no API call (auto when key unset)
uv run python manage.py provision_vapi             # create-or-PATCH the Squad + assistants + tools (idempotent)
```

**One agent per store:** `provision_vapi --per-store` adds a squad per store (shared assistants; per-store
model override = store-lock line, that store's transfer number only, greeting with `{store_name}`) and
attaches each to its own number from `VAPI_PHONE_NUMBER_STORE_MAP`. It re-routes live calls — `--dry-run`
first, and unset `VAPI_PHONE_NUMBER_ID` so the legacy squad doesn't re-claim a number. Revert = PATCH the
number's `squadId` back to the legacy squad. A call on a mapped number always reads that store's data,
whatever store the model passes to a tool.

Re-running is a no-op (zero-drift, tracked by `VapiObject.last_provision_hash`). Edit prompts/model/voice
in the dashboard, then **Publish to Vapi** to PATCH the live assistants/squad.

## Owner checklist (fill before going live)

`.env` placeholders the owner supplies — see [`docs/plans/03-CONVENTIONS.md`](docs/plans/03-CONVENTIONS.md) §3:

- `VAPI_PRIVATE_KEY`, `VAPI_WEBHOOK_SECRET`, `VAPI_PHONE_NUMBER_ID`, `PUBLIC_BASE_URL` (HTTPS base Vapi calls back to).
- For multiple inbound Vapi numbers, set `VAPI_PHONE_NUMBER_STORE_MAP='{"pn_...":"yakima","pn_...":"mount-vernon","pn_...":"pullman"}'`.
- `HHT_BUDTENDER_BASE_URL` + `HHT_BACKEND_TOKEN` (must match the budtender service; the same token
  gates `POST /api/voice/kb/search` for the website chatbot's grounded RAG lookup).
- `GOOGLE_CLOUD_PROJECT` / `GOOGLE_APPLICATION_CREDENTIALS` (Vertex generation) and
  `GEMINI_API_KEY` (required for the canonical `gemini-embedding-2` embedding surface).
  Keep `GEMINI_EMBED_MODEL=gemini-embedding-2`; changing embedding spaces requires a full re-embed.
- Transfer numbers (`HHT_TRANSFER_NUMBER_*`) are pre-filled with the published store lines — confirm.
- **Dutchie POS keys live in the `happytime-budtender` service**, not here.
- `/healthz` reports sanitized dependency booleans for DB, Gemini, Vapi, and the internal budtender
  service (`ok` / `configured` only; no URLs, tokens, or exception strings).
- **Brand assets DEFERRED:** real hex/fonts/logo per [`brand/CAPTURE.md`](brand/CAPTURE.md) (blocked by the
  site's Vercel checkpoint — needs a manual browser capture).
- Store hours and June specials are seeded as confirmed KB facts; run `seed_kb` after monthly specials change.

## Dynamic greeting rollout (greet a returning caller by first name)

Off by default (`HHT_DYNAMIC_GREETING`). On, the phone line answers Vapi's `assistant-request` with a
per-call squad: the entry greeting says "Welcome back to Happy Time, <first name>!", every agent's prompt
ends with a code-built `CALLER` line (first name + taste; never the number), and a caller we have no
name for is asked once and saved to **our** budtender profile (never Dutchie). Code: `voice/caller.py`,
`webhooks.handle_assistant_request`, `provision.build_call_squad`. Offline tests: `test_caller_greeting.py`.

1. **Deploy budtender first**: migration `0011_customer_identity` + `POST /api/v1/customer/caller-context`
   and `/customer/profile-upsert` (backend token only). Voice never calls them with the flag off.
2. Set `HHT_DYNAMIC_GREETING=1` in voice's `.env` and restart. With several gunicorn workers and no
   Celery also set `HHT_CACHE_URL` (Redis), or each worker looks a caller up once (correct, just chattier).
   Go straight to steps 3-4: until `provision_vapi` has created the `remember_caller` tool, a dashboard
   Publish of the greeter/budtender is skipped with "tool not provisioned: remember_caller".
3. **`provision_vapi --dry-run` and read the diff.** Expect: `PATCH /phone-number/<id>` with
   `"squadId": null`; every assistant prompt ending in `{{caller_context}}`; the `remember_caller` tool on
   `entry_router` and `budtender`; nothing else changed; `assistant-request` is NOT in any `serverMessages`
   (Vapi rejects it there; it is sent to a number that has no squad/assistant).
4. `provision_vapi` (apply). From this moment every call on that number is answered by *our server*
   (Vapi gives it 7.5 s; the caller lookup is capped at 2.5 s): do it when the line is quiet. A lookup
   failure only costs the name; an unreachable server costs the call.
5. **One live test call from a known number and one from a new number.** Each costs Vapi minutes: the
   owner approves them. Known: greeted by name, the agent never reads the history aloud. New: standard
   greeting, asked for a name once, a second call from that number greets by name.
6. The name rule in `kb/seed.py` (`CALLER_NAME_RULE`) reaches live prompts only through `seed_kb`, which also
   resets dashboard prompt edits, or by pasting that paragraph into the entry_router and budtender prompts
   on the dashboard. The per-call `CALLER` line carries the same instruction, so the flow works without it.
7. **Rollback**: unset `HHT_DYNAMIC_GREETING`, restart, run `provision_vapi` (re-binds the squad, drops the
   `{{caller_context}}` block and the tool). **Kill switch for the name only**: dashboard Capabilities, "Greet
   callers by first name" (`call.greet_by_name`): instant, no re-provision; the standard greeting returns and
   no agent uses or asks for a name.

**Unverified (no Vapi call was made building this):** whether Vapi applies `membersOverrides.variableValues`
to every member across handoffs, and accepts a transient squad whose destinations name assistants by
`assistantName`; the real `assistant-request` latency; whether `PATCH /phone-number` with `squadId: null`
unbinds the number (the static payload already sends `assistantId: null`). A web/test-console call that
bypasses the number gets no `caller_context` variable. Settle each with the step-5 calls before relying on it.

## Price asks run through the questions (price gate + aroma slot)

A price is per **size**, so no price leaves the code without one. `suggest_products` / `check_inventory`
(`voice/tools/suggest.py`, `needs_size`) return **no price field of any kind** when the category is in
`constants.SIZE_REQUIRED_CATEGORIES` (flower, concentrate, cartridge, edible, tincture, pre-roll; a blank or
unknown category fails closed) and the call has no `size`; a `price_max` ceiling or `size: "any"` is not a
size. Such a result is `needs_size: true` + `size_options` (the sizes the shelf really has) + a
`spoken_summary` that asks the size; picks, names, THC, terpenes and lab facts stay. Both the phone agent and
the text brain (`voice/chat.py`) go through it, so the guarantee is code, not prompt: the text brain speaks that
question, reads the next turn ("an eighth") as the size of the same search, rewords the question once for
"just the price" and then offers a team member, and after a price is spoken asks one light scent question
(`AROMA_QUESTION`). The `aroma` slot (citrus | earthy | pine | floral | spicy) reaches budtender, which nudges
picks whose batch lab carries it; each pick's `profile_explain` (budtender's hedged `lab.profile.explain`) is
what the agent reads when asked what a pick smells like, as a *beta* feature, never a medical claim. Tests:
`test_suggest_price_gate.py`, `conversations/test_thread_24_price_asks_size.py`.

**Reaching the live assistants:** the `kb/seed.py` prompt changes (the `PRICE ASKS RUN THROUGH THE QUESTIONS`
rule, the flower SIZE and the AROMA questions, the LAB TALK paragraph) and the new tool schema (the `aroma` /
`size` / `category` parameters in `TOOL_SPECS`) only reach Vapi through `seed_kb` (which also resets dashboard
prompt edits) followed by `provision_vapi`, or by pasting the paragraphs into the dashboard prompts; the guided
Workflow also needs `provision_workflow` (59 nodes now). Until then the live agents keep the old
"quote a price straight from the tool" behaviour — but with the gate deployed in code, `suggest_products` already
returns no price for a size-less search, so the old prompt can no longer read one out.

## Status

P0–P5 code-complete and green (ruff + `manage.py check` + 363 offline tests). Not yet exercised against a
live Vapi number / real Dutchie keys / a real outbound transfer — that's the live-smoke step after the
owner checklist is filled.
