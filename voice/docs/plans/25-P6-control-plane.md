# 25 — P6 — Full control plane: instant sync · Gemini · ElevenLabs · customers · tool logs

> Status: SHIPPED (2026-06-30). Builds on P4 (dashboard+publish). All changes are committed +
> green (423 tests). ADR-024 (below) overrides ADR-010's model pin per owner direction.

## What shipped

1. **Dashboard edits actually reach Vapi.** `provision.build_assistant_payload` was hardcoding the
   model/voice constants and IGNORING the saved `AgentPrompt` fields — so editing model/voice in the
   UI did nothing. Now the payload reads `model_provider` / `vapi_model` / `temperature` /
   `max_output_tokens` + a provider-aware voice block from the row (constants are the bare-tree
   fallback). New `AgentPrompt` fields: `model_provider`, `voice_provider`, `voice_settings` (JSON).

2. **Gemini 2.5 Flash is the default model** (ADR-024 — owner override of ADR-010 `gpt-4.1-mini`).
   Provider `google`, model `gemini-2.5-flash`. Editable per-role from the Agents page.

3. **ElevenLabs voice switch** from the Agents page: set Voice provider → `11labs`, a voice id, and
   `voice_settings` JSON knobs (stability, similarityBoost, style, useSpeakerBoost, model,
   optimizeStreamingLatency). Cartesia stays the default. (Verified shapes against the live Vapi
   OpenAPI spec.)

4. **Instant Vapi sync** — saving an assistant auto-publishes (PATCH /assistant + /squad) right
   away (`HHT_AUTO_PUBLISH`, on by default). Zero-drift hash keeps a no-edit re-save a cheap no-op.

5. **Credentials editor** (`/dashboard/credentials/`) — edit every secret/config live from the UI
   (Vapi key/secret/ids, budtender URL+token, transfer numbers, staff email, SMTP, n8n URL, Slack).
   A save applies to both `os.environ` and `settings.<name>` immediately — no redeploy.

6. **Tool calls are logged** (args + results) per call (`VoiceToolCall`, PII-masked + leak-scrubbed),
   shown on the call detail page. **`/full-conversation`** pulls the authoritative transcript +
   tool-call timeline from Vapi (`GET /call/{id}`) via the call-detail "Fetch full conversation"
   button or `manage.py full_conversation <call_id>`.

7. **n8n** — two directions: (a) set `N8N_WEBHOOK_URL` (credentials page) and every call POSTs a
   leak-safe event to your n8n webhook (`N8nSink`); (b) the bot-callable `notify_n8n` tool lets an
   assistant trigger an n8n workflow mid-call for a caller-requested follow-up (bind it to a bot
   from the Agents page — binding a new tool auto-provisions it on the next publish).

8. **Customer intelligence** (`/dashboard/customers/`) — rich per-customer profiles (RFM, spend,
   persona, category/tier affinities, favorite SKUs, shopping rhythm) + a personalized suggestion
   feed (favorite-replenish, basket cross-sell, tier-upgrade, cold-start). Browse, search, drill in.

## Owner steps (manual, one-time)

- **ElevenLabs / Google (Gemini) provider keys go in Vapi**, not here: Vapi → Settings →
  Integrations. There is no public Vapi credential API; Vapi resolves the key by provider. (Our
  Credentials page manages the keys *this app* uses.)
- **Import customers** (the export carries names, so it's owner-run, not committed):
  `uv run python manage.py import_customer_profiles --customers /path/to/customers.json`
  (optionally `--limit 500`). For basket cross-sell on the profile feed, set
  `BASKETS_JSON_PATH=/path/to/baskets.json`.
- **Re-seed prompts** to pick up the Gemini/voice defaults on an existing DB:
  `uv run python manage.py seed_kb`, then **Publish to Vapi** (or just save a prompt — auto-publish).
- A Django-settings credential changed from the UI is live immediately; a process restart re-reads
  `.env` first, then re-applies DB credentials on the first request.

## Verification
- 434 offline tests green; `ruff` clean; `makemigrations --check` clean; `manage.py check` clean.
- Booted locally on SQLite (`HHT_SQLITE_PATH`), imported 400 real customer profiles, and confirmed
  every new page renders 200 with real data (agents, credentials, customers + a real profile, call
  detail with tool calls).
- A 20-agent adversarial review of the P6 diff produced 12 confirmed findings (all medium/low),
  all fixed — chiefly **PII-redaction parity** (the transcript was masked, but the fetched summary,
  tool-call results, the n8n outbound summary, and the eocr transcript path now share one masker)
  and a units→TypeError crash on the customer page. See commit 8116936.

## ADR-024 — Vapi assistant model = Gemini 2.5 Flash (supersedes ADR-010)
Owner direction (2026-06-30): the squad assistants run **google / gemini-2.5-flash**, editable
per-role from the dashboard. ADR-010 pinned `gpt-4.1-mini`; this supersedes it. The server-side LLM
(prompt-assist, embeddings, summary) stays Gemini via Vertex as before.
