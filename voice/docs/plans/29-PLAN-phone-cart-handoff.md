# Phone Cart Handoff Plan

## Goal

Let the voice agent help a caller shop like a budtender: answer product availability, specials, policies, add requested items to a staged phone cart, quote the current estimated total and discounts, then hand that draft to the POS website after hangup.

The voice service must not submit, reserve, hold, or save a Dutchie order. Only the POS website/register flow may write to Dutchie through the existing `cart_submit` path.

## Current Repo Facts

- `voice` already logs calls, turns, and tool invocations in `VoiceCall`, `VoiceTurn`, and `VoiceToolCall`.
- Vapi custom tools already route through `voice.tools.dispatch`, then write redacted tool-call audit rows.
- The budtender API is bearer-gated by `HHT_BACKEND_TOKEN` and already exposes product search, SKU lookup, pairing, chat persistence, and customer profile endpoints.
- POS cart lines are session-owned. `cart_add` re-resolves the item server-side, checks live price when possible, and never trusts client price/batch/serial fields.
- `cart_submit` is the Dutchie write boundary. It calls `submit_cart`, records `DutchieWriteAudit`, tracks checkout, and clears the register session.

## Non-Negotiable Boundaries

- Voice can stage intent only: product, SKU, quantity, store, quote snapshot, source tool call, and transcript link.
- Voice cannot call `cart_submit`, `submit_cart`, Dutchie write APIs, or any endpoint that creates a shipment/order.
- Phone cart drafts expire and are revalidated when POS staff loads them.
- The final total is confirmed by POS/register. Voice may quote the current estimate with the source and timestamp.
- Customer age/ID and WA compliance remain in-store/register responsibilities.
- If product, discount, or policy data is not grounded, the agent must say it cannot confirm and offer staff escalation.

## Minimal Architecture

### 1. POS-Owned Draft Model

Add one model in the POS/budtender side, not in `voice`:

`PhoneCartDraft`

Core fields:

- `draft_token`: random staff-visible lookup token.
- `call_id`: Vapi call id or text-session id.
- `session_token`: website/agent session when available.
- `location_slug`: store.
- `phone_hash`, `phone_last4`: lookup without storing raw phone in voice.
- `pickup_name`: optional caller-provided name.
- `status`: `open`, `released`, `claimed`, `expired`, `cancelled`.
- `lines`: JSON list of staged products.
- `quote`: JSON estimate: subtotal, discounts, total, currency, source, generated_at.
- `audit`: JSON compact list of agent actions and upstream API call summaries.
- `expires_at`, `released_at`, `claimed_at`.

Use JSON for lines/audit first. A line table is only worth it after staff need reporting by line item.

### 2. Budtender API Endpoints

Bearer-gated, server-to-server:

- `POST /api/v1/phone-cart/upsert`
  - Creates or updates a draft for a call/session.
  - Adds, updates, or removes lines.
  - Recomputes quote from the same public product/price fields voice can see.

- `POST /api/v1/phone-cart/release`
  - Marks the draft `released` at hangup.
  - Does not write Dutchie and does not reserve inventory.

- `POST /api/v1/phone-cart/claim`
  - POS website only.
  - Staff claims by phone scan/lookup/token.
  - Revalidates each SKU against current store inventory and price.
  - Loads valid lines into the normal POS session cart, then staff uses existing `cart_submit`.

### 3. Voice Tool

Add one new Vapi/text-shared tool:

`stage_phone_cart`

Allowed actions:

- `add_item`
- `remove_item`
- `set_quantity`
- `quote`
- `release`

The handler only calls the budtender phone-cart endpoints. It never imports POS clients or Dutchie clients.

### 4. Conversation Behavior

The agent can move across topics in one call:

1. Availability: use `suggest_products` or `check_inventory`.
2. Specials/discounts: use `faq_lookup` for published specials and `stage_phone_cart quote` for line-level estimates.
3. Policies: use `faq_lookup`; if missing, say it cannot confirm and offer staff.
4. Cart changes: use `stage_phone_cart`.
5. Hangup: release the draft if any cart lines exist.

Voice phrasing should be explicit: “I can stage this for the register, but the store will confirm ID, availability, discounts, and final total when you check out.”

## Tests

### Unit Tests

- `stage_phone_cart` refuses unknown actions and missing SKU/quantity.
- `stage_phone_cart` calls only budtender endpoints and returns graceful failure if budtender is down.
- Phone quote includes source/timestamp and never exposes cost/margin.
- Release marks draft `released` but creates no `DutchieWriteAudit`.

### POS/Budtender Tests

- Upsert creates a draft with product lines and compact audit entries.
- Claim revalidates live SKU availability before loading POS session cart.
- Out-of-stock/price-changed lines are flagged for staff, not silently submitted.
- `cart_submit` remains the only test path that creates `DutchieWriteAudit(action="submit")`.

### Realistic Conversation Tests

Script a continuous caller:

1. “Do you have 1g carts under $35?”
2. “Any discounts today?”
3. “What is the return policy if it is defective?”
4. “Add two of the first one.”
5. “What is my total after discounts?”
6. “Actually remove one.”
7. “Can I pick it up later?”
8. End call.

Assertions:

- Product questions use product tools.
- Specials and policy use grounded KB sources.
- Cart operations use `stage_phone_cart`.
- Hangup releases the draft.
- No Dutchie shipment/order is created.
- POS claim can load the draft into a normal register cart.

### Manual Acceptance

Run the same script against text chat and Vapi voice. Answers should match except voice can transfer/escalate and has call lifecycle behavior.

## Implementation Order

1. Add `PhoneCartDraft` model/admin and bearer-gated upsert/release endpoints.
2. Add POS claim endpoint/view that revalidates and loads the normal session cart.
3. Add `BudtenderClient.phone_cart_*` methods.
4. Add `stage_phone_cart` tool and bind it to Vapi budtender assistant.
5. Add scripted multi-turn tests for availability, specials, policy, cart quote, release, and POS claim.
6. Run full root and voice test suites.

## Intentional Non-Builds

- No second checkout engine.
- No new scheduler.
- No new dependency.
- No voice-side Dutchie client.
- No inventory reservation until the POS/register flow explicitly supports it.
