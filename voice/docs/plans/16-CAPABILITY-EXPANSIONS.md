# 16 — CAPABILITY EXPANSIONS — Happy Time Voice Agent (the "come up with more" backlog)

> **Status:** FOUNDATION/BACKLOG (authoritative for the EXP track). Written 2026-06-22.
> **Reads from:** `00-MASTER-ROADMAP.md` §2 (S7) + §3 (EXP), `01-ARCHITECTURE.md`, `02-DECISIONS.md`, `03-CONVENTIONS.md`, the merged synthesis brief, `_research-education-blogs.md`, `_research-suggestion-engine.md`.
> **Binds:** every EXP item below is gated on P0–P5 being live and MUST honor the locked decisions (ADR-001…020) — it never contradicts them. Where an item needs a new owner choice, it is flagged as an **Open-question**, not assumed.
>
> **What this doc is:** a *prioritized* backlog of ADDITIONAL high-value capabilities beyond the core 6 phases (P0–P5). Each entry carries a short design, an effort estimate, dependencies, the **existing components it reuses** (so agents don't re-invent), and acceptance criteria. This is the "more" — brainstormed broadly, then ranked by value ÷ effort with a reuse bias.
>
> **The non-negotiable rails every item inherits (do NOT re-litigate):**
> - **Leak-Guard (ADR-008):** cost/margin/velocity can NEVER appear in any spoken, SMS'd, emailed, or logged customer-facing surface. Everything customer-facing flows through budtender's `public_product` allowlist (`PUBLIC_PRODUCT_FIELDS`) + the voice-repo contract test.
> - **Numbers-Guard (ADR-012, conventions §1.5):** the LLM never originates a figure (price/limit/dose/hours/qty). Numbers come from KB rows or budtender responses; the model only phrases them.
> - **OTD pricing (ADR-009):** any price spoken/sent is out-the-door (tax-included).
> - **Single Squad, no Workflow (ADR-002); set voice/transcriber/model once per member (ADR-011); never call `/workflow` (conventions §2).**
> - **Per-store Dutchie keys live ONLY in budtender (ADR-004/019);** the voice repo never holds one. All inventory/ranking/history work proxies to budtender over Bearer HTTP.
> - **PII discipline (ADR-006):** raw caller numbers are never persisted — only the peppered `PHONE_HASH_PEPPER` SHA-256. Any outbound channel (SMS/win-back) needs a consent record and a re-resolvable number store that respects this.
> - **Security (ADR-019):** every inbound webhook HMAC-verified + fail-closed; constant-time compares; prod-fail-closed boot.
> - **Provisioning is idempotent code (ADR-003):** any new tool/assistant ships through `tools/provision_vapi.py` (GET-then-PATCH), never click-ops.

---

## 0. How to read the ranking

Each item is scored **Value (1–5)** × **inverse Effort** with a **Reuse** modifier (a "+" means it leans heavily on code that already exists in swedish-bot / budtender / this repo's P0–P5 surface, which both lowers effort and de-risks). The table in §1 is the ranked roadmap; §2 is the full design per item. **Tiers:**

- **Tier 1 — do first (high value, mostly reuse):** returning-caller greeting, daily-specials mention, SMS the cart, multi-store routing, daily owner digest, compliance guardrails. These are small deltas on P0–P4 surfaces.
- **Tier 2 — high value, moderate build:** loyalty-tier awareness, reserve-for-pickup hold, post-call CSAT, abandoned-cart follow-up, Spanish assistant, live staff whisper/barge-in.
- **Tier 3 — strategic, larger or owner-gated:** outbound win-back calls, web-chat fallback, budtender training mode, KB pgvector swap, analytics deepening, voicemail/after-hours capture.

Effort legend: **XS** ≤0.5 day · **S** ~1 day · **M** 2–4 days · **L** ~1 week · **XL** >1 week.

---

## 1. Ranked backlog (value ÷ effort, reuse-weighted)

| # | Capability | Tier | Value | Effort | Reuse | Net rank | Hard deps |
|---|---|---|---|---|---|---|---|
| **E1** | Returning-caller greeting by name + "want your usual?" | 1 | 5 | S | +++ | **1** | P1 (phone-hash, `resume-by-phone`) |
| **E2** | Proactive daily-specials mention (Flower Monday 30% …) | 1 | 4 | XS | +++ | **2** | P0 (KB), specials editor (P4) |
| **E3** | Compliance / safety guardrails (no medical claims, dosing floor) | 1 | 5 | S | ++ | **3** | P0 (`guardrails.py`, KB) |
| **E4** | SMS the cart / hold / pickup summary post-call | 1 | 5 | S | ++ | **4** | P1 (picks), P2 (eocr), Twilio |
| **E5** | Multi-store smart routing by area code / stated location | 1 | 4 | S | +++ | **5** | P0 (entry_router), P3 |
| **E6** | Daily owner digest email (calls, outcomes, top asks) | 1 | 4 | S | +++ | **6** | P2 (VoiceCall), sinks |
| **E7** | Loyalty-tier awareness (greeting + perk mention) | 2 | 4 | M | ++ | **7** | P1, budtender profile |
| **E8** | "Reserve for pickup" hold via Dutchie | 2 | 5 | L | + | **8** | P1, budtender (write path) |
| **E9** | Post-call CSAT survey (1-tap SMS or spoken) | 2 | 3 | S | ++ | **9** | E4 (SMS), P2 (eocr) |
| **E10** | Abandoned-cart / unfinished-call follow-up | 2 | 4 | M | ++ | **10** | P1, P2, E4 |
| **E11** | Spanish-language assistant (multilingual STT/TTS) | 2 | 4 | M | ++ | **11** | P0–P3 (clone Squad) |
| **E12** | Live staff whisper / barge-in (monitor + take over) | 2 | 3 | M | + | **12** | P2 (transfer), dashboard |
| **E13** | Outbound win-back calls for lapsed customers | 3 | 4 | L | ++ | **13** | E11 consent, budtender history, P1 |
| **E14** | Web-chat fallback (reuse swedish-bot widget) | 3 | 3 | M | +++ | **14** | budtender, KB |
| **E15** | Budtender training mode (sandbox + scorecard) | 3 | 2 | M | ++ | **15** | P4 dashboard, KB |
| **E16** | KB pgvector swap (scale retrieval) | 3 | 2 | S | +++ | **16** | P0 (`kb/semantic.py` seam) |
| **E17** | After-hours / voicemail intelligent capture | 3 | 3 | S | ++ | **17** | P0, P2 |
| **E18** | Analytics deepening (funnel, attach-rate, miss-log) | 3 | 3 | M | ++ | **18** | budtender `/analytics/summary`, P4 |
| **E19** | Order-status / "is my pickup ready?" lookup | 3 | 3 | M | + | **19** | budtender, Dutchie order read |
| **E20** | Proactive restock-alert opt-in ("text me when it's back") | 3 | 3 | M | ++ | **20** | E4, budtender stock sync |

> **Reuse legend:** `+++` = >70% is existing code (swedish-bot/budtender/P0–P5); `++` = ~half existing; `+` = mostly net-new with a reused seam. The ranking deliberately front-loads the `+++`/`++` items — Karpathy bias: ship the deltas on proven code first.

---

## 2. Full design per item

Each item: **Goal · Design · Reuses (real paths) · New code · Data contract · Vapi deploy · Effort · Acceptance · Risks/Open-qs.** Items honor §0 rails by construction.

---

### E1 — Returning-caller greeting by name + "want your usual?"  ★ Tier 1, rank 1

**Goal.** When a recognized caller dials in, `entry_router` greets them warmly ("Welcome back!") and the `budtender` member offers a one-tap reorder of their most-bought / most-recent item before any slot-filling — the single biggest conversion lever for a regular.

**Design.** P1 already resolves the caller via the peppered phone-hash → budtender `POST /api/v1/chat/resume-by-phone` → `profile_summary` (`{has_history, top_categories[], price_tier}` — non-PII). Extend the resume response consumption: when `has_history` is true, `entry_router` opens with "Welcome back to Happy Time!" (no name unless the owner opts into storing a first-name — see Open-q). The `budtender` member, on `has_history`, calls `suggest_products` with the caller's phone (flips to `W_KNOWN` taste-first) AND surfaces a "your usual" line built from the budtender `_why` personal hook ("your go-to {brand}" / "your usual {subcategory}"). If they say "yes, the usual," go straight to quantity/upsell. **Numbers-Guard:** the "usual" item and its OTD price come from budtender, never the LLM. **Leak-Guard:** still only `public_product` fields.

**Reuses.** budtender `chat/resume-by-phone` + `ranking.py::_why` personal hooks (`_research-suggestion-engine.md` §2.5, §3.4) + `_affinity_score` (brand/strain-type strongest); swedish-bot peppered phone-hash (`crm/` + `PHONE_HASH_PEPPER`, ADR-006); P1's `voice/budtender_client.py` + `voice/tools/suggest.py`. **No new ranking.**

**New code.** A thin "reorder" branch in `voice/tools/suggest.py` (a `resolve_usual()` helper that reads `profile_summary` + the top `times_bought` pick from a `suggest_products` call with the caller phone) and a greeting variant in the `entry_router` + `budtender` AgentPrompt rows. No new tool — reuse `suggest_products` with phone set.

**Data contract.** Existing `resume-by-phone` → `{has_history, top_categories[], price_tier}`; existing `products/search` with `phone` → taste-first `results[0]` carries the "your usual" `why_this`. Nothing new on the wire.

**Vapi deploy.** PATCH the `entry_router` + `budtender` assistant prompts (the greeting + the "want your usual?" behavior). No new tool/assistant. Idempotent via `provision_vapi.py`.

**Effort.** S. **Acceptance.** A test call from a number whose hash maps to a budtender profile → greeting includes "welcome back" and the first budtender turn offers the caller's actual most-bought item with a personal `why_this`; an anonymous caller gets the neutral greeting and margin-first picks (regression: `W_ANON` path unchanged). Contract test: no name spoken unless the owner enabled name storage.

**Risks/Open-qs.** **O-E1-name:** storing a first name violates the "hash-only" PII rule unless the owner explicitly opts in to a consented `Caller.first_name` field. Default: greet "welcome back" with NO name; name is an owner-gated add-on (consent + a non-hashed field, documented as a deviation from ADR-006 requiring an ADR). Mis-recognition risk (shared phone) → keep it soft ("want your usual, or start fresh?").

---

### E2 — Proactive daily-specials mention  ★ Tier 1, rank 2

**Goal.** The agent proactively name-drops today's running special at a natural moment ("Heads up — it's Flower Monday, 30% off all flower today"), driving the exact category the promo targets.

**Design.** The weekly specials are known house facts (research: Flower Monday 30% / Cyber Tuesday 30% online / Wax Wednesday 25% / Self-Care Thursday 25% / Happy Friday 30% online). Store them as `StoreFact`/specials KB rows (already part of the P0 seed + the P4 "specials/hours editor"). A tiny `voice/tools/faq.py` helper `todays_special(store)` returns the active special for the call's weekday (server computes the day — Numbers-Guard: the % comes from the KB row, not the LLM). `entry_router` mentions it once after the age confirm; `budtender` re-mentions it ONLY if the caller's category matches the promo category ("…and since you're after flower, today's Flower Monday gets you 30% off"). Online-only specials are flagged as "online order" so the agent doesn't imply an in-store discount.

**Reuses.** P0 KB (`StoreFact`/specials seed); P4 specials editor (ADR-014 dashboard expansion); `voice/tools/faq.py` (P0 faq tool surface); the day-of-week is server-side (no LLM number).

**New code.** `todays_special(store)` in `voice/tools/faq.py` (weekday → active special row); a specials KB schema field for `channel` (in-store vs online) + `active_days`. A one-line behavior in `entry_router`/`budtender` prompts.

**Data contract.** KB special row: `{title, store_scope, percent, category_scope, channel: "in_store"|"online", active_weekday}`. Tool returns `{special: {title, percent, category_scope, channel} | null}`.

**Vapi deploy.** Attach `todays_special` (or fold into `faq_lookup` as a query type) to `entry_router` + `budtender`. PATCH prompts. Idempotent.

**Effort.** XS. **Acceptance.** A Monday test call surfaces the Flower special once; a Wednesday call surfaces Wax Wednesday; an online-only special is spoken as "online order." The % always traces to a KB row (Numbers-Guard test). Editing the special in the dashboard changes the next call's mention with no redeploy.

**Risks/Open-qs.** Stale specials → the dashboard editor is the single source; never hardcode the % in a prompt. **O-E2:** confirm exact current specials + which are online-only before seeding (some may have changed).

---

### E3 — Compliance / safety guardrails (no medical claims, dosing floor)  ★ Tier 1, rank 3

**Goal.** Make the agent structurally incapable of giving medical advice, promising a strain-type outcome, or inventing a dose — the house editorial standard ("never invent a dosing number or a medical claim; cite the education page; stay conservative," research §1, §8) enforced in code, not just prose.

**Design.** Extend `voice/guardrails.py` (code-owned, P0) with voice-specific compliance rules that run on every tool result and call summary: (1) a **medical-claim refusal** path — if the caller asks "will this cure/treat X," the agent gives the conservative house line ("I can't give medical advice, but our education guide covers…") and offers to connect a human; (2) a **dosing floor** — any dose the agent states must come from the KB taxonomy (2.5 mg start / 5–10 mg piece / wait-2-hours / 100 mg WA pack max), never an LLM-originated number (Numbers-Guard); (3) a **strain-type honesty** rule — never promise "indica = couch-lock"; steer by terpene + reported effect (research §5); (4) **21+ / WA-limit gating** re-asserted (1 oz flower / 7 g concentrate / 16 oz solid edibles, research §10). These live in version-controlled Python and **cannot be deleted from the dashboard** (the `_clean_graph` fail-closed boundary, ADR-014).

**Reuses.** swedish-bot `chat/guardrails.py` pattern (fail-closed → escalate); P0 `voice/guardrails.py`; the KB dosing/limits taxonomy (`_research-education-blogs.md` §2, §9, §10); the `_clean_graph` guardrail-immutability boundary (ADR-014).

**New code.** Guardrail rules `refuse_medical_claim`, `dose_from_kb_only`, `strain_type_honesty`, `wa_limit_check` in `voice/guardrails.py` + a system-prompt "house rules" block seeded from research §8 into the `budtender`/`faq` AgentPrompts. Unit tests per rule.

**Data contract.** Guardrail input = the proposed agent response / tool result; output = pass | rewrite-to-conservative | escalate. Dosing numbers resolved from KB rows.

**Vapi deploy.** PATCH `budtender`/`faq`/`entry_router` prompts with the house-rules block; guardrails enforced server-side in the tool webhook before the result is returned.

**Effort.** S. **Acceptance.** Unit tests: "will this cure my anxiety?" → conservative refusal + education pointer, never a claim; "how much should I take?" → the KB 2.5 mg start line verbatim, never an invented mg; a request that would exceed a WA limit → the agent states the limit (from KB). A red-team prompt cannot get the agent to state a dose absent from the KB (Numbers-Guard contract test).

**Risks/Open-qs.** Over-refusal (annoying) vs under-refusal (liability) — tune the medical-claim trigger conservatively; default to "education pointer + offer human." This is partly covered by P0 already; E3 is the explicit, tested hardening pass.

---

### E4 — SMS the cart / hold / pickup summary post-call  ★ Tier 1, rank 4

**Goal.** After a suggestion call, text the caller a tidy summary (picks + OTD prices + store address + pickup-ready note + Dutchie menu link) so they can finish on their phone — closes the "I'll think about it" gap.

**Design.** On `end-of-call-report` with a `suggested` outcome (P2 eocr handler), if the caller consents on the call ("want me to text you that?"), fire an SMS via a new `crm/sinks.py` `SmsSink` (Twilio or the owner's existing n8n SMS gateway). The body is built from the `VoiceCall` record's stored picks (already `public_product`-safe — OTD prices only, no cost/margin) + the store `StoreFact` (address, pickup note) + the store's Dutchie menu URL. **PII:** the destination number is needed to SMS — store it as a short-lived, consented `Caller.sms_optin` record keyed to the hash, purged per a retention policy; never log the raw number in `VoiceCall`. **Numbers-Guard:** prices in the SMS come from the budtender response captured at call time, not regenerated.

**Reuses.** swedish-bot `crm/sinks.py` `dispatch` + sink pattern (DBSink/EmailSink/WebhookSink — add `SmsSink` alongside); P2 `voice/webhooks.py::end_of_call_report` + `VoiceCall` record; the OTD-safe picks already on the record (Leak-Guard intact); P0 `StoreFact` for address/menu link.

**New code.** `crm/sinks.py::SmsSink` (Twilio/n8n); a consent slot in the `budtender` flow ("text you a summary?"); a `Caller.sms_optin` consent field + retention purge; an SMS template builder in `voice/summarize.py`.

**Data contract.** SMS payload `{to (consented, transient), body: picks[]+prices_otd+store_addr+menu_url}`. Sink dispatch reuses the idempotent per-`(record, sink)` delivery pattern so a retry never double-texts.

**Vapi deploy.** No new Vapi tool needed (SMS fires server-side on eocr). Optionally a small `offer_sms_summary` tool if the consent must be an explicit in-call confirmation captured as a tool call.

**Effort.** S. **Acceptance.** A suggestion call where the caller says "yes text me" → exactly one SMS arrives with the spoken picks at OTD prices, store address, and the menu link; no cost/margin in the body (Leak-Guard test on the SMS payload); a caller who declines → no SMS, no stored number. Idempotent: a webhook retry sends zero extra texts.

**Risks/Open-qs.** **O-E4:** SMS gateway choice (Twilio direct vs the owner's n8n) + the consent/retention policy for the number (the only place a raw number touches the system — needs an ADR documenting the deviation from hash-only, scoped to consented transient SMS). TCPA/WA messaging consent compliance.

---

### E5 — Multi-store smart routing by area code / stated location  ★ Tier 1, rank 5

**Goal.** Route the caller to the right store's context (hours, transfer number, inventory) automatically — by stated location first, area-code heuristic second — instead of always defaulting to Yakima.

**Design.** `entry_router` resolves `store ∈ {yakima, mount-vernon, pullman}` early: (1) if the caller names a city/store, use it; (2) else infer a default from the caller's area code (509 → Yakima/Pullman region, 360 → Mt Vernon region) as a *soft* default the agent confirms ("Calling our Yakima store?"), never a hard assumption; (3) else `HHT_DEFAULT_STORE`. The resolved `store` then keys every downstream call: budtender `slots.store`, the transfer number (`HHT_TRANSFER_NUMBER_*`), the hours/specials KB scope, and the staff-alert email (`STAFF_ALERT_EMAIL_*`). This makes one Vapi number front all three stores (the O-4 "one number, intent-route" option) viable.

**Reuses.** P0 `entry_router` classification; the per-store env catalog (`HHT_TRANSFER_NUMBER_{YAKIMA,MTVERNON,PULLMAN}`, `STAFF_ALERT_EMAIL_*`, conventions §3.5/§3.7); budtender's `store` slug param (valid slugs `yakima`/`mount-vernon`/`pullman`, `_research-suggestion-engine.md` §5.1); P3 vendor transfer routing.

**New code.** A `resolve_store(caller_area_code, stated_location)` helper in `voice/tools/` (deterministic mapping table 509/360 → soft default) + a confirm-the-store turn in `entry_router`. Wire `store` through to the transfer-number + alert-email selection.

**Data contract.** Input `{stated_location?, caller_number_area_code}` → `{store, confidence, needs_confirm}`. Area-code map is a small code constant (allowed — generic, not per-entity brand/farm data).

**Vapi deploy.** PATCH `entry_router` to confirm/resolve store; per-phone-number assistant overrides hydrate `{{store_name}}`/hours/transfer-number (fixes export #11). Idempotent.

**Effort.** S. **Acceptance.** A caller from a 360 number → soft default Mt Vernon, confirmed; a caller who says "Pullman" → Pullman regardless of area code; the resolved store correctly scopes the budtender inventory, the transfer number, and the alert email. Ambiguous 509 (Yakima vs Pullman) → the agent asks. Default-to-Yakima only when nothing resolves.

**Risks/Open-qs.** Area code is a weak signal (mobile portability) → always confirm, never hard-route on it. Depends on O-4 (one-number-fronting-three vs one-per-store); E5 makes the one-number option clean.

---

### E6 — Daily owner digest email  ★ Tier 1, rank 6

**Goal.** A once-a-day email to the owner: call count, outcome breakdown (faq/suggested/vendor/escalation/defective), top product asks, escalations to review, vendor callbacks pending — the "what happened on the phones today" glance.

**Design.** A scheduled job (cron'd `manage.py` command per swedish-bot's no-Celery pattern, or the optional queue if added) aggregates the day's `VoiceCall` rows (durable, from P2) into a digest and sends via `crm/sinks.py::EmailSink` to `STAFF_ALERT_EMAIL` (+ per-store splits). All figures are counts over the durable log (Numbers-Guard: real rows, no LLM math); an optional one-paragraph Gemini summary of themes uses `core/services/gemini.py` but the LLM never originates a count. Leak-Guard: the digest lists product *names* asked for, never cost/margin.

**Reuses.** P2 `VoiceCall`/`Outcome` durable log; swedish-bot `crm/sinks.py::EmailSink` + the cron'd-command background pattern; `core/services/gemini.py` (lifted verbatim) for the optional theme summary; budtender `/analytics/summary` for merchandising counts if desired.

**New code.** `voice/management/commands/daily_digest.py` (aggregate + render + send); a digest HTML template; the cron entry in the deploy chassis.

**Data contract.** Digest model = day's `VoiceCall` aggregates `{n_calls, by_outcome{}, top_asks[], escalations[], pending_vendor_callbacks[]}`. Email via the idempotent sink.

**Vapi deploy.** None (server-side scheduled job).

**Effort.** S. **Acceptance.** Running the command for a day with seeded `VoiceCall` rows produces an email with correct counts per outcome, the top asked-for products by name, and the open vendor-callback/escalation queue; counts equal a hand-recompute over the fixtures; no cost/margin anywhere.

**Risks/Open-qs.** Timezone for "a day" (store-local); empty-day handling (send a "0 calls" or skip — owner pref, **O-E6**). Per-store vs single digest (reuse the `STAFF_ALERT_EMAIL_*` split, O-9).

---

### E7 — Loyalty-tier awareness  ★ Tier 2, rank 7

**Goal.** Recognize a high-value / loyalty-tier regular and acknowledge it ("As one of our regulars…") + surface a tier-appropriate perk, deepening the relationship without ever exposing internal numbers.

**Design.** budtender already derives a non-PII customer profile from Dutchie history (`total_orders`, `price_tier`, `top_categories`, novelty). Add a derived **tier label** (e.g. `regular`/`vip` from `total_orders` + recency thresholds — computed server-side in budtender or in a thin voice-repo classifier over `profile_summary`) and a KB-driven perk line per tier (the perk text/percent is a KB row → Numbers-Guard). The `entry_router`/`budtender` member speaks the tier acknowledgment + perk only when the caller is recognized. **Leak-Guard:** the tier is a coarse label, never a spend figure; OTD only.

**Reuses.** budtender `chat/resume-by-phone` `profile_summary` + `recompute_affinity` outputs (`total_orders`, `price_tier`, `_research-suggestion-engine.md` §3.3); P1 personalization path; P0 KB for the perk copy; the specials/perk editor (P4).

**New code.** A `tier_for(profile_summary)` deterministic classifier (thresholds as config, owner-tunable in the dashboard) + a per-tier perk KB schema + a greeting/perk behavior in the prompts. If the tier is computed in budtender, it's a small additive field on `profile_summary` (still non-PII).

**Data contract.** `profile_summary` gains `tier: "new"|"regular"|"vip"` (coarse, non-PII). KB perk row `{tier, perk_text, percent?, channel}`.

**Vapi deploy.** PATCH `entry_router`/`budtender` prompts for the tier acknowledgment. Idempotent.

**Effort.** M. **Acceptance.** A recognized high-`total_orders` caller hears a regular/VIP acknowledgment + the KB perk; a new caller hears neither; the tier label never carries a spend amount; perk % traces to a KB row. Thresholds editable in the dashboard.

**Risks/Open-qs.** **O-E7:** does Happy Time run a formal loyalty program (with real tiers/points), or is this a derived "regular" courtesy? If formal, source the tier from the loyalty system (likely via Dutchie/marketing_dashboard `loyalty` logs) rather than a derived heuristic — confirm before building. Avoid implying a discount the POS won't honor.

---

### E8 — "Reserve for pickup" hold via Dutchie  ★ Tier 2, rank 8

**Goal.** Let a caller actually reserve/hold the picks for in-store pickup ("I'll hold those two for you under your name for 24 hours"), converting the call into a near-sale.

**Design.** This is the highest-value, highest-friction item because it needs a **write path** to Dutchie, which today lives only in budtender and is read-only (`_pos_get`). Two options, owner-gated: (A) a true Dutchie order/hold API call (if the POS exposes a reservation/online-order create endpoint — needs verification; the marketing_dashboard memory notes Dutchie's POS API is largely read-only with limited write surface) added to budtender behind a new authenticated endpoint; or (B) a "soft hold" = log the reservation as a `VendorCallback`-style record + immediately email/Slack the store staff to physically pull and hold the items, with the caller given a pickup window and a confirmation SMS (E4). **Recommend B first** (no POS write risk, ships fast, owner-controllable), with A as a follow-on once the Dutchie write surface is confirmed. Leak-Guard/OTD intact; the hold record carries `public_product` picks only.

**Reuses.** budtender purchasability gate (`_is_purchasable`, MIN_STOCK=5 — never hold understocked items); P2 `VoiceCall` + `crm/sinks.py` staff alert (option B); E4 SMS confirmation; the P3 vendor-callback record pattern (a "hold request" is structurally the same durable record + staff alert).

**New code.** Option B: a `voice/tools/hold.py` `reserve_pickup(store, skus, pickup_window)` async tool → a `PickupHold` crm model + an immediate staff alert + an SMS confirmation. Option A (later): a budtender write endpoint + a new ADR (it breaks the "read-only" property).

**Data contract.** `reserve_pickup` → `{hold_id, store, skus[], pickup_window, status}`; staff alert email/Slack with the pull list (names + bin/qty, no cost).

**Vapi deploy.** Register `reserve_pickup` async tool on `budtender`; PATCH the prompt with the "want me to hold these?" close. Idempotent.

**Effort.** L (option B; A is XL + an ADR). **Acceptance.** A caller who accepts a hold → a `PickupHold` record + a staff alert listing the items to pull + an SMS to the caller with the window; the held items all passed the MIN_STOCK gate; no double-hold on webhook retry (idempotent). Leak-Guard on the hold record + SMS.

**Risks/Open-qs.** **O-E8:** does Dutchie's POS API expose any reservation/online-order *write*? The project memory (`dutchie-pos-api-no-adjust-write`) says the POS API is largely read-only — so option A may be impossible and B is the answer. Stock can sell out between hold and pickup → the soft hold + immediate staff pull mitigates. Requires an ADR if any Dutchie write is added (ADR-004/019 isolation).

---

### E9 — Post-call CSAT survey  ★ Tier 2, rank 9

**Goal.** Measure call quality: a one-tap SMS ("How did we do? Reply 1–5") or a single spoken end-of-call question, logged per `VoiceCall` for the owner digest + dashboard.

**Design.** Two modes: (1) **spoken** — `escalation`/`budtender`/`faq` end with "Before you go, how would you rate this call, 1 to 5?" captured as a tool call into the `VoiceCall` record; (2) **SMS** — on eocr, if the caller opted into SMS (E4), send a 1–5 reply prompt via `SmsSink`, with an inbound SMS webhook capturing the reply. Reuse budtender's existing `/feedback/` endpoint (phone hashed) as the durable store so CSAT lands in the same analytics surface. Prefer the SMS mode (non-intrusive) where consent exists, spoken otherwise.

**Reuses.** budtender `POST /api/v1/feedback/` (stores rating/message, phone hashed — `_research-suggestion-engine.md` §5.1); E4 `SmsSink` + the consent record; P2 `VoiceCall` (attach the score); P4 analytics view for the rollup.

**New code.** A `record_csat` tool (spoken mode) + an inbound-SMS reply handler (SMS mode) → budtender `/feedback/`; a CSAT field on `VoiceCall`; a dashboard tile.

**Data contract.** `record_csat → {call_id, score 1-5, comment?}` → budtender `/feedback/` (phone hashed). Inbound SMS reply parsed to a 1–5 int.

**Vapi deploy.** Register `record_csat` on the closing members; PATCH the closing prompt line. Idempotent.

**Effort.** S. **Acceptance.** A call ending with a spoken "4" → a `feedback` row + `VoiceCall.csat=4`; an SMS "5" reply → the same; non-numeric/no-reply → no score, no error; the daily digest (E6) shows the day's average. Phone is hashed in the feedback store.

**Risks/Open-qs.** Survey fatigue → cap frequency (don't survey a caller more than once/week). SMS mode depends on E4's consent/number plumbing.

---

### E10 — Abandoned-cart / unfinished-call follow-up  ★ Tier 2, rank 10

**Goal.** When a caller got recommendations but didn't commit (or the call dropped mid-flow), follow up later that day with a gentle SMS ("Those picks we talked about are still in stock — here's the menu"), recovering the near-miss.

**Design.** On eocr, classify the outcome: a `suggested`-but-not-`committed` call (or a `dropped` mid-flow with picks captured) flags an "abandoned" record. A scheduled job (later that day, owner-tuned delay) checks the picks are still in stock (budtender `products/in-stock/` / `check_inventory`) and, if the caller consented to SMS (E4), sends a single follow-up with the still-available picks at OTD + the menu link. **One follow-up only**, suppressed if the caller already returned/purchased (attribution via budtender's `SuggestedProduct → accepted` conversion signal, `_research-suggestion-engine.md` §3.2). Leak-Guard/Numbers-Guard intact.

**Reuses.** P2 `VoiceCall` + outcome classification; budtender `products/in-stock/` + `check_inventory` (re-validate stock) + the `SuggestedProduct`/`accepted` conversion attribution; E4 `SmsSink` + consent; the cron'd-command pattern.

**New code.** Outcome classifier `is_abandoned(call)` + a `voice/management/commands/followup_abandoned.py` scheduled job + the stock re-check + the suppression-if-converted check.

**Data contract.** Abandoned record `{call_id, hash, store, picks[], consented}`; follow-up gated on `in_stock && !already_purchased && consented`.

**Vapi deploy.** None (server-side scheduled). Relies on the eocr outcome field.

**Effort.** M. **Acceptance.** A `suggested`-not-committed consented call → exactly one follow-up SMS later, only for picks still in stock, suppressed if the caller purchased in between; a committed call → no follow-up; a non-consented call → no SMS. Idempotent (no duplicate follow-ups).

**Risks/Open-qs.** Annoyance/opt-out — strict one-shot + honor opt-out; consent required (E4). "Committed" detection on a phone call is fuzzy (no checkout) → lean on the budtender conversion signal + a conservative "did they say yes to a hold?" flag.

---

### E11 — Spanish-language assistant  ★ Tier 2, rank 11

**Goal.** Serve Spanish-speaking callers (a material share of the Yakima Valley population) end-to-end in Spanish — greeting, slot-filling, FAQ, transfer — at parity with English.

**Design.** Vapi supports multilingual voices/STT; Deepgram nova-3 and Cartesia sonic-3 both support Spanish. Approach: detect language at `entry_router` (the caller speaks Spanish, or presses/asks for Spanish) and hand off to a **parallel Spanish Squad member set** — OR use Vapi's per-assistant language config + Spanish AgentPrompt variants. Recommend **cloning the Squad's prompts into Spanish** (the AgentPrompt rows already store per-role editable bodies; add a `lang` dimension) and a Spanish voice/transcriber config on the Spanish members (set once per member, ADR-011). The KB needs Spanish FAQ/return-policy/limits rows (translate the seed; legal limits identical). budtender is language-agnostic (it returns structured data + `why_this`; the `why_this` strings would need Spanish phrasing — either translate `_why` outputs in the voice layer via Gemini, or pass through and let the assistant phrase them). Numbers-Guard/Leak-Guard unchanged.

**Reuses.** P0–P3 entire Squad (clone, don't rebuild); `AgentPrompt`/`FlowConfig` two-layer config (add `lang`); the Cartesia/Deepgram config seed (Spanish variants); budtender (unchanged — structured data); `core/services/gemini.py` for translating `why_this`/KB if not hand-authored.

**New code.** A `lang` field on `AgentPrompt` + Spanish prompt rows; Spanish KB rows (FAQ/policy/limits); Spanish voice/transcriber config; a language-detect/handoff rule in `entry_router`; optional `why_this` translation in `voice/summarize.py`.

**Data contract.** No wire change to budtender; the voice layer carries `lang` through the call context. KB gains language-scoped rows.

**Vapi deploy.** Provision Spanish assistant members (or per-assistant language config) + attach Spanish prompts/voice via `provision_vapi.py`; `entry_router` routes on detected language. Idempotent.

**Effort.** M. **Acceptance.** A Spanish-spoken call is handled fully in Spanish (greeting → slot-fill → grounded FAQ → transfer) with correct WA limits/dosing from Spanish KB rows; English calls unaffected; the Squad clone re-provisions drift-free. Leak/Numbers guards hold in Spanish.

**Risks/Open-qs.** `why_this`/KB translation quality (hand-author the high-traffic strings; Gemini for the long tail). **O-E11:** confirm owner wants Spanish (very likely high value in Yakima); confirm the transfer destination has Spanish-capable staff (else the Spanish path should set expectations).

---

### E12 — Live staff whisper / barge-in  ★ Tier 2, rank 12

**Goal.** Let a staffer monitor a live AI call from the dashboard and "whisper" (inject guidance the caller can't hear) or barge-in (take over) — a safety net for tricky calls and a training tool.

**Design.** Vapi exposes live-call control (listen/control via its live-call APIs / control URL on the call object). Add a **live call monitor** to the dashboard (ADR-014 already lists "live call monitor + call log" as an expansion): a server-sent stream of in-progress calls (from status-update webhooks) with a "listen"/"take over" action that uses Vapi's call-control surface to transfer to a staffer or inject a system message. Barge-in maps onto the existing warm-`transferCall` path (P2) — "take over" is a warm transfer to the monitoring staffer with the `{{transcript}}` summary. Whisper (inject without transferring) uses Vapi's say/control API if available; otherwise scope E12 to listen + take-over only.

**Reuses.** P2 warm `transferCall` + `summaryPlan` ({{transcript}}); P4 dashboard live-call-monitor expansion + the call log; `status-update` webhook stream; `core/services/vapi.py` (extend with call-control methods).

**New code.** Dashboard live-monitor view + SSE of active calls; `core/services/vapi.py` call-control methods (listen/say/transfer-to-staff); a "take over" action wired to the warm-transfer path.

**Data contract.** Active-call list from `status-update` events; control actions via Vapi call-control endpoints (verify exact surface).

**Vapi deploy.** No new assistant; uses live-call control on existing calls. The transfer-to-staff reuses P2 transfer config.

**Effort.** M. **Acceptance.** A staffer sees an in-progress call in the dashboard, clicks "take over," and the call warm-transfers to them with the transcript summary; "listen" streams the live transcript. (Whisper gated on Vapi support; document if scoped out.)

**Risks/Open-qs.** **O-E12:** confirm Vapi's live-call-control surface (listen/whisper/control URL) — barge-in via transfer is certain; silent whisper needs API verification. Latency/UX of the SSE monitor.

---

### E13 — Outbound win-back calls for lapsed customers  ★ Tier 3, rank 13

**Goal.** Proactively call (or text-first) lapsed regulars who haven't visited in N days with a personalized, consent-respecting "we miss you + here's a perk" outreach — the highest-revenue Tier-3 item.

**Design.** budtender's nightly history sync already computes `last_purchase_at`/`total_orders` per profile. A scheduled job selects lapsed-but-formerly-frequent profiles (e.g. `total_orders ≥ k AND last_purchase_at > N days`), and — **only for callers with prior outbound consent** — initiates a Vapi **outbound** call (Vapi supports outbound via the same Squad/assistant + a phone-number) or, safer/cheaper, an SMS-first win-back (E4 channel) that invites them back. The pitch is personalized from their taste profile (`W_KNOWN` picks) + a KB perk. **Consent + TCPA/WA messaging law is the gate** — no cold outbound; only to numbers with a recorded opt-in. Leak/Numbers guards hold.

**Reuses.** budtender history (`last_purchase_at`, `total_orders`, taste affinity) + `products/search` taste-first; E11/E4 channels; the existing Squad (outbound reuses the same assistants); `provision_vapi.py` (outbound call creation); the consent store (E4/E13).

**New code.** A `voice/management/commands/winback.py` selector + the outbound call/SMS initiation via `core/services/vapi.py` (outbound call create) + a strict consent gate + frequency cap + an outbound `VoiceCall` outcome.

**Data contract.** Lapsed cohort = budtender query `{consented, total_orders≥k, days_since_last>N}`; outbound call/SMS payload personalized from taste profile + KB perk.

**Vapi deploy.** Outbound call uses an outbound-capable phone number + the existing Squad/assistant; provisioned idempotently.

**Effort.** L. **Acceptance.** Only consented lapsed regulars are contacted, at most once per cap window; the outreach references their actual taste (taste-first picks) + a KB perk; opt-outs are honored immediately; no cost/margin spoken. A dry-run lists the cohort without dialing.

**Risks/Open-qs.** **O-E13 (legal):** TCPA/WA outbound-consent compliance is mandatory — build the consent ledger first; SMS-first is lower-risk than robo-calls. Brand risk (annoyance) → strict caps + easy opt-out. Likely a phased SMS-then-call rollout.

---

### E14 — Web-chat fallback (reuse swedish-bot widget)  ★ Tier 3, rank 14

**Goal.** A paste-on-site web chat that answers the same FAQ + gives the same budtender suggestions as the phone agent, for customers who'd rather type — reusing the swedish-bot embeddable widget.

**Design.** The roadmap (S7) already names this. swedish-bot ships `static/widget/nordland-widget.js` (vanilla-JS, CORS-allowlisted) + the SSE chat channel. Re-point the widget at a new `voice/`-adjacent web endpoint that reuses the SAME tool handlers (`faq_lookup`, `suggest_products`, `pair_upsell`) — the tool registry (ADR-020) makes this clean: the web channel calls the same `TOOL_REGISTRY` handlers the Vapi webhook does, just over a different transport (the chat `process_turn` pattern). KB grounding via the same `kb/` + embeddings. Leak/Numbers/OTD guards identical (same handlers). No telephony.

**Reuses.** swedish-bot `static/widget/nordland-widget.js` + `chat/views.py` SSE channel + CORS-allowlist middleware; this repo's `voice/tools/` registry (call the same handlers); `kb/` + `kb/semantic.py`; budtender client. Per the synthesis brief, the widget is "reusable for a future web chat fallback."

**New code.** A web-chat view that reuses `TOOL_REGISTRY` over SSE/HTTP; widget re-theming; a web `Conversation` session (anonymous public_id token, swedish-bot pattern). No new ranking/KB.

**Data contract.** Reuses the same tool request/response shapes; web session token = `Conversation.public_id`.

**Vapi deploy.** None (web channel, not Vapi). Shares the tool handlers + KB only.

**Effort.** M. **Acceptance.** The widget on a test page answers an FAQ from the KB and returns ≤3 leak-safe budtender picks with `why_this` + OTD prices + one gated upsell — identical logic to the phone path; no cost/margin in any web response (the same contract test).

**Risks/Open-qs.** Scope creep into a full web product — keep it a thin fallback reusing the handlers. CORS allowlist + rate-limit (swedish-bot pattern). Anonymous web = `W_ANON` margin-first (no phone) unless the user provides one.

---

### E15 — Budtender training mode (sandbox + scorecard)  ★ Tier 3, rank 15

**Goal.** A staff-facing practice surface: a new budtender role-plays calls against the AI (which plays the customer, or critiques the staffer's recommendations) and gets a scorecard — turning the agent's product expertise into onboarding.

**Design.** A dashboard "training" mode (P4 surface) that runs a sandboxed assistant variant: either (a) the AI plays a customer with a scripted need and the trainee responds, scored on whether they'd pick the budtender-ranked answer; or (b) the trainee proposes a recommendation and the AI critiques it against the budtender ranking + KB dosing/compliance rules (E3). Scoring is deterministic (did the trainee respect WA limits, dosing floor, no-medical-claims, and land near the ranked pick?). Reuses the entire stack read-only; writes a `TrainingSession` score. No customer impact, no Dutchie writes.

**Reuses.** P4 dashboard; the budtender ranking (as the "correct answer" oracle); E3 compliance guardrails (as the scoring rubric); the KB taxonomy; `core/services/gemini.py` for the role-play/critique. Read-only — no new ranking/KB.

**New code.** A training view + scripted scenarios + a deterministic scorer (limits/dosing/claims/pick-match) + a `TrainingSession` model.

**Data contract.** Scenario `{need, store, constraints}`; score `{wa_limit_ok, dose_ok, no_claim_ok, pick_distance}`.

**Vapi deploy.** Optional sandbox assistant (not on the live inbound number) for spoken practice; otherwise dashboard-text-only.

**Effort.** M. **Acceptance.** A trainee scenario produces a deterministic scorecard; a trainee who suggests an over-limit purchase or a medical claim is dinged; the "correct" pick matches the budtender ranking. No live-call or Dutchie-write side effects.

**Risks/Open-qs.** Lower direct revenue (internal tool) → Tier 3. **O-E15:** is staff onboarding a real owner pain worth the build, or is a doc enough?

---

### E16 — KB pgvector swap (scale retrieval)  ★ Tier 3, rank 16

**Goal.** When the KB grows past a few thousand chunks, swap the in-memory cosine retrieval for pgvector — the documented seam, isolated and low-risk.

**Design.** `kb/semantic.py` (ported from swedish-bot, ADR-013) already documents the pgvector swap-seam ("swap past a few thousand rows"). E16 is executing that seam: add a pgvector column + index, move the cosine query into the DB, keep the same `embed()` (768-dim Matryoshka) + the same retrieval interface so nothing upstream changes. Purely a performance/scale change behind a stable interface.

**Reuses.** swedish-bot `kb/semantic.py` + the documented seam (ADR-013); `core/services/gemini.py::embed`; the existing retrieval interface (callers unchanged). The synthesis brief calls this an EXP item explicitly.

**New code.** A pgvector migration + the DB-side cosine query swapped behind `semantic.search()`; a reindex command (the dashboard "reindex" button already exists, P4/ADR-014).

**Data contract.** Unchanged retrieval interface (`search(query) → top-k chunks`); storage moves to a pgvector column.

**Vapi deploy.** None (server-side retrieval).

**Effort.** S (the seam is pre-designed). **Acceptance.** Retrieval results match the in-memory implementation on a fixture set; query latency is bounded at N× the current corpus; the dashboard reindex repopulates the vectors; no interface change upstream.

**Risks/Open-qs.** Only needed at scale — premature before the KB is large (Karpathy: don't build until the corpus warrants it). pgvector infra dependency (a Postgres extension) added to the deploy chassis.

---

### E17 — After-hours / voicemail intelligent capture  ★ Tier 3, rank 17

**Goal.** When the stores are closed (or no human can take a transfer), the agent still captures the caller's need, states hours, and logs/alerts so staff can follow up — instead of a dead transfer.

**Design.** `entry_router` checks store hours (from the KB `StoreFact`, server-computed against the call time — Numbers-Guard) and, when closed, sets expectations ("We're closed now, open at 9 AM"), still answers FAQ + gives suggestions (budtender is 24/7), and for vendor/escalation paths that would transfer, captures the reason as a `VendorCallback`/escalation record + a staff alert with a "call back in the morning" window instead of a failed warm transfer. Essentially the vendor no-answer flow (P3) generalized to "store closed."

**Reuses.** P0 hours KB + P3 vendor no-answer→callback pattern + P2 `VoiceCall`/sinks; E5 store resolution; the existing escalation/vendor records (no new model — reuse the callback record with an `after_hours` reason).

**New code.** An `is_open(store, now)` helper over KB hours + an after-hours branch in `entry_router` that routes transfers to the capture-and-alert path.

**Data contract.** `is_open(store, now) → bool`; after-hours transfers become callback records (existing shape) with `reason="after_hours"`.

**Vapi deploy.** PATCH `entry_router`/`vendor`/`escalation` prompts to branch on hours. Idempotent.

**Effort.** S. **Acceptance.** A simulated after-hours call → hours stated, FAQ/suggestions still work, a would-be transfer becomes a logged callback + staff alert with a morning window (no dead transfer). Depends on resolved hours (O-8 for Mt Vernon).

**Risks/Open-qs.** O-8 (Mt Vernon hours conflict) must resolve before seeding those hours; until then, "call to confirm" stub.

---

### E18 — Analytics deepening (funnel, attach-rate, miss-log)  ★ Tier 3, rank 18

**Goal.** Beyond the daily digest: a dashboard analytics surface showing the suggestion funnel (asked → suggested → accepted), upsell attach-rate, top unmet asks ("we got asked for X but had no stock"), and CSAT trend — the merchandising/ops view.

**Design.** budtender already exposes `POST /api/v1/analytics/summary` (funnel/merchandising counts) and tracks `SuggestedProduct → accepted` conversion + hashed `track/` events. Surface these in the P4 dashboard analytics tab + add a **"miss log"**: when `suggest_products` returns `[]` (honest miss — a real size/type/DOH filter matched nothing), log the unmet ask so the owner sees demand they can't fill. Combine with the `VoiceCall` outcomes (E6) for a calls-side funnel. All counts are real (Numbers-Guard); no cost/margin.

**Reuses.** budtender `/analytics/summary` + `/track/` + the `accepted` conversion signal (`_research-suggestion-engine.md` §3.2, §5.1); P2 `VoiceCall` outcomes; P4 dashboard + the analytics expansion (ADR-014); E9 CSAT.

**New code.** A dashboard analytics view aggregating budtender summary + `VoiceCall` outcomes + a `MissLog` (empty-result asks) writer in `voice/tools/suggest.py`.

**Data contract.** Reads budtender `/analytics/summary`; `MissLog` row `{store, requested_slots, ts}` on empty results.

**Vapi deploy.** None.

**Effort.** M. **Acceptance.** The dashboard shows asked/suggested/accepted counts, upsell attach-rate, top unmet asks, and CSAT trend, all matching hand-recomputes over fixtures; an empty-result call writes a miss-log row; no cost/margin surfaced.

**Risks/Open-qs.** Attribution on a phone call (no checkout) leans on budtender's conversion signal — directional, not exact. Owner-value: high for merchandising, but downstream of the core build.

---

### E19 — Order-status / "is my pickup ready?" lookup  ★ Tier 3, rank 19

**Goal.** A caller asks "is my online order ready?" and the agent looks it up and answers — deflecting a high-volume staff-time call.

**Design.** Needs an order-read path. Dutchie's POS API exposes order/transaction reads (budtender already reads `/reporting/transactions`); a pickup-order-status read (if Dutchie's online-order/pickup status is queryable) would back a new budtender endpoint `order-status-by-phone(phone_hash)` → the caller's most recent open pickup order + its ready/not-ready state. The agent confirms identity loosely (phone-hash match) and states status, hours, and address. Read-only; no writes. If Dutchie can't expose pickup-order status via the POS API, this degrades to "let me connect you to the store" (a transfer).

**Reuses.** budtender Dutchie client (read pattern) + phone-hash identity; P0 store facts (address/hours); E5 store resolution; P2 transfer fallback.

**New code.** A budtender `order-status-by-phone` read endpoint (if Dutchie supports it) + a `check_order_status` voice tool + the prompt branch.

**Data contract.** `check_order_status(store, phone_hash) → {has_open_order, ready: bool, items_count, store_addr}` (no prices needed; no cost/margin).

**Vapi deploy.** Register `check_order_status` on `entry_router`/`faq`; PATCH prompt. Idempotent.

**Effort.** M. **Acceptance.** A caller with a recent online order hears its ready/not-ready status; no order → "I don't see one — want me to connect you?"; identity is phone-hash-loose with a human fallback for sensitive cases. (Gated on Dutchie pickup-status read availability.)

**Risks/Open-qs.** **O-E19:** does Dutchie's POS API expose pickup/online-order status? (The marketing_dashboard team has deep Dutchie API knowledge — verify there first.) Identity verification on a phone is weak → only reveal coarse status, never order contents/PII; transfer for anything sensitive.

---

### E20 — Proactive restock-alert opt-in  ★ Tier 3, rank 20

**Goal.** A caller wants something that's out of stock → "want me to text you when it's back?" → an opt-in restock alert fires when budtender's stock sync sees it return.

**Design.** When `suggest_products`/`check_inventory` returns out-of-stock for a specifically-requested item/brand, offer a restock alert. Store a consented `RestockWatch(phone_hash, store, sku/brand/category)`. budtender's inventory sync (every 10 min) is the trigger source — a small watcher compares new in-stock arrivals against open `RestockWatch` rows and fires an SMS (E4) when a match returns to the sales floor (MIN_STOCK gate). One alert per watch, then close it. Leak/Numbers/consent guards hold.

**Reuses.** budtender inventory sync + `products/in-stock/` + `_is_purchasable`/MIN_STOCK (`_research-suggestion-engine.md` §1, §7); E4 `SmsSink` + consent; the phone-hash identity.

**New code.** A `RestockWatch` model + an opt-in tool `watch_restock(store, sku_or_brand)` + a watcher job (cron'd or hooked to the sync) that matches returns-to-stock → SMS + close.

**Data contract.** `RestockWatch {hash, store, target_sku|brand|category, consented, status}`; fires when a matching purchasable item appears.

**Vapi deploy.** Register `watch_restock` on `budtender`; PATCH prompt. Idempotent.

**Effort.** M. **Acceptance.** A caller who opts into a restock watch on an OOS item gets exactly one SMS when it returns to the sales floor (passing MIN_STOCK), then the watch closes; no alert while still OOS; consent required; no cost/margin in the SMS.

**Risks/Open-qs.** Watch volume/noise → cap per caller; expire stale watches (e.g. 30 days). Depends on E4's SMS/consent plumbing.

---

## 3. Cross-cutting enablers (build these once; several items depend on them)

These are not standalone capabilities but shared substrate that multiple EXP items need. Build the enabler with the first item that requires it.

| Enabler | Needed by | What it is | First builder |
|---|---|---|---|
| **SMS channel + sink** (`crm/sinks.py::SmsSink` + inbound reply webhook + Twilio/n8n) | E4, E9, E10, E13, E20 | A leak-safe, idempotent SMS send/receive sink alongside the existing email/Slack sinks. | E4 |
| **Consent + transient-number ledger** (`Caller.sms_optin`/`outbound_optin` + retention purge + an ADR for the hash-only deviation) | E4, E9, E10, E13, E20 | The ONLY place a raw/usable number lives, consented + time-bounded; everything else stays hash-only (ADR-006). Requires a new ADR. | E4 |
| **Scheduled-job runner** (cron'd `manage.py` commands per swedish-bot, or a real queue) | E6, E10, E13, E20, E18 | Background aggregation/outreach. The synthesis brief flags that voice "will likely want a real queue for post-processing" — decide cron vs queue here. | E6 |
| **Store-resolution helper** (`resolve_store` / `is_open`) | E5, E17, E19, E20 | Deterministic store + hours resolution keying inventory/transfer/alert/hours. | E5 |
| **Outbound Vapi call creation** (`core/services/vapi.py` outbound + an outbound phone number) | E13 | The Vapi outbound-call surface, provisioned idempotently. | E13 |
| **Live-call control** (`core/services/vapi.py` listen/say/control) | E12 | Vapi live-call-control methods (verify surface). | E12 |

> **Sequencing tip:** E4 (SMS + consent ledger) unblocks five later items — build it early in the EXP track even though it's rank 4, because E9/E10/E13/E20 all wait on it. Same for the scheduled-job decision (E6) and store-resolution (E5).

---

## 4. What is explicitly NOT in scope (and why)

To keep the backlog honest (Karpathy: surface what we're deliberately not doing):

- **Payments over the phone** — Happy Time is cash/debit + on-site ATM (research §FAQ); no card-not-present, and cannabis payment rails are fraught. The agent reserves/holds (E8) but never charges.
- **Delivery** — WA law + house policy is pickup-only, no delivery (research §FAQ). The agent must never offer or imply delivery.
- **Re-implementing Dutchie/ranking in the voice repo** — forbidden by ADR-004; every inventory/history/order item proxies to budtender.
- **A Vapi Workflow-based anything** — ADR-002; the Squad is the only surface; we never touch `/workflow`.
- **Speaking or storing cost/margin in any channel** — ADR-008; the allowlist + contract test cover SMS/email/web/training surfaces too (every new channel inherits the Leak-Guard test).
- **LLM-originated numbers** anywhere (prices/limits/doses/perk %) — ADR-012/Numbers-Guard; all figures come from KB rows or budtender.
- **Cold outbound** (E13) without a consent ledger — legally gated; consent-first only.

---

## 5. Recommended EXP execution order (the "do this next" sequence)

A pragmatic ordering that front-loads value-per-day and respects the enabler dependencies:

1. **E2** (specials, XS) + **E5** (store routing, S) + **E3** (compliance guardrails, S) — three small, high-value deltas on P0–P3 surfaces, no new infra.
2. **E1** (returning-caller "want your usual?", S) — the conversion lever, on the P1 personalization path.
3. **E6** (daily digest, S) — first scheduled job; decide cron-vs-queue here.
4. **E4** (SMS + consent ledger, S) — the enabler that unblocks E9/E10/E13/E20; needs the consent ADR.
5. **E9** (CSAT, S) + **E10** (abandoned-cart follow-up, M) — ride E4's SMS/consent.
6. **E7** (loyalty-tier, M) + **E17** (after-hours capture, S) — once hours (O-8) + loyalty source (O-E7) confirmed.
7. **E11** (Spanish, M) — high Yakima-Valley value; clone the Squad.
8. **E8** (reserve-for-pickup, L) — soft-hold option B first; gate option A on the Dutchie write check (O-E8).
9. **E18** (analytics, M) + **E12** (whisper/barge-in, M) — ops/quality once volume justifies.
10. **E13** (win-back, L), **E14** (web-chat, M), **E19** (order-status, M), **E20** (restock alerts, M), **E15** (training, M), **E16** (pgvector, S) — strategic / owner-gated / scale-triggered, scheduled per owner priority.

---

## 6. Open-questions roll-up (owner decisions for the EXP track)

These extend the foundation's O-1…O-10 (do NOT block P0–P5 on them; resolve per EXP item as it's scheduled):

| Ref | Item | Question |
|---|---|---|
| **O-E1** | E1 | Store a consented first name (deviates from hash-only ADR-006) for "welcome back, {name}", or greet name-less? |
| **O-E2** | E2 | Confirm exact current weekly specials + which are online-only before seeding. |
| **O-E4** | E4 | SMS gateway (Twilio direct vs owner's n8n) + the consent/retention policy + ADR for the transient-number deviation. |
| **O-E6** | E6 | Digest timezone (store-local) + send-on-empty-day + per-store vs single. |
| **O-E7** | E7 | Formal loyalty program (real tiers/points via Dutchie/marketing_dashboard loyalty logs) vs a derived "regular" courtesy? |
| **O-E8** | E8 | Does Dutchie's POS API expose any reservation/online-order *write*? (Project memory says largely read-only → favor soft-hold B.) |
| **O-E11** | E11 | Confirm Spanish is wanted + that transfer destinations have Spanish-capable staff. |
| **O-E12** | E12 | Confirm Vapi's live-call-control surface (listen/whisper/control URL). |
| **O-E13** | E13 | TCPA/WA outbound-consent — build the consent ledger first; SMS-first vs robocall. |
| **O-E19** | E19 | Does Dutchie's POS API expose pickup/online-order status reads? (Check with the marketing_dashboard Dutchie-API team.) |

---

## 7. Source anchors (every claim traces to a real file)

- **Roadmap S7 / EXP framing:** `00-MASTER-ROADMAP.md` §2 (row S7), §3 (EXP), §5 (P5→EXP).
- **Architecture rails:** `01-ARCHITECTURE.md` §1 (Squad), §3 (budtender contract), §4 (KB plane), §5 (publish), §7 (security), §9 (placeholders).
- **Locked decisions:** `02-DECISIONS.md` ADR-001…020 (every rail in §0).
- **Conventions / env catalog:** `03-CONVENTIONS.md` §1.5 (Numbers/Leak-Guard), §2 (Vapi), §3 (env vars), §5 (testing planes).
- **Ranking / personalization / pairing / leak contract:** `_research-suggestion-engine.md` §0–§8 (W_ANON/W_KNOWN, `_why`, `resume-by-phone`, `PUBLIC_PRODUCT_FIELDS`, `/feedback/`, `/analytics/summary`, MIN_STOCK, sync cadence).
- **KB / dosing / compliance / specials / WA limits / store facts:** `_research-education-blogs.md` §1–§11.
- **Reusable code (paths):** swedish-bot `crm/sinks.py` (sinks/dispatch), `kb/semantic.py` (pgvector seam), `static/widget/nordland-widget.js` (web widget), `dashboard/views.py` + `templates/dashboard/flow.html` (canvas/_clean_graph), `core/services/gemini.py` (lift verbatim); budtender `ranking.py`/`pairing.py`/`serializers.py`/`dutchie.py`/`facets.py`/`auth.py` + `/api/v1/*` endpoints; this repo's P0–P5 surface (`voice/tools/` registry, `voice/webhooks.py`, `voice/guardrails.py`, `voice/models.VoiceCall`, `crm/models.VendorCallback`, `core/services/vapi.py`, `tools/provision_vapi.py`).

> **End rule:** every EXP item ships through the same gates as a phase doc — `ruff`/`pytest`/Leak-Guard/HMAC-fail-closed contract tests + a manual call (or SMS/web) script + docs updated in the same change. No EXP item starts without P0–P5 live; none contradicts ADR-001…020.
