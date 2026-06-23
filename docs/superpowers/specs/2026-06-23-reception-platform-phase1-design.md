# Happy Time AI Receptionist — Design Spec (Phase 1)

**Date:** 2026-06-23 · **Status:** approved (brainstorm) · **Module:** `voice/` (evolve in place)

## 1. Goal
Turn the live Vapi voice agent into a polished, on-brand AI receptionist for Happy Time Weed
(Yakima / Mt Vernon / Pullman, WA): greets as **Happy Time**, asks the **location** first, runs the
rich per-category budtender consultation from the owner's workflow JSON, answers FAQ + return policy
from the real site content, routes vendors, escalates to the correct store, and emails staff.

## 2. Locked architecture decisions
1. **Engine = Vapi Squad** (Vapi's recommended path; already live on +1 509 852 8844). NOT Workflows —
   Vapi de-recommends them and the `/workflow` API is undocumented.
2. **The owner's JSON is the *conversation design source*** — its branches/steps/prompts are preserved
   1:1, but executed as squad assistant logic, and **owned in our Django DB** (source of truth).
3. **Editor = a Happy-Time-branded flow canvas we build** (Phase 2; swedish-bot's Alpine.js pattern)
   that compiles the graph → squad. Vapi only runs the squad.
4. **Home = `voice/`** — reuse KB, dashboard, CRM/email, vendor/escalation, budtender-client, webhook,
   tool handlers, guardrails. Only provisioning/prompts/seed change.

## 3. Phase 1 scope (this spec)
The "great live agent" milestone — 6 concrete changes, all building on what's live:
1. **Identity → "Happy Time"** (remove "Koptza" from every prompt + greeting).
2. **Location-first** — `entry_router` greets, captures **store** (Yakima/Mt Vernon/Pullman); the
   `store` slot drives per-store inventory (budtender already keys by store) and per-store transfer
   routing (`HHT_TRANSFER_NUMBER_*`). Works on the shared number now; auto-detect per-store numbers later.
3. **Port the JSON budtender flow** into the `budtender` assistant — the per-category consultation
   (flower / concentrate / cartridge / edibles / tinctures), each step (effect → activity → preferences
   /flavor/ratios → past-wins → explore → budget → select(+`check_inventory`) → quantity → cross-sell →
   checkout), dosing education, upsell lines. Tools: `suggest_products`, `check_inventory`, `pair_upsell`.
4. **Fix vendor/escalation transfer** — the `transferCall` block 400'd on provisioning; correct it to
   the current Vapi `transferPlan` schema and attach per-store destination numbers.
5. **Seed real KB** — 48 verbatim FAQs, return policy, and store facts (hours/address/phone/specials/
   loyalty/payment/compliance) from the website repo (`/data/faqs.json`, `/data/store-locations.json`,
   `/data/company-info.json`, `/data/deals.json`).
6. **Numbers/Returns guard** — agent never invents a return window, discount %, price, or stock; it
   states the verbatim policy and offers a human when ungrounded.

## 4. Conversation design (squad)
**Members:** `entry_router` → `budtender` → `faq` → `vendor` → `escalation`.

- **entry_router (start):** First message ≈ *"Thanks for calling Happy Time! Which store are you trying
  to reach — Yakima, Mount Vernon, or Pullman?"* Confirms 21+, sets `store`, then routes by intent
  (product → budtender; question → faq; vendor → vendor; human → escalation).
- **budtender:** runs the full JSON consultation for the chosen category; calls `suggest_products` /
  `check_inventory` / `pair_upsell` with `store`. Margin-first ranking + leak-guard already enforced
  server-side. Out-the-door pricing language; conservative dosing.
- **faq:** answers from KB (`faq_lookup`); keyword fallback if Gemini off. Never invents numbers.
- **vendor:** detect vendor caller → warm-transfer to the store; on no-answer → return to agent,
  collect reason → `notify_vendor_callback` (VendorCallback + staff email).
- **escalation:** de-escalate, resolve first; transfer to the **store's** line on repeat/dispute/
  defective-return; transcript summary to staff.

**`store` values:** `yakima` | `mount-vernon` | `pullman` (match budtender's slot + `HHT_TRANSFER_NUMBER_*`).

## 5. Tool/backend contract (reused unchanged)
Webhook `POST /api/voice/vapi`, HMAC-verified; `message.type="tool-calls"` dispatched by `function.name`
through `TOOL_REGISTRY`. Tools: `faq_lookup`, `suggest_products`, `check_inventory`, `pair_upsell`,
`notify_vendor_callback`. Squad and the (future) graph speak the same contract — **no webhook change.**

## 6. KB content to seed (verbatim, from website repo)
- **48 FAQs** → `FAQEntry` rows (category + Q + A verbatim).
- **Return policy** (verbatim): *"…we cannot accept returns once items leave our dispensary. However,
  if you experience issues… bring it back to the Happy Time location where you made your purchase…
  Bring your receipt and the product in its original packaging…"* — **no day-window stated.**
- **Store facts:** Yakima 1315 N 1st St / (509) 571-1106 / **8 AM–11:30 PM daily**; Mt Vernon
  200 Suzanne Ln / (360) 488-2923 / **Sun–Thu 9–10, Fri–Sat 9–11**; Pullman 5602 WA-270 /
  (509) 334-2788 / **9–10 daily**. Shared email `happytimeyak509@gmail.com`.
- **Specials/loyalty:** daily happy hours (30% off), Mt Vernon themed daily deals, Pullman monthly
  vendor deals; loyalty = Dutchie, phone-keyed, 1 pt/$1, never expire. (Dovetails with budtender's
  phone-keyed returning-caller profiles.)
- **Payment/compliance:** cash + debit only (no credit), ATM in-store; 21+ valid ID, everyone in party;
  no delivery (WA law); online reserve → in-store pickup (~15 min, held to EOD); RCW 69.50 limits.

## 7. Brand tokens (from `tailwind.config.ts`)
- Colors: amber `#FFB74D`, ember `#FF8A00`, coal bg `#0F1A24`, ink `#172331`, fog `#1F2C3B`,
  cream text `#E8EEF5`, sage `#9AB0C6`. Per-city accents: Yakima `#FFB74D`, Mt Vernon `#5EE0D0`,
  Pullman `#FF7A5C`.
- Fonts: **Inter** (body) + **Bebas Neue** (display). Logo `/media/logo-happy-time.png`.
- Persona: warm, community-first, value-savvy, "everyone knows your name"; compliance copy stays flat.

## 8. Provisioning changes (`voice/provision.py`, `constants.py`)
- Strip "Koptza"; identity = "Happy Time".
- `entry_router` prompt: greet + location capture + intent routing.
- `budtender` prompt: the JSON consultation (category-aware) + tool-use rules.
- Fix `_transfer_tool()` `transferPlan` to current Vapi schema; per-store destination from `store`.
- `SERVER_MESSAGES` already corrected (no `assistant-request`).

## 9. Testing
Offline/key-free unit tests stay green (mock Vapi/Gemini/budtender/SMTP). Add: location-routing test,
identity-has-no-Koptza test, KB-seed count test, transfer-payload schema test. Then provision dry-run +
live-call smoke on +1 509 852 8844.

## 10. Owner confirmations (agent must NOT state until confirmed)
- Exact **return window** (none on site) — agent says "bring it to the store of purchase with receipt +
  original packaging; the team will work with you within WA law."
- **Senior/veteran/birthday/first-visit discount %** (not in repo) — agent says "ask the budtender in store."

## 11. Later phases (own spec/plan each)
- **Phase 2 — owned branded canvas:** graph model in Django + Alpine flow canvas + KB management UI +
  brand theming + graph→squad compiler.
- **Phase 3 — receptionist depth:** deeper conflict-resolution/de-escalation playbooks, vendor
  hardening, analytics/reporting, optional per-store Vapi numbers (auto-detect store).
