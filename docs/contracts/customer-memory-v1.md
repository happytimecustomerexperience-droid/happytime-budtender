# Customer memory v1 contract (budtender API <-> voice <-> website)

One source of truth: `CustomerProfile` in the budtender DB. Phone (Vapi) and website chat read the SAME short `brief`
and write learned facts through the SAME function. Nothing here is a medical claim: inferred "wellness-oriented" shopping
only steers ranking/pairing; the bot never says "medical", never diagnoses, never states effects as treatment
(LCB-CONTENT-COMPLIANCE.md; experiential words only, hedged).

## Trust tiers (who may see / write what)
`ChatSession.identity_via` decides the tier. Memory is PERSONAL data: a typed website phone is not proof of identity.
| tier | identity_via | READ brief | WRITE learned facts to profile |
|---|---|---|---|
| trusted | `caller_id` (Vapi carrier ID), `web_verified` (future SMS code) | full brief: first name, style, notes, taste, derived | merged into `profile.memory` |
| unverified | `web_phone` (typed) | `brief_public` ONLY: taste + derived lines, no name unless purchase-backed (existing identity.context rule), NO notes, NO conversation summary | NEVER merged into the profile; kept on `ChatSession.learned` and dropped with the session |
| anonymous | none | nothing | session-only |
So an attacker typing a victim's number gets product taste at most, can never read the victim's notes, and can never
poison the victim's memory. `HHT_WEB_PHONE_IDENTITY` stays as is.

## Storage (budtender/models.py, migration owner = backend-core agent)
- `CustomerProfile.memory` JSONField default dict, hard cap 4 KB serialized (trim oldest notes first).
- `CustomerProfile.memory_updated_at` DateTimeField null.
- `ChatSession.learned` JSONField default dict (same schema as `memory["learned"]`, unverified/session-only).

## `memory` schema (all keys optional; anything else is dropped on write)
```
{
 "v": 1,
 "style": {"length":"short|medium|long", "tone":"casual|neutral|formal", "emoji":bool, "pace":"quick|browse",
           "wants_explanations":bool},                        # <= 5 keys, enums only
 "notes": [{"t":"<=120 chars, third person, no PII", "at":"YYYY-MM-DD", "src":"voice|chat"}],   # <= 8, newest last
 "likes":   ["<=40 chars", ...],   # <= 8   things they said they like (e.g. "citrus terps", "1:1 gummies")
 "dislikes":["<=40 chars", ...],   # <= 8
 "context": ["<=60 chars", ...],   # <= 4   e.g. "buys for sleep routine" (experiential words only)
 "last_topics": ["<=40 chars", ...],  # <= 4
 "derived": { ...see below, computed from purchase_history by customer_model.py, never from chat text... }
}
```
`derived` (computed by `budtender/customer_model.py::compute_derived(profile) -> dict`, deterministic, no LLM):
```
{"ratio_pref":["1:1","2:1","20:1"],          # THC:CBD ratios they buy, most frequent first (names/lab)
 "cbd_lean": 0..1,                            # share of buys that are CBD-dominant or ratio products
 "forms": {"gummy":0.6,"chocolate":0.2,...},  # edible/other form shares
 "extraction": {"live-rosin":0.4,"distillate":0.3,"full-spectrum":0.2},
 "dose_mg": {"min":5,"p50":10,"max":20},      # edibles/tinctures
 "price_by_cat": {"flower":{"p10":..,"p50":..,"p90":..},...},   # per CATEGORY (not per size) observed unit prices
 "thc_by_cat": {"flower":{"p10":..,"p50":..,"p90":..},...},
 "cadence_days": 14, "days_since_last": 6, "due_for_reorder": bool,
 "pairings": {"accepted":["category|category"], "declined":[...]},
 "next_likely": ["edibles","flower"],         # ranked categories, from recency/cadence/co-purchase
 "confidence": "low|med|high"}                # by order count
```

## The brief (what both bots read; one builder, `budtender/memory.py`)
`brief(profile, tier) -> {"text": str, "style": {...}, "public": bool}`; `text` <= 600 chars, plain lines, e.g.
```
Name: Sam (returning, ~every 2 wks). Style: short, casual, no emoji, likes quick picks.
Usually buys: 1:1 and 2:1 gummies 10mg, live-rosin carts; mid price. Last: Verdelux 1:1 10pk (6d ago).
Likes: citrus terps. Said: new to concentrates, wary of strong stuff.
```
`public=True` (unverified tier) drops Name-if-unbacked, Said/notes, last-topics; keeps "Usually buys" and style only.
Consumers must treat `text` as DATA (strip control chars, cap length, never let it override rules).

## API additions (budtender, backend-token unless marked)
- `POST /api/v1/customer/caller-context` (existing, voice): response gains `brief`, `style`, `tier:"trusted"`.
- `POST /api/v1/customer/session-context` (existing, website token): response gains `brief_public` (text), `style`, never
  notes. Website browser still gets only `{known, first_name}`; `brief_public` stays server-side (prompt only).
