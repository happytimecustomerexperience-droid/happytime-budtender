# 02 — DECISIONS — Happy Time Voice Agent (ADR log)

> **Status:** FOUNDATION (authoritative, binding). Written 2026-06-22.
> These are **locked** decisions. Every plan/phase doc MUST honor them and MUST NOT contradict them. Each entry: Decision · Rationale · Consequences. Open owner items are recorded at the end as env placeholders (do NOT block on them).

---

## Locked decisions (ADR-style)

### ADR-001 — Repo: new Django service at `C:\happytime-voice`, off OneDrive, forking swedish-bot
- **Decision:** A brand-new repo at `C:\happytime-voice` (NOT under OneDrive). It forks the **swedish-bot** chassis: lean single `config/settings.py`, env-driven, prod-fail-closed, Docker+Caddy, `uv`. `core/services/gemini.py` is **lifted verbatim**.
- **Rationale:** swedish-bot is a proven, minimal, prod-fail-closed Django AI chassis with the exact patterns we need (FSM orchestrator, KB-without-vector-DB, editable agent config, lead sinks, deploy chassis). OneDrive sync corrupts `.git`/`.venv` and causes file-lock races. Karpathy bias: reuse the working chassis, write only the net-new voice layer.
- **Consequences:** Apps = `core`, `voice` (new), `kb`, `crm`, `dashboard`. swedish-bot's `chat` (SSE web-chat) folds into `voice` (telephony). Gemini client unchanged → Vertex-preferred + API-key fallback + token accounting all come for free.

### ADR-002 — Vapi surface: Assistants + ONE Squad (NOT a Workflow)
- **Decision:** Build **one Squad** of saved Assistants (`entry_router`, `budtender`, `faq`, `vendor`, `escalation`). Do **NOT** use a Vapi Workflow.
- **Rationale:** Vapi's own June-2026 guidance: *"We no longer recommend Workflows for new builds. Prefer Assistants or Squads."* Large all-in-one workflow prompts cause more hallucination/tokens/latency. The `/workflow` CRUD endpoint is **live but undocumented/beta** (401 = route exists, absent from the public OpenAPI/llms.txt). Assistants/Squads/Tools/Phone-numbers are **fully-documented, stable CRUD** — the safe surface for a dashboard editor.
- **Consequences:** Smaller focused per-member prompts. The dashboard "Publish to Vapi" path uses `PATCH /assistant/{id}` + `PATCH /squad/{id}` only. We never call `/workflow`.

### ADR-003 — Everything auto-deployable via the documented Vapi REST API; provisioning is idempotent code
- **Decision:** Squad + assistants + tools + phone-number are **code-defined** and provisioned by an **idempotent, re-runnable script** (`tools/provision_vapi.py`) via `core/services/vapi.py`. Base `https://api.vapi.ai`, `Authorization: Bearer <VAPI_PRIVATE_KEY>`.
- **Rationale:** Reproducible infra; no click-ops drift; the dashboard publish action reuses the same client. Re-running must not duplicate Vapi objects.
- **Consequences:** GET-then-PATCH (never blind POST) when an object already exists; store each Vapi `assistantId`/`squadId`/`toolId`/`phoneNumberId` on the local row. A re-run yields zero drift (an acceptance criterion).

### ADR-004 — budtender reused as a SEPARATE HTTP microservice; voice repo never re-implements Dutchie/ranking
- **Decision:** Reuse **happytime-budtender** (`C:\Users\vladi\OneDrive\Desktop\MEsh\happytime-budtender`) unchanged as a separate microservice the voice repo calls over HTTP with `Authorization: Bearer <HHT_BACKEND_TOKEN>`. The voice repo NEVER re-implements Dutchie access or ranking.
- **Rationale:** budtender is ~90% of a voice budtender's backend — already containerized, voice-aware (`channel="voice"` reserved), leak-safe, low-latency (ranks over a pre-synced per-store table, not a live Dutchie call). Re-implementing it would duplicate the secret sauce and the leak guard.
- **Consequences:** Per-store Dutchie keys live ONLY in budtender. The voice repo holds `HHT_BUDTENDER_BASE_URL` + `HHT_BACKEND_TOKEN`. `voice/budtender_client.py` is a thin Bearer client.