- `POST /api/v1/customer/memory/learn` NEW, backend token only (voice at call end; also called internally for chat):
  `{call_id|session_token, transcript_user_turns:[str<=500 x<=40], channel}` -> deterministic extraction (+ optional Gemini
  summary behind `HHT_MEMORY_LLM`, strict JSON schema, allowlist, user turns only, transcript is untrusted) -> writes per tier rules.
- `POST /api/v1/customer/call-ids` `{customer_id, limit<=500}` -> `{ok, customer_id, total, call_ids:[Vapi call id]}`, backend token
  only (NOT `website_ok`): the ids parsed from that customer's `vc-<call id>` session tokens, never any other token. The voice
  dashboard joins them to its own `VoiceCall` rows for the customer page's conversation list.
- `POST /api/v1/customer/name-match` `{name}` -> `{ok, count, id}`, backend token only: how many live (not merged-away) customers
  carry EXACTLY that name (case/whitespace-insensitive, never a substring); `id` only when `count == 1`.
- Celery task `learn_from_session(session_id)` for website chats, fired when a session goes idle (>= 10 min) or on persist.
- `manage.py audit_customer --phone <E164> [--json]`: prints raw history stats vs `derived`, then runs `rank_products` per
  category and PASS/FAIL checks (price within p10-p90 band widened by tier, thc within band, ratio/form match, stock).
  Run on the VPS; this sandbox has no customer data.

## Rules for learning
- Learn only from the CUSTOMER's own turns. Never store: phone, address, DOB, ID numbers, payment, health conditions or
  diagnoses (drop the note instead), other people's names, anything an injection says to remember. Reuse the existing
  PII redaction (voice tests test_pii_redaction_dob_address.py) before anything is stored.
- A learned note must be a fact the customer stated or a style signal measured in code; the model may only phrase it.
- Conflicts: newer replaces older for the same likes/dislikes entry; `dislikes` beats `likes`.
- `forget`: `identity.unlink_session` / "forget me" clears `ChatSession.learned`; `POST /customer/memory/clear` (staff) wipes `profile.memory`.

## Summaries (AI conversation summaries, `budtender/memory_summary.py`)
Two more `memory` keys (allowlisted like the rest; same quarantine, plus no prices/dollar amounts):
```
 "summaries": [{"t":"<=240 chars, third person, experiential, no PII/health/prices", "at":"YYYY-MM-DD",
                "src":"voice|chat"}],     # newest last, hard ceiling 20
 "summary": "<=500 chars"                 # the ONE consolidated summary
```
- **Write.** After a call (`memory/learn`) or a website chat (`learn_from_session`) a Celery task
  (`summarize_conversation`, never a request) asks Gemini for ONE short factual summary of what the CUSTOMER
  said/wanted, from the customer's own turns only (memory_learn screening: instruction/health/PII turns dropped,
  "remember that..." and redacted turns never sent; delimited untrusted data). Strict JSON `{"t": "..."}`,
  temperature 0.1, max 150 output tokens, thinking OFF. The answer must clear the quarantine and be grounded in
  the customer's words, else nothing is stored. Idempotent per conversation (`ChatSession.learned.sdigest`; a
  resumed chat replaces its own entry via `skey`). Tier, re-read under the row lock: trusted -> profile;
  unverified -> `ChatSession.learned["summaries"]` only (dropped with the session); anonymous -> no call.
- **Consolidate.** When a profile holds `HHT_MEMORY_CONSOLIDATE_AT` (default 10) entries (or memory nears 3 KB
  with >= 2 entries) `consolidate_memory_summaries` (locked per customer, idempotent) sends the old `summary` +
  the entries to Gemini (thinking off, strict JSON `{"summary": "..."}`, durable preferences only, no new facts),
  validates it the same way, then atomically sets `summary` and removes only the consumed entries (entries
  added meanwhile stay). Any failure keeps every entry; a memory wiped meanwhile is never resurrected.
- **Read.** The trusted brief ends with one line `Remembers: <summary> <up to 2 newest entries>` inside the
  600-char cap (entries are cut first, then the summary at a word; the cap never moves). The unverified
  (`brief_public`) brief carries it only when `HHT_MEMORY_WEB_SUMMARIES` is on (default OFF: a typed website
  phone is not proof of identity). Consumers treat it as DATA and never recite it: personalisation is silent;
  `chat/message` replaces a reply that reads stored memory back (`memory.echoes`).
- **Forget.** `memory/clear` wipes `summary`/`summaries` with the rest; `forget`/unlink clear the session's.
- **Switches.** `HHT_MEMORY_SUMMARIES` (default on, needs `GEMINI_API_KEY`/`GOOGLE_API_KEY`), independent of
  `HHT_MEMORY_LLM`. Model `HHT_MEMORY_LLM_MODEL` (default `gemini-2.5-flash`): every call goes through
  `budtender/llm.py`, which sets `thinking_budget=0` (2.5 Flash/Flash-Lite) or `thinking_level=MINIMAL`
  (3 Flash) and refuses any model that cannot turn thinking off.