### ADR-005 — Product suggestions use budtender's ranking engine: margin-first when UNKNOWN, taste-first when KNOWN
- **Decision:** All suggestions run through `budtender/ranking.py::rank_products`. When the caller is **UNKNOWN**, use `W_ANON` (margin 0.55) → **HIGH MARGIN priority** (owner emphasis). When the caller is **KNOWN**, use `W_KNOWN` (affinity 0.34) → **taste-first**, drawn from their real Dutchie purchase history.
- **Rationale:** Owner wants margin maximized for anonymous callers and personalization for regulars. budtender already encodes exactly this two-weight scheme; the final order is intentional (#1 highest gross-margin $, #2 highest velocity, #3+ real demand with brand-variety penalty).
- **Consequences:** The voice repo only consumes the order budtender returns; it does not re-rank. Every pick carries a speakable `why_this` built from real signals only (`_why()`), never invented.

### ADR-006 — Returning-caller recognition via swedish-bot's peppered phone-hash
- **Decision:** Recognize returning callers by a peppered SHA-256 of the caller number (`PHONE_HASH_PEPPER`, distinct from `SECRET_KEY`). A hash hit → budtender `chat/resume-by-phone` → their profile/history → `W_KNOWN`.
- **Rationale:** swedish-bot already ships this exact mechanism (`crm/` + `PHONE_HASH_PEPPER`). Storing only the hash keeps raw numbers out of the DB (PII discipline).
- **Consequences:** Raw caller numbers are never persisted. `PHONE_HASH_PEPPER` MUST differ from `SECRET_KEY` (prod-fail-closed checks this).

### ADR-007 — Upsell via pairing.py: ONE gated complement
- **Decision:** Upsells use `budtender/pairing.py::pair_for` — exactly ONE complementary, in-stock item, hard price gate ≤50% of anchor (`MAX_PAIR_PRICE_RATIO=0.50`), surfaced only when `strength∈[0,1]` clears the gate.
- **Rationale:** A single, genuinely-strong, significantly-cheaper add-on converts; a weak/expensive one annoys. budtender already encodes the complement ladder + strength gate.
- **Consequences:** `pair_upsell` may return nothing (silent) — that's correct, not a bug.

### ADR-008 — Leak-safe allowlist serializer: cost/margin can NEVER be spoken
- **Decision:** Every product reaching the agent comes through budtender's `serializers.public_product` allowlist (`PUBLIC_PRODUCT_FIELDS`); `cost`/`margin` are never referenced. The voice repo adds a defensive contract test asserting no "cost"/"margin" substring in any tool response.
- **Rationale:** Owner rule + budtender's existing `tests/test_no_leak.py`. The agent must be physically incapable of speaking cost/margin.
- **Consequences:** Two layers of guard (serializer + voice contract test).

### ADR-009 — Agent speaks OUT-THE-DOOR (tax-included) prices
- **Decision:** Whenever the agent quotes a price, it quotes **out-the-door (OTD, tax-included)** — what the customer pays — never pre-tax net.
- **Rationale:** Customer-facing voice; OTD is the only number a customer cares about. Consistent with the marketing_dashboard tax-inclusive customer-facing convention.
- **Consequences:** budtender returns `price_otd`; the voice repo speaks it. (Cost/margin remain blocked regardless — ADR-008.)

### ADR-010 — Model: gpt-4.1-mini for the Vapi assistants (single intentional model); server-side LLM uses Gemini/Vertex
- **Decision:** The Vapi assistants use **gpt-4.1-mini** for slot-filling/classification (a single, intentional model). Server-side LLM work (KB grounding, call summaries) uses swedish-bot's **Gemini/Vertex** client (`core/services/gemini.py`).
- **Rationale:** The export declared `gpt-5.2-chat-latest` at workflow level but every node overrode to `gpt-4.1-mini` → the workflow model never ran. Pick one intentionally; gpt-4.1-mini is cheap/fast and sufficient for slot-filling. Gemini stays for grounding/embeddings (already wired, Vertex residency, token accounting).
- **Consequences:** One model set once per assistant (no per-node override). Two LLM providers by design: gpt-4.1-mini (conversation) + Gemini (grounding/summaries/embeddings).

### ADR-011 — Voice/persona set ONCE at assistant level (no per-node duplication)
- **Decision:** Persona "Koptza"; Cartesia `sonic-3` (`voiceId a3520a8f-226a-428d-9fcd-b0a4711a6829`, `emotion: positivity:highest`); Deepgram `nova-3` with the ~33-term cannabis keyterm list. Set **once per assistant member**, never per node.
- **Rationale:** The export duplicated the identical voice/transcriber/model block across all 51 conversation nodes (~10× bloat + drift risk). Member-level config is DRY and PATCH-friendly.
- **Consequences:** A test asserts no per-node voice/transcriber/model duplication. The keyterm list is a single shared constant.

### ADR-012 — Knowledge base: seed ALL listed sources; canonical = Django kb/, mirrored to Vapi Files
- **Decision:** Seed the KB with ALL of: FAQ; return policy incl. **WAC 314-55-079** defective-product exception; store-facts (3 stores); WA purchase LIMITS; the FULL weights+types taxonomy (flower gram/eighth-3.5g/quarter-7g/half-14g/oz-28g, pre-rolls, concentrates 0.5g/1g, carts 0.5g/1g, edibles mg/10-serving-100mg WA packs, tinctures, THC:CBD ratios, solventless vs BHO, distillate vs live resin/rosin); `happytimeweed.com/education`; happytimeweed.com blog posts. Canonical store = Django `kb/` models (dashboard-editable, instant); mirrored to Vapi Files + a Query tool.
- **Rationale:** The agent must speak all weights/types/limits fluently; the export hand-waved "answer from the knowledge base" with no KB. Django-canonical means edits are live with no redeploy; the Vapi mirror gives a low-latency grounded fallback.
- **Consequences:** `kb/seed.py` is the seed source of truth. The agent must never originate a figure (Numbers-Guard) — limits/prices/hours come from KB rows.

### ADR-013 — KB retrieval uses swedish-bot's embeddings engine; pgvector swap-seam documented
- **Decision:** Semantic chunk retrieval uses swedish-bot's embeddings engine — Gemini `embed()` (768-dim Matryoshka) + `kb/semantic.py` cached cosine. The pgvector swap-seam is documented ("swap past a few thousand rows").
- **Rationale:** Proven, no extra infra at this KB scale; content-hash-keyed cache self-invalidates on edit.
- **Consequences:** No pgvector dependency now; the seam is documented so scaling is a known, isolated change (EXP item).

### ADR-014 — Dashboard: port the FULL swedish-bot dashboard AND expand it; flow canvas is config+docs only
- **Decision:** Port the full swedish-bot dashboard (agents editor, flow canvas, KB manager, agent-prompt-assist, CRM) AND expand it with: ranking-weights tuner, live call monitor + call log, vendor-callback queue, escalation review, KB-source manager (FAQ/education/blogs) + embeddings-reindex button, specials/hours editor, analytics, and a **"Publish to Vapi"** action. The flow canvas is config+docs ONLY; safety guardrails stay in version-controlled Python (`voice/guardrails.py`) and cannot be deleted from the UI (`_clean_graph` fail-closed: MAX_NODES=80, role allowlist, coord clamp).
- **Rationale:** The owner wants to manage everything from one console. The fail-closed boundary prevents an operator from deleting a safety rule via the canvas.
- **Consequences:** "Publish to Vapi" depends on all assistant/tool rows existing as code (P4 after P1–P3). Guardrails are never editable from the UI.

### ADR-015 — Vendor flow: detect at entry → warm transfer → on no-answer return to AI → capture reason → callback + alert
- **Decision (owner's exact flow):** At `entry_router`, detect vendor/wholesale/delivery/manifest callers → warm `transferCall` to the store human → if **NO ANSWER**, control returns to the AI → AI asks them to explain what they're calling about → log a `VendorCallback` + email/alert staff + state a callback window. Never drop a vendor into the retail budtender flow.
- **Rationale:** This owner runs returns/auto-return/vendor workflows; the same store number takes vendor calls. Vendors must reach a human first, with a reliable fallback that captures intent.
- **Consequences:** `vendor` member + `notify_vendor_callback` async tool + `crm/models.VendorCallback`. The transfer destination is a per-location env placeholder (O-4).

### ADR-016 — Escalation: fix the dead orphan; real inbound transitions + warm transfer with summary
- **Decision:** Wire real INBOUND transitions from `entry_router`/`budtender`/`faq` into an `escalation` assistant on (≥2 explicit human requests) OR (return dispute) OR (defective-product return). Warm `transferCall` (`transferPlan.mode = "warm-transfer-wait-for-operator"` + `summaryPlan` injecting `{{transcript}}`) to the per-location number.
- **Rationale:** The export's `escalation` had zero inbound edges (orphan) and `transfer_call.destinations: []` (empty) → human handoff was unreachable. Warm transfer with a transcript summary hands the operator context.
- **Consequences:** Escalation is reachable and testable. Destination is a per-location env placeholder (O-4).

### ADR-017 — Staff alerts: Vapi end-of-call-report → durable VoiceCall → email sink; Slack optional
- **Decision:** The Vapi `end-of-call-report` webhook writes a durable `VoiceCall` record, then an email sink fires to `happytimeyak509@gmail.com` (+ per-store env), with an **immediate** alert on escalation/vendor/defective-return outcomes. Slack is an optional secondary sink.
- **Rationale:** The export's record path was best-effort Slack-only → silent data loss. A durable DB record + email is reliable; Slack stays as a nice-to-have.
- **Consequences:** Reuse swedish-bot `crm/sinks.py` (DBSink always + EmailSink). `voice/models.VoiceCall` is the durable record. Slack behind an env flag.

### ADR-018 — Age/ID: drop "peek at your ID"; spoken 21+ confirm; KB carries 21+/limits
- **Decision:** A phone agent can't see ID — DROP the export's "take a peek at your ID" greeting. Use a spoken **"are you 21 or older?"** confirm. The KB carries 21+/limits.
- **Rationale:** The export's ID line was cosmetic and impossible on a phone channel.
- **Consequences:** `entry_router` greeting reworded. If a future kiosk/web channel needs a real age gate, that's an EXP item.

### ADR-019 — Security: HMAC-verified webhooks (fail-closed), constant-time compares, prod-fail-closed, per-store keys only in budtender
- **Decision:** Every Vapi webhook is HMAC/secret-verified and **fails closed**; all secret compares are constant-time (`hmac.compare_digest`); settings are prod-fail-closed (swedish-bot); per-store Dutchie keys live ONLY in budtender, never in the voice repo.
- **Rationale:** Standard for an inbound public webhook; matches swedish-bot's posture; keeps the high-value POS keys isolated.
- **Consequences:** `core/middleware.py` verifies the Vapi signature before any handler runs. The voice repo has no Dutchie key.

### ADR-020 — Parallel-safe tool layout: a `voice/tools/` package with a registry
- **Decision:** Tools live as one module per concern under `voice/tools/` (`faq.py`, `suggest.py`, `vendor.py`) with a dispatch registry in `voice/tools/__init__.py` that P0 ships.
- **Rationale:** P1/P2/P3 run in parallel worktrees and would otherwise all edit a single `voice/tools.py`, causing merge pain. A package lets each phase add its own file.
- **Consequences:** P0 must ship the registry scaffold. Each later tool registers itself; no shared-file edits across parallel phases.

### ADR-021 — Post-call background work on Celery (gated); the durable `VoiceCall` write stays SYNCHRONOUS in the webhook
- **Decision:** P5 may move post-call work (the Gemini call summary, the `crm/sinks.py` email/Slack alerts, the nightly analytics rollup) onto **Celery**, gated behind `HHT_USE_CELERY` (default `0` → inline, exactly the P2 behavior). The tasks live in `voice/tasks.py`; the app is wired in `core/celery.py` (ported from happytime-budtender's proven `core/celery.py`) over the existing Redis. **The durable `VoiceCall` write in `voice/webhooks.py::end_of_call_report` is NEVER queued — it stays synchronous and completes before the handler returns 200 to Vapi.** Only the summary/email/rollup are enqueued when the flag is on.
- **Rationale:** swedish-bot ships Redis-free, so Celery is genuinely net-new and must be optional — the system works inline. But under concurrent-call load, doing the Gemini summary + email inline in the webhook adds latency to the Vapi turn; moving them to a queue keeps the 200 fast. budtender already proves the exact Celery + Redis + idempotent-task pattern to port. The durable record is the one thing that must never be lost to a queue failure (ADR-017), so it stays synchronous — queueing it would reintroduce the export's "best-effort, silently dropped" record bug.
- **Consequences:** New files `core/celery.py` + `voice/tasks.py` (three idempotent tasks keyed on `voice_call_id`: `summarize_call`, `dispatch_alerts`, `rollup_analytics`); Redis worker + broker added to `docker-compose*.yaml`; `config/settings.py` adds `HHT_USE_CELERY`/`CELERY_BROKER_URL` (prod-fail-closed unchanged). With the flag off, behavior is byte-identical to P2 (inline). Immediate escalation/vendor/defective alerts may stay inline OR ride a high-priority queue (documented at build). This ADR is authored BEFORE P5 executes its §3.5 queue option (resolves reviewer gap G-9 / CN-2).

### ADR-022 — Returning-caller recognition: peppered phone-hash stored at rest; normalized phone sent to budtender over the secured channel; consent/PII boundary
- **Decision:** Recognize returning callers by the swedish-bot **peppered SHA-256 phone-hash** (`PHONE_HASH_PEPPER`, distinct from `SECRET_KEY` — ADR-006). **The voice repo persists ONLY the peppered hash** (`VoiceCall.caller_phone_hash`); the **raw caller number is never written to the voice DB.** To resolve a budtender profile, the voice repo sends the **normalized phone** (`+1XXXXXXXXXX`) to budtender over the already-secured server-to-server **Bearer + TLS** channel — the same pattern the website proxy uses today — because budtender's existing `CustomerProfile.phone` index keys on the normalized phone, not on the (one-way, pepper-specific) hash. budtender returns a non-PII `profile_summary` (`{has_history, top_categories[], price_tier}`) + a `session_token`; a hit drives `W_KNOWN` (taste-first), a miss drives `W_ANON` (margin-first). **This is "Option A" from `11-P1` §3.4.**
- **Rationale:** ADR-006's actual goal is *no reversible phone index at rest in the voice repo* — the peppered hash satisfies that. budtender already legitimately holds caller PII (transaction history), already receives the normalized phone from the website over the same Bearer/TLS channel, and indexes profiles by it; sending a peppered hash budtender doesn't store would resolve nothing. The raw number lives only in-transit (TLS) to a service that already holds it, and is never persisted by voice. Option B (budtender stores the same peppered hash, voice never sends a raw number) is the fallback ONLY if the owner mandates "voice never transmits a raw number" — it needs a budtender migration + the shared pepper, so it defers behind O-1.
- **Consequences:** `voice/recognition.py::resolve_caller` computes the hash (stored) and calls `budtender_client.resume_by_phone(phone=normalized)` (in-transit only). The `VoiceCall` row holds only the hash — a voice-DB leak never exposes a reversible phone index. No budtender change for v1. If the owner ever mandates Option B (or a stored consent ledger for SMS follow-up — EXP E4), that is a future ADR amending this one. **(This supersedes the placeholder "ADR-021" label used in `11-P1` §3.4/§9, which is renumbered to ADR-022 to avoid the Celery collision.)**

---

## Open owner items → env placeholders (do NOT block; ship against placeholders)

These are recorded so phase docs treat them as configuration, not blockers. Each becomes an entry in `.env.example` (see `03-CONVENTIONS.md`).

| Ref | Open item | Env placeholder(s) | Default / stub behavior until supplied |
|---|---|---|---|
| **O-1** | budtender deploy location + current per-store Dutchie keys | `HHT_BUDTENDER_BASE_URL`, `HHT_BACKEND_TOKEN` (keys themselves live in budtender) | Client ships against the contract; integration test uses a stub/recorded response. |
| **O-4** | Per-location transfer numbers + inbound number(s) (one fronting 3 stores vs one per store) | `VAPI_PHONE_NUMBER_ID`, `HHT_TRANSFER_NUMBER_YAKIMA`, `HHT_TRANSFER_NUMBER_MTVERNON`, `HHT_TRANSFER_NUMBER_PULLMAN` | Transfer code reads the env; a placeholder number is used in test; transfer is config, not code. |
| **O-8** | Mt Vernon hours conflict (`/mount-vernon` 9a–10p vs `/contact` 9a–11p) | (KB content) | Do NOT seed Mt Vernon hours until owner confirms; seed a "call to confirm" stub. |
| **O-9** | Staff alert routing — shared vs per-location email; keep Slack? | `STAFF_ALERT_EMAIL` (default `happytimeyak509@gmail.com`), `STAFF_ALERT_EMAIL_YAKIMA/MTVERNON/PULLMAN`, `SLACK_WEBHOOK_URL`, `SLACK_ALERTS_ENABLED=0` | Email to the shared address by default; Slack off until a webhook is supplied. |
| **O-10** | Brand visuals (logo/hex/fonts) — behind a Vercel checkpoint | (theming assets, P5) | Defer to a manual browser-capture pass in P5; dashboard ships with neutral theme. |

**Decisions already taken that resolve earlier open questions (for the record):**
- O-2 (Vapi surface) → **resolved by ADR-002** (Assistants + Squad).
- O-3 (model) → **resolved by ADR-010** (gpt-4.1-mini for assistants; Gemini server-side).
- O-5 (price quoting) → **resolved by ADR-009** (speak OTD).
- O-6 (vendor callback semantics) → **resolved by ADR-015** (warm transfer first; on no-answer, capture reason + log VendorCallback + alert + state window).
- O-7 (age/ID) → **resolved by ADR-018** (spoken 21+ confirm; drop "peek at ID").
