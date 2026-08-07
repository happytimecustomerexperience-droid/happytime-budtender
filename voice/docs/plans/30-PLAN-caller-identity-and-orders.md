# 30 — PLAN — Caller Identity & Order Linking (Phone + Web Chat)

> **Status:** PROPOSAL. Nothing in this document is built. Every mechanism, model field, and
> endpoint described as new is labeled **PROPOSED**; anything not labeled PROPOSED is existing,
> verified behavior (file:line cited). Where a fact could not be nailed down with confidence it
> is flagged **UNCERTAIN** rather than asserted.
> **Subsystem:** touches `voice/` (Vapi channel), `budtender/` (the shared API + the native
> `chat` channel it already serves), and `pos/` (register claim). Builds on `29-PLAN-phone-cart-handoff.md`
> (staging) and `21-SPEC-budtender-contract.md` §7 (the returning-caller handshake). Does not
> replace either — this plan is the missing piece between "we recognized a phone number" and
> "we know who that is and we can act on it."
> **Read order before executing:** `00-MASTER-ROADMAP.md` → `02-DECISIONS.md` →
> `21-SPEC-budtender-contract.md` §6/§7 → `29-PLAN-phone-cart-handoff.md` → this doc.

---

## 1. Goal

Today the agent can recognize a returning phone number well enough to rank products
taste-first, and it can stage a cart for the register. It cannot do either of the two things
the owner actually asked for:

1. Confirm to itself and to the caller **who they are**, safely — a phone match alone is not
   proof of identity (caller ID is trivially spoofable).
2. If the caller has no account, **start** one — honestly, without pretending a phone call can
   do what an ID scan does.

This plan specifies, state by state, how a phone number becomes a confirmed (or explicitly
unconfirmed) identity, how a staged order gets glued to the right Dutchie account without staff
re-typing a phone number they already have, and how "no account yet" becomes a real account
without the agent ever claiming to have verified something it cannot verify over a phone line.

---

## 2. Current Repo Facts (verified this session)

Grounded against the working tree at `C:\Users\vladi\OneDrive\Desktop\happytime-budtender`
(branch `feat/pos-roles-queue`). Every path below was opened and read, not assumed.

- **Voice caller-ID capture:** `voice/voice/webhooks.py:200-205` (`handle_tool_calls`) reads
  `call.customer.number` into `ctx["caller_number"]` on every tool-call turn. A comment there is
  explicit: the raw number is transient, used only to feed recognition, "NEVER persisted."
- **Lookup exists (voice→budtender):** `voice/voice/recognition.py::resolve_caller` (`voice/voice/recognition.py:50-95`)
  normalizes the number to E.164 (`normalize_e164`, `voice/voice/recognition.py:29-41`), computes
  a peppered hash for voice's own storage (`phone_hash`, `voice/voice/recognition.py:44-47`,
  backed by `crm.models.phone_hash` at `voice/crm/models.py:20-32`), then calls
  `voice/voice/budtender_client.py:271` `resume_by_phone(...)`, which POSTs the **raw E.164
  number** (not the hash) to `budtender/views.py:883` `ResumeByPhoneView`. This is a deliberate,
  documented boundary decision (`21-SPEC-budtender-contract.md` §7.1) — budtender resolves
  `CustomerProfile` by normalized raw phone, not by a hash it doesn't share the pepper for.
- **What a HIT returns:** `ResumeByPhoneView` (`budtender/views.py:883-921`) matches
  `CustomerProfile.objects.filter(phone=phone).first()` and always returns
  `profile_summary(profile)` (`budtender/serializers.py:61-70`) — `{has_history, top_categories,
  price_tier}`. **No name, no address, no order lines, no phone.** A MISS creates nothing; it
  just returns `has_history: False`.
- **`CustomerProfile` actually stores more than it discloses.** `budtender/models.py:91-116`:
  `phone` (E.164, unique), **`name`** ("from Dutchie, staff browse" — the comment says this
  explicitly), `total_orders`, affinities, `purchase_history`. The name and history exist on the
  row; `profile_summary()` simply never serializes them into the voice/chat-facing response. This
  matters: a server-side identity check can compare against `CustomerProfile.name` without ever
  putting that name on the wire to the LLM (§5 below).
- **Two different things both look like "an account" — they are not the same thing (see §3).**
  `CustomerProfile` is a budtender-only analytics shell, rebuilt nightly from synced Dutchie
  transaction history (`budtender/tasks.py::_fold_history`, referenced ~L169-332, folds
  `get_transactions_detailed` into per-phone rows). It only exists for phones that have
  **completed a purchase** that synced. A phone with a real Dutchie register account but zero
  purchases has **no** `CustomerProfile` row and would read as `has_history: False` even though
  Dutchie itself knows them. The actual "does this person have a Dutchie account" answer lives in
  Dutchie's own guest/customer table, reached today only via
  `bundles/customers.py::lookup_by_phone` (`bundles/customers.py:69-90`), which calls
  `PosRegisterClient.guest_search` — a live, per-store Dutchie API call, digit-matched against
  `PhoneNo`/`Phone`/`CellPhone`. This function is what `bundles/views.py:505` area
  (`bundles/views.py:460-530`, the web-order checkout) calls via `customers.attach(draft)`
  (`bundles/customers.py:96-100`) at order time, and what `pos/views.py` calls via
  `bundle_customers.ensure_customer(draft)` (`pos/views.py:1838-1849`, inside
  `phone_cart_claim`) at claim time to create-or-match the real guest.
- **`ProfileUpsertView` is not account creation.** `budtender/views.py:1165-1172`:
  `CustomerProfile.objects.get_or_create(phone=phone)` — makes a bare, nameless analytics row.
  Nothing in `voice/` calls it. It creates no Dutchie guest, confers no purchase right, and
  `has_history` stays `False` for it forever (no orders folded in).
- **`voice/voice/tools/phone_cart.py::handle_stage_phone_cart` (`voice/voice/tools/phone_cart.py:53-79`)
  DOES currently send `"phone"`** in the upsert payload (`ctx["_caller_phone"]` or
  `ctx["caller_number"]`), contradicting a naive read of "voice never sends phone." But
  `budtender/views.py::PhoneCartUpsertView` (`budtender/views.py:964-1038`) only ever uses that
  incoming `phone` to compute `phone_hash`/`phone_last4` on a **new** draft
  (`budtender/views.py:981-984`) — it never sets `draft.contact_phone`. The model comment at
  `budtender/models.py:210-212` is accurate: `contact_phone`/`contact_email` are the web-order
  fields; the voice path leaves them blank by design.
- **The auto-resolve gate is real and does sit unused for voice.**
  `pos/views.py:1838-1849` (inside `phone_cart_claim`): `if draft.contact_phone or
  draft.dutchie_acct_id: acct_id, cust_name, how = bundle_customers.ensure_customer(draft)` — the
  gate keys on `contact_phone`, which is exactly the field the voice path never populates. So a
  voice-originated draft always lands in the `else` branch, and staff sees no customer note and
  no auto-attach. (Line number drifted slightly from the 883/1170/1808 landmarks the requesting
  brief cited — current HEAD has them at 883/1165/1838-1849; the logic described is identical to
  what the brief described.)
- **`ensure_customer` already has a working, deliberately-incomplete-DOB creation path.**
  `bundles/customers.py:103-134`: if no existing Dutchie guest matches, it calls
  `create_guest(first_name=, last_name=, dob="", phone=, email=)` — **DOB is deliberately blank**.
  The comment is explicit: *"we never collect it online. The customer shows ID at the counter."*
  This is the store's already-shipped compliance posture for a remote channel (web checkout):
  create an unverified guest eagerly, gate the actual product hand-off on an in-person ID check.
  No product ever leaves the building through this path without `cart_submit`
  (`pos/views.py:~1798+`), which is staff-only, in-store, and requires a `store`/`acct_id`/`cart`
  already in an authenticated POS session.
- **Age/ID verification machinery exists and is register-only.** `pos/views.py::create_customer`
  (`pos/views.py:822-865`) is `@login_required`, requires `pending.raw_scan` with
  `first_name`/`birth_date`, and refuses if `scan.get("over_21") is False`
  (`pos/views.py:840-843`). This is real ID-scan-backed creation. `idscan/` (`idscan/aamva.py`,
  `idscan/pipeline.py`) is the AAMVA driver's-license parser behind it. None of this is reachable
  from a phone call or a chat message, and this plan does not try to make it reachable — a phone
  cannot present a driver's license.
- **No identity-challenge gate exists anywhere today.** `resolve_caller` sets `ctx["known"] =
  True` on a phone match alone (`voice/voice/recognition.py:85`) and nothing downstream asks the
  caller to prove they are that phone's owner. `voice/voice/guardrails.py` (full file read) has a
  code-owned, version-controlled wall for cost/margin leakage (`scrub_leak`/`assert_no_leak`,
  `_FORBIDDEN_KEYS`) and a separate keyword-deterministic scope/crisis gate (`in_scope`,
  `age_gate_required`) — **no analogous wall exists for customer PII disclosure.** This is the
  gap §5 below closes.
- **"Web chat" is not inside `voice/`.** `budtender/gemini_chat.py` serves the native `chat`
  channel (`ChatSession.channel` default `"chat"`, `budtender/models.py:129`) directly — this is
  the happytimeweed.com chat widget (a separate Next.js repo per `repo-map-happytime.md`), not a
  Vapi call. **Verified: `gemini_chat.py` today contains no reference to `phone` at all** — the
  live web-chat surface neither asks for nor uses a phone number. `resume-by-phone` is called
  only from `voice/`. Any web-chat identity flow this plan proposes is new on **both** sides: a
  budtender API contract (in scope here) and a happytimeweed widget change (out of scope — a
  downstream dependency, flagged in Open Questions).
- **`voice/voice/guardrails.py`, `voice/voice/constants.py:190-213`
  (`stage_phone_cart` tool schema), and `29-PLAN-phone-cart-handoff.md`'s non-negotiable
  boundaries** ("Voice can stage intent only," "Customer age/ID and WA compliance remain
  in-store/register responsibilities," "Phone cart drafts... never a reservation") all still hold
  and are not weakened by anything in this plan.

---

## 3. Terms — "account" means two different things

The owner's ask uses "account" the way a person naturally would. The codebase has two things
that could answer to that word, and they answer different questions. Getting this wrong would
mean building the wrong lookup.

| Term (this doc) | What it is | Lookup | Exists for | Confers purchase rights? |
|---|---|---|---|---|
| **History Profile** | `budtender.models.CustomerProfile` | `resume-by-phone` (existing) | Any phone with at least one **synced completed purchase** | No — it's an analytics/personalization shell |
| **Register Account** | A real Dutchie guest record (`AcctId`) | `bundles.customers.lookup_by_phone` → `PosRegisterClient.guest_search` (existing, currently POS/web-only) | Any phone Dutchie has a guest row for, purchase or not | Yes — `cart_submit` requires an `AcctId` sourced from here |

**PROPOSED, and flagged as a decision the owner should confirm (see Open Questions Q1):** the
identity/account state machine in §4 is driven by the **Register Account** lookup, not
`has_history`. Reasoning: "does this caller have an account" is naturally read as "can we ring
them up," and only a Register Account answers that. `has_history`/History Profile stays exactly
as it is today — an independent, unmodified signal that only affects margin-vs-taste product
ranking (`21-SPEC` §6) and is never spoken to the caller as identity information.

---

## 4. Non-Negotiable Boundaries (extends `29-PLAN`'s list)

- A phone number matching a Register Account is a **lead**, never a credential. It unlocks
  nothing about that account until the caller passes the challenge in §6.
- The agent may never read back a stored name, address, order history, or any other
  account-specific fact **before** confirmation succeeds.
- Voice still cannot call `cart_submit`, `create_guest`, or any Dutchie write directly
  (`21-SPEC` §1.3, `29-PLAN` boundaries) — everything proposed here is either read-only lookups
  or writes to budtender's own tables (`PhoneCartDraft`, and any new PROPOSED model), exactly
  like the existing phone-cart staging pattern.
- A raw phone number is never persisted in the voice repo's own storage (unchanged from
  `21-SPEC` §7.1/D1). It may cross the voice→budtender Bearer/TLS hop transiently, exactly as
  `resume_by_phone` already does.
- Nothing in this plan creates a Dutchie guest from a bare phone call with no order attached
  without an explicit owner decision (§7, Open Questions Q4). The safe default is: an account is
  only ever materialized in Dutchie the same way it is today — lazily, at claim, via
  `ensure_customer`, with ID checked in person before pickup.

---

## 5. Phone acquisition per channel

| Channel | Source | Trust level | Normalization | Notes |
|---|---|---|---|---|
| **Vapi inbound, caller ID present** | `call.customer.number` (`voice/voice/webhooks.py:204`) | Untrusted for identity, fine for ranking | `recognition.normalize_e164` (existing, `voice/voice/recognition.py:29-41`) | Already flows into `resolve_caller`. **PROPOSED:** this becomes the *candidate* phone for the identity lookup (§6), not an automatic identity. |
| **Vapi inbound, caller ID blocked/withheld** | `customer.number` empty/missing | N/A — no candidate | `normalize_e164("")` → `""`, handshake skipped today (`voice/voice/recognition.py:70-75`, `21-SPEC` §7.2) | Existing behavior: margin-first ranking, no personalization, no error. **PROPOSED (new, this plan):** for the *identity/account* flow specifically (not ranking), the agent must ask the caller to say their phone number aloud. A spoken number is a transcript, not a telephony signal — see next row, same trust level as "caller states a different number." |
| **Web chat** | No caller-ID equivalent exists in a browser | N/A — no candidate | N/A until typed | **PROPOSED:** the chat widget must collect a typed phone number before any lookup can run. Client-side validation should mirror the existing 10-digit rule already used at `bundles/views.py:468-476` (`_clean_phone`) for consistency, but this is a happytimeweed-repo change (Open Questions Q5) — budtender's contract only needs to accept a phone string from either source. |
| **Caller states a phone different from the one they're calling on** | Spoken/typed number, may differ from `caller_number` | Same as "typed" — **the spoken/typed number always wins for the lookup**, caller ID is only ever a hint | Same `normalize_e164` | **PROPOSED rule:** whenever a caller explicitly states a phone number, that stated number becomes the lookup candidate, full stop — even if it doesn't match caller ID. Do not silently keep using caller ID once the caller has stated a different number; do not surface a "these don't match" warning to the caller (that discloses nothing useful and just sounds suspicious for the extremely common case of a call from a work phone about a personal account). |

**Normalization (binding, matches existing convention):** E.164, via the same
`normalize_e164`/`_normalize_phone` shape already used by `recognition.py` and
`budtender/views.py`. **Hashing:** unchanged from today — the peppered hash
(`crm.models.phone_hash`) is computed for voice's own storage the instant a candidate number
exists; the raw number is used transiently in-request for the budtender lookup call and is never
written to a voice-repo table (same rule as `21-SPEC` §7.1, D1).

---

## 6. The lookup state machine

Six states, named exactly as requested. `ctx` here means the same per-turn context dict
`resolve_caller` already mutates (`voice/voice/recognition.py`); the state is **PROPOSED** as a
new field, e.g. `ctx["identity_state"]`, alongside the existing `ctx["known"]` (which keeps
meaning "History Profile has_history" and is left alone — see §3).

| State | How reached | What the agent MAY say | What the agent MAY do |
|---|---|---|---|
| **UNKNOWN** | No phone candidate yet (caller hasn't stated one; chat hasn't collected one) | Ask for the phone number naturally, e.g. "What's the best number for your order?" | Nothing account-related. Product Q&A, FAQ, and phone-cart staging with no `contact_phone` all still work exactly as today. |
| **MATCHED_UNCONFIRMED** | A phone candidate exists AND the Register Account lookup (§3, PROPOSED endpoint TODO-C1) returns a match | "I see an account for this number — can you confirm the first name on it for me?" (issue the challenge, §6) | Issue the identity challenge. May NOT say the name, may NOT say anything else about the account (order history, address, whether they've ordered before). May continue staging a cart (product/qty/store) — cart contents are not account PII. |
| **MATCHED_CONFIRMED** | Challenge in §6 passes | "Thanks, you're all set under that account." May now use the caller's first name if it was the successful challenge answer (they just said it themselves — nothing new is disclosed) | Populate `PhoneCartDraft.contact_phone`/`dutchie_acct_id`/`customer_status` (§8) so the register auto-resolves. May reference "your usual order" style hints already permitted via `profile_summary.top_categories` (`21-SPEC` §5.1) — that channel is unchanged and was already non-PII. |
| **NO_ACCOUNT** | Register Account lookup returns no match (Dutchie `guest_search` genuinely finds nobody) | "I don't see an account for that number yet — want me to get one started?" | Offer §8 (account creation, PROPOSED). Continue staging a cart under `Customer.NEW` exactly as `bundles/customers.py`'s `PhoneCartDraft.Customer.NEW` already models for the web path. |
| **CREATION_PENDING** | Caller says yes to starting an account (from NO_ACCOUNT) | "Got it — I've got you down as [name]. We'll finish setting it up with your ID when you come in." Never "your account is created" / "you're verified" / "you're all set to buy." | Write the PROPOSED pending-caller record (§8) OR, if an order is being placed in the same interaction, simply populate `contact_phone`/`pickup_name` on the `PhoneCartDraft` and let the existing `ensure_customer` (`bundles/customers.py:103-134`) create the real (DOB-blank) Dutchie guest at claim — no new Dutchie-write code needed for the order-attached case. |
| **CREATION_BLOCKED** | Register Account lookup itself failed/unreachable (Dutchie API error — the existing `UNRESOLVED` status, `bundles/customers.py:77-79`), OR the challenge lockout in §6 was hit, OR the caller refuses to state any phone at all | "I'm not able to look that up right now — a team member can help you when you're in, or I can transfer you." | No account claims of any kind. Staging can continue anonymously (identical to today's UNKNOWN-caller experience) if the caller wants to keep shopping; offer human escalation per the existing escalation path (P2, `12-P2-ESCALATION-TRANSFER-EMAIL.md`) rather than retrying silently. |

**Transitions are one-directional except for retries:** UNKNOWN → (MATCHED_UNCONFIRMED |
NO_ACCOUNT | CREATION_BLOCKED) on lookup; MATCHED_UNCONFIRMED → MATCHED_CONFIRMED on a correct
challenge answer, or → CREATION_BLOCKED after N failed attempts (§6); NO_ACCOUNT →
CREATION_PENDING on caller consent. A call/session never re-enters UNKNOWN once a phone
candidate has been resolved — a wrong number stated later is a **new** lookup on that new number,
not a retry of the old one.

---

## 7. The identity-confirmation gate (security-critical — PROPOSED)

This is the piece that does not exist today and is the actual point of this plan. Caller ID is
spoofable (any VoIP/PBX can set an arbitrary `From` number); a phone match by itself is a lead,
not proof. This section specifies a gate structurally identical in spirit to the existing
`scrub_leak` wall — **code-owned, version-controlled, not a prompt line, not overridable by
anything the LLM emits.**

### 7.1 Mechanism (PROPOSED)

- New module `voice/voice/identity_gate.py` (or a section of the existing `guardrails.py` —
  either is fine; the point is it lives in code, not in the assistant prompt), mirroring
  `scrub_leak`'s posture: a deterministic function the webhook/tool dispatcher calls, not
  something the model can talk its way around.
- **The challenge:** caller states their first name (or ZIP, whichever the owner prefers — see
  Open Questions Q2). The comparison happens **entirely server-side, inside budtender**, against
  the `CustomerProfile.name` field that already exists but is never serialized today
  (`budtender/models.py:93`) — or, if the owner wants the challenge tied to the Register Account
  rather than the History Profile (consistent with §3's recommendation), against whatever name
  field Dutchie's guest record carries (`_name_of`, `bundles/customers.py:63-66`, already parsed
  from `guest_search` rows).
- **PROPOSED new budtender endpoint**, `POST /api/v1/customer/identity-confirm` (TODO-C2, table
  in §9): request carries `{phone, location, claim: {"field": "first_name", "value": "<what the
  caller said>"}}`; response is `{"match": true|false, "attempts_remaining": <int>, "locked":
  true|false}` — **and nothing else.** The actual stored name never appears in the response body.
  This is the same discipline as `PUBLIC_PRODUCT_FIELDS`/`profile_summary` (allowlist by
  construction, not by hoping nobody adds a field later).
- **Comparison rule:** case-insensitive, whitespace-trimmed, first-name-only (not full name — a
  caller who only knows "Sam" for a "Samantha" account should still pass; exact matching rule is
  an implementation detail for whoever builds this, not frozen here).
- **Attempts + lockout (PROPOSED):** a cache-backed counter keyed by `phone_hash` (not IP — a
  spoofed-caller-ID attacker isn't meaningfully IP-rate-limited, and IP is unavailable at all on
  the Vapi path), modeled on the existing fixed-window pattern in `pos_core/ratelimit.py:25-50`
  but keyed differently: `identity_challenge:{phone_hash}`, **N = 3 attempts per rolling 30
  minutes** (numbers are a starting proposal, not frozen — Open Questions Q3). On the 3rd
  consecutive miss, `locked: true` for the remainder of the window; the state machine moves to
  **CREATION_BLOCKED** and the agent stops asking — repeated guessing is exactly the attack this
  gate exists to stop, so silently allowing more tries defeats the point.
- **A successful match resets the counter for that phone_hash.**

### 7.2 What may be disclosed before vs. after confirmation

| | Before confirmation (UNKNOWN / MATCHED_UNCONFIRMED / NO_ACCOUNT) | After confirmation (MATCHED_CONFIRMED) |
|---|---|---|
| That *some* account exists for this number | Yes ("I see an account for this number") | Yes |
| Account name | **No** | Yes, only by echoing back what the caller themselves already said as their challenge answer — the agent never reads out a *stored* name the caller hasn't spoken first |
| Order history / past purchases | **No** | **No, still** — nothing in this plan exposes `purchase_history` or `total_orders` to the agent at all; `profile_summary`'s existing non-PII hints (`top_categories`, `price_tier`) are the only history-shaped thing ever spoken, and that channel is unchanged and was already permitted pre-confirmation for ranking purposes (it's not identity-revealing — see next row) |
| Address / email / any contact field other than the phone being used right now | **Never**, at any state | **Never** — out of scope entirely; nothing in this plan reads or speaks these fields |
| Whether the number matched at all | Only implicitly (a lookup failure and a "someone else's account" failure must sound identical — see Never list) | N/A |

### 7.3 Must NEVER be spoken, regardless of confirmation state

- The stored account name, unless the caller said it first (per 7.2).
- Order history, purchase amounts, item-level history, loyalty/points balances, or anything
  resembling `CustomerProfile.purchase_history`.
- Cost/margin (already covered by the existing `scrub_leak` wall — this gate does not weaken or
  duplicate it, just extends the same posture to a new PII category).
- Any signal that would let a caller enumerate whether a *given* phone number has an account by
  brute force (e.g., the response for "no account" and "account exists but you failed the
  challenge" must be worded so a caller can't distinguish "wrong number" from "right number,
  wrong name" — this is the standard login-enumeration mitigation, applied here for the same
  reason).
- The full account phone number back to the caller (they already know it — no need — but this
  also blocks a "read it back to me" fishing attempt from someone who only has a partial number).
- Anything from a **different** account than the one on the currently-active phone candidate,
  even if a staff-facing view elsewhere (P7 `customer_detail`/`customer_row`,
  `budtender/serializers.py`) would show it to a logged-in employee. Staff auth and caller
  confirmation are not the same trust boundary and this plan does not blur them.

---

## 8. Linking a staged order to a matched account (PROPOSED)

Mirrors the exact pattern `bundles/customers.py` already uses for web orders — this section is
mostly "do for voice what the web path already does," not new design.

**What voice must now populate on `PhoneCartDraft` once `identity_state` reaches
`MATCHED_CONFIRMED` (or `CREATION_PENDING` with a name given):**

| Field | Today (voice) | PROPOSED |
|---|---|---|
| `contact_phone` | Always blank (`budtender/models.py:210-212`) | Set to the confirmed E.164 phone the same turn identity is confirmed — this is the one field the gate at `pos/views.py:1838` (`if draft.contact_phone or draft.dutchie_acct_id`) actually checks, so populating it is the whole fix. |
| `dutchie_acct_id` | Never set by voice | **Optionally** set immediately if the identity-confirm lookup (TODO-C2) already resolved a Register Account — saves `ensure_customer` a redundant `guest_search` at claim. Not required; `ensure_customer` re-checks anyway (`bundles/customers.py:112-119`, deliberately, in case the account changed between order and claim). |
| `customer_status` | Never set by voice | `PhoneCartDraft.Customer.MATCHED` if confirmed, `.NEW` if `CREATION_PENDING`, `.UNRESOLVED` if the lookup itself failed (`CREATION_BLOCKED` from a Dutchie-side error, not a challenge failure). |
| `customer_name` | Never set by voice | The name the caller confirmed (matched case) or gave (new-account case) — same field the web path fills from `_name_of()`. |
| `pickup_name` | Already settable via `stage_phone_cart`'s `pickup_name` arg (`voice/voice/constants.py:207`) | Unchanged — keep using it, it already flows through. |

**What changes at `pos/views.py:1838-1849` (PROPOSED, minor):** nothing structural — the gate
`if draft.contact_phone or draft.dutchie_acct_id:` already fires correctly once voice populates
`contact_phone`. The only PROPOSED change is the `customer_note` wording, which currently reads
generically; once voice-origin drafts can arrive already `customer_status=MATCHED`, staff should
see something like *"Confirmed by phone: [name]"* instead of the current *"Created a new account
for..."* copy, so staff can tell a phone-confirmed match apart from a claim-time cold create.
This is a copy-only change, not a logic change.

**New PROPOSED tool surface for voice:** `stage_phone_cart` (`voice/voice/constants.py:190-213`)
currently has no field for "the caller confirmed identity, here's the phone to attach." Either
extend the existing tool's payload (add `contact_phone`/`customer_status`/`customer_name`,
server-injected the same way `phone` already is in `voice/voice/tools/phone_cart.py:63-70` — the
LLM never supplies these directly, ctx does) or add these as fields the identity-confirm response
itself writes directly to the draft via a combined budtender-side call. Either shape works; not
frozen here — an implementation decision for whoever builds this.

---

## 9. Account creation from a call — be honest about the compliance problem

**The hard constraint:** WA cannabis retail requires in-person 21+ ID verification before
product changes hands. *(General regulatory requirement — this plan does not cite a specific WAC
section; confirm exact requirement text with the owner or a compliance advisor before treating
this as legally authoritative — UNCERTAIN beyond "ID check at point of sale is how the business
already operates.")* A phone call cannot perform that check: no liveness check, no document
capture, nothing that substitutes for a human looking at a photo ID.

**The good news, found during verification, not assumed:** the business has *already* solved
this for the web-order channel, and the solution generalizes cleanly to voice. `ensure_customer`
(`bundles/customers.py:103-134`) creates a Dutchie guest with **DOB deliberately blank** the
moment an online order is placed, and defers all real verification to the in-person pickup
counter. No product ever leaves the building without a staff-run `cart_submit`
(`pos/views.py`, `@login_required`, register-session-scoped). **This means: an order-attached
account creation from a phone call is not a new compliance category** — it is the exact same
posture the store already accepts for orders that arrive with zero in-person interaction (web
checkout). The **PROPOSED** path for `CREATION_PENDING` when an order is being placed in the same
call/chat is therefore: populate `contact_phone`/`pickup_name` on the `PhoneCartDraft` (§8) and
let the existing, unmodified `ensure_customer` create the guest at claim, same as today. **No new
Dutchie-write code path is needed for this case.**

**What IS new, and does need sign-off, is the bare "start an account, no order" case** — a caller
who wants to be "in the system" without buying anything right now. Nothing today creates any
kind of record for that. Two shapes, presented as options, neither built:

- **Option A (recommended, minimal — ponytail-flagged: build only if actually asked for).** Don't
  build a standalone account-creation path at all. `CREATION_PENDING` with no order in progress
  just means: "I can get that started once you tell me what you'd like" — i.e., fold "starting an
  account" into "let's stage an order," which reuses 100% of existing/§8 machinery. If the caller
  truly wants nothing, there is genuinely nothing safe to create yet.
- **Option B (PROPOSED new model, only if the owner wants a bare signup).** A small
  `PendingAccountRequest` model in `budtender/` — **not** `CustomerProfile`, and explicitly not a
  Register Account: `phone_hash`, `phone_last4`, `first_name` (as the caller gave it, unverified),
  `location_slug`, `created_at`, `fulfilled_at` (null until an in-person ID scan promotes it —
  reusing the existing `pos/views.py::create_customer` ID-scan-gated path, unmodified). Confers
  **zero** purchase rights — it is a note-to-self for staff, nothing else. Expires after a fixed
  window (e.g. 30 days) if never fulfilled.

**What needs the owner's (or a compliance advisor's) explicit sign-off before either option is
built:**

1. Whether the agent may say *any* variant of "you're all set" / "your account is created" before
   an ID has been checked — even Option A's order-attached path defers real verification to
   pickup, so the phrasing in the CREATION_PENDING row of §6's table ("we'll finish setting it up
   with your ID when you come in") is a proposal, not a decision, and should be reviewed for
   wording that could be read as a compliance claim.
2. Whether Option B is wanted at all, given Option A already covers the order-placing case and a
   bare signup has no clear business need identified in this research.
3. Retention period for any PENDING record that never gets fulfilled (§10).
4. Confirmation that nothing in this flow can be used to reserve, hold, or imply availability of
   product for an unverified caller — `PhoneCartDraft` is already explicitly a non-reservation
   per `29-PLAN-phone-cart-handoff.md`, and this plan does not change that; flagging it here
   because account-creation language ("you're set up to order") could easily drift into
   reservation-sounding language if not reviewed.

---

## 10. Data retention + PII

Restates and extends the existing discipline (`21-SPEC` §7.1, D1/H3; `voice/voice/guardrails.py`
`redact_pii`) — nothing here loosens it.

| Data | Where it lives | Raw or hashed | Retention |
|---|---|---|---|
| Caller's raw phone number | In-request only (voice→budtender Bearer/TLS hop), never written to a voice-repo table | Raw, transient | Not persisted in `voice/` (unchanged). Budtender persists it on `CustomerProfile.phone`/`PhoneCartDraft.contact_phone` — **existing** behavior for the web path, now proposed to extend to voice-confirmed drafts (§8). This is not new PII exposure to budtender; budtender already holds raw phones for every web order and every synced Dutchie transaction. |
| Peppered phone hash | `voice`'s own DB (`crm.models.Caller`, `VoiceCall`) | Hashed | Unchanged — indefinite, same as today, it's not reversible PII. |
| Challenge answer (name/ZIP the caller spoke) | **PROPOSED:** never stored — compared in-request inside the identity-confirm endpoint and discarded. Only the pass/fail + attempt count is retained (in the cache-backed lockout counter, which expires with the window). | N/A — not persisted | Attempt counter: rolling window only (§7.1, e.g. 30 min), auto-expires. |
| `identity_state` for a call | **PROPOSED:** transient `ctx` field, same lifecycle as `ctx["known"]` today — not a new persisted column unless the owner wants an audit trail of confirmation outcomes (Open Questions Q6). | N/A | Call duration only, unless audited (open question). |
| `PendingAccountRequest` (Option B, if built) | Budtender DB | `phone_hash` + `phone_last4`, plus an unverified first name | Fixed expiry window (proposed 30 days), purged or flagged stale after. |
| Account name used in the challenge comparison | `CustomerProfile.name` / Dutchie guest name — both already exist and already persist today, unchanged | Raw (already the case) | Unchanged by this plan — this plan only adds a *comparison* against data that already exists, not new storage of it. |

**No new field in this plan stores anything more sensitive than what `bundles/customers.py`
already stores for every web order today.** The delta this plan introduces is entirely on the
*voice/chat side*: previously voice never touched `contact_phone` at all; now it does, under a
gate. That gate (§7) is the actual new surface area, and it stores the least possible amount —
a pass/fail and a rolling counter, not the compared value.

---

## 11. New budtender endpoints this plan requires (PROPOSED — none exist yet)

| ID | Endpoint | Purpose | Wraps |
|---|---|---|---|
| **TODO-C1** | `POST /api/v1/customer/identity-lookup` | Given `{phone, location}`, return `{status: "matched"\|"new"\|"unresolved"}` — the Register Account check, leak-safe (no name/acct_id in the response; those come only after confirmation via `phone_cart` fields in §8). | `bundles.customers.lookup_by_phone` (`bundles/customers.py:69-90`) — same app, same process, just a new route; no new Dutchie boundary crossed. |
| **TODO-C2** | `POST /api/v1/customer/identity-confirm` | Given `{phone, location, claim: {field, value}}`, return `{match, attempts_remaining, locked}` (§7.1). | Compares against `CustomerProfile.name` and/or the Dutchie guest name resolved via TODO-C1's lookup — implementation detail, not frozen. |
| **TODO-C3** *(only if Option B, §9, is approved)* | `POST /api/v1/customer/pending-create` | Given `{phone, location, first_name}`, create a `PendingAccountRequest` row. | New model, new endpoint — no existing code to wrap. |
| **TODO-C4** *(nice-to-have, only if Open Questions Q6 says yes)* | Extend `PhoneCartDraft.audit` (already a JSON list, `budtender/models.py`) with an `identity_confirmed` entry, or a small `IdentityConfirmationAudit` row, so a confirmed/failed/locked outcome is traceable per call for the owner to review abuse patterns. | Reuses the existing `audit` JSON pattern already used throughout `PhoneCartUpsertView`/`PhoneCartClaimView`. |

None of these block anything already shipped. Voice ships against TODO-C1/C2 with graceful-empty
fallbacks (`{"status": "unresolved"}` / `{"match": false, "locked": false}`) exactly like every
other `budtender_client` method already does on a timeout or non-2xx (`21-SPEC` §8.2) — a
budtender outage degrades to CREATION_BLOCKED-with-human-offer, never a false confirmation.

---

## 12. Acceptance Criteria

**A. Phone acquisition (§5)**
- A1. Vapi inbound with a caller-ID number present populates a lookup candidate without asking
  the caller to repeat it.
- A2. Vapi inbound with caller ID blocked/withheld results in the agent asking for the number
  before any account-state lookup runs; ranking (margin-vs-taste) is unaffected and still
  degrades to anonymous/margin-first exactly as today (`21-SPEC` §7.2) — this plan does not touch
  that path.
- A3. Web chat never has an implicit phone source; a lookup never runs until a number has been
  typed and passes the same 10-digit validation shape already used at checkout.
- A4. A caller who states a number different from their caller ID has that stated number used for
  every subsequent lookup in the call, with no "mismatch" language ever spoken to the caller.

**B. Lookup state machine (§6)**
- B1. Every one of the six named states is reachable from a scripted conversation, and the
  agent's disclosure at each state matches §6/§7.2 exactly (a contract test can assert the tool
  result shape per state carries no forbidden field).
- B2. UNKNOWN never blocks product Q&A or cart staging — identity is additive, not a gate on
  shopping (consistent with `29-PLAN`'s "voice can move across topics in one call").
- B3. A Dutchie-side lookup failure (guest_search errors) lands in CREATION_BLOCKED, never in
  NO_ACCOUNT — these must not be conflated, since one is "we know there's no account" and the
  other is "we don't know."

**C. Identity confirmation gate (§7) — the security-critical suite**
- C1. A phone match alone (no challenge passed) never results in the agent speaking a stored
  name, order history, or any account field beyond "an account exists."
- C2. A correct challenge answer moves the state to MATCHED_CONFIRMED and unlocks only what §7.2
  permits — no test may assert access to `purchase_history`/`total_orders` from voice/chat, full
  stop, because no code path in this plan ever fetches them into that response.
- C3. Three consecutive wrong answers (or the owner-tuned N) lock the phone_hash for the window;
  a fourth attempt in the same window is refused without even evaluating the claim.
- C4. The identity-confirm endpoint's response body contains neither the compared-against name
  nor any `cost`/`margin` substring — reuse the existing no-leak contract-test philosophy
  (`21-SPEC` §11 H1) against this new endpoint.
- C5. **Spoofed caller ID:** a caller-ID number that matches a Register Account, where the caller
  then fails the name/ZIP challenge, never reaches MATCHED_CONFIRMED and never has any account
  fact disclosed — caller ID trust level is capped at "candidate," never "proof," by construction
  (there is no code path that sets MATCHED_CONFIRMED without a passed TODO-C2 call).
- C6. **Wrong name on challenge:** the failure response is worded identically whether the phone
  had no account at all or had an account and the name was wrong (enumeration resistance, §7.3).
- C7. **Caller refuses to confirm:** the agent may still stage a cart and complete a call; refusal
  simply keeps the state at MATCHED_UNCONFIRMED (or moves it to CREATION_BLOCKED if the caller
  explicitly asks to stop trying) — it is never an error state that blocks the rest of the call.

**D. Order-to-account linking (§8)**
- D1. A `PhoneCartDraft` reaching MATCHED_CONFIRMED before release has `contact_phone` populated,
  and `pos/views.py`'s existing gate (`if draft.contact_phone or draft.dutchie_acct_id:`) fires on
  claim without any code change to that condition.
- D2. Staff sees a distinct note for a phone-confirmed match vs. a claim-time cold create (the
  copy-only change in §8).
- D3. **Two accounts sharing a phone** (e.g., a shared household line): `ensure_customer`'s
  existing re-check-before-create behavior (`bundles/customers.py:112-119`) is unchanged by this
  plan — if `guest_search` returns an ambiguous/multiple result today, that ambiguity is not
  newly introduced or newly hidden by the identity gate; the gate only decides whether *this
  caller* gets to hear the *one* name/ZIP they successfully claimed, not which of several
  Dutchie rows is "correct." (This plan does not attempt to disambiguate multiple Dutchie guests
  on one phone — flagged as Open Question Q7, since the existing `lookup_by_phone` already
  returns only the first digit-matching row and this plan inherits that limitation.)

**E. Account creation (§9)**
- E1. An order placed during CREATION_PENDING results in exactly the same Dutchie guest-creation
  call (`create_guest(..., dob="")`) that a web order already makes today — no new Dutchie write
  path is added.
- E2. The agent never utters a phrase claiming ID/age verification has occurred during a call.
- E3. (Option B only, if approved) A `PendingAccountRequest` confers no entry into `cart_submit`'s
  `acct_id` requirement — it cannot be used to check out without also going through
  `ensure_customer`/ID-scan creation.

**F. Data retention (§10)**
- F1. No challenge answer (the spoken name/ZIP itself) is ever written to any table — only
  match/no-match and a rolling attempt count.
- F2. A test asserts the voice repo's own DB rows contain no raw phone number after an
  identity-confirmed call (unchanged invariant from `21-SPEC` D1, re-verified because this plan
  adds a new write path that must not violate it).

---

## 13. Phased build order

**Smallest safe first step:** ship TODO-C1 (`identity-lookup`) alone, with voice doing nothing
with it yet except logging the state transition (UNKNOWN/MATCHED_UNCONFIRMED/NO_ACCOUNT/
CREATION_BLOCKED) internally — no challenge, no disclosure, no order-linking. This validates the
Register-Account-vs-History-Profile distinction (§3) against real data with zero risk: nothing
new is ever spoken to a caller, nothing new is written to a draft.

1. **Phase 0 (validate the lookup):** TODO-C1 endpoint + `resolve_caller`-style client method +
   internal-only state logging. No behavior change a caller can observe.
2. **Phase 1 (the gate):** TODO-C2 endpoint + `identity_gate.py` + the attempt/lockout counter.
   Ship behind a flag; test the adversarial cases (§12 C-suite) before it ever talks to a real
   caller.
3. **Phase 2 (disclosure + order linking):** wire §6's per-state spoken behavior into the actual
   prompt/tool flow; populate `contact_phone`/`customer_status`/`customer_name` on
   `PhoneCartDraft` per §8; ship the `pos/views.py` copy change.
4. **Phase 3 (account creation, order-attached only — Option A):** confirm §9's sign-off items
   with the owner; if cleared, this is close to free — it's `contact_phone`/`pickup_name`
   populated the same way Phase 2 already does it, feeding the unmodified `ensure_customer`.
5. **Phase 4 (bare account creation, Option B — only if the owner wants it):** new model, new
   endpoint, new expiry job. Explicitly the last, most optional phase.
6. **Phase 5 (web chat parity):** once voice is proven, extend the same TODO-C1/C2 contract to
   the happytimeweed chat widget — this is a separate repo's frontend work and is not scoped
   further here (Open Questions Q5).

---

## 14. Open questions for the owner

1. **Q1 — Which lookup defines "has an account"?** This plan recommends the Register Account
   (Dutchie guest, via `lookup_by_phone`) over the History Profile (`has_history`), because only
   the former confers real purchase rights. Confirm or override.
2. **Q2 — Challenge factor:** first name, ZIP code, or something else (last 4 of a stored payment
   method is explicitly ruled out — this repo should never touch payment data)? First name is
   recommended as the friendliest and least error-prone for a phone conversation.
3. **Q3 — Lockout tuning:** is 3 attempts / 30-minute window right, or does the owner want it
   tighter/looser? Should a lockout be per-phone only, or also contribute to a global abuse
   signal if the same call/session tries many different phone numbers in sequence (a pattern this
   plan does not currently detect)?
4. **Q4 — Bare account creation (Option B, §9):** build it at all, or is "start an account" always
   really "let me place your first order" in practice? Recommend deferring until a real caller
   asks for the bare case.
5. **Q5 — Web chat widget:** who owns the happytimeweed.com frontend change to collect a phone
   number and call TODO-C1/C2? That work lives outside this repo; this plan only specifies the
   budtender-side contract it would call.
6. **Q6 — Audit trail:** does the owner want a persisted record of confirm/fail/lock events per
   call (TODO-C4), e.g. to review abuse patterns later, or is the transient in-call state
   sufficient? Persisting it is a small addition but is new PII-adjacent storage and should be an
   explicit yes.
7. **Q7 — Multiple Dutchie guests on one shared phone** (e.g., a family landline): out of scope
   for this plan (inherits the existing single-row-match limitation of `lookup_by_phone`) —
   does the owner want a follow-up plan for disambiguating this case (e.g., asking for a last
   name too when the phone is known to be shared), or is it rare enough to leave to staff
   judgment at claim time, same as today?
8. **Q8 — Exact WA ID-verification requirement text:** this plan asserts the general shape (ID
   check before hand-off, in person) based on how the store already operates, but does not cite
   a specific regulation. Worth a compliance-advisor pass before anything in §9 ships, even though
   §9's recommended design (Option A) changes no existing compliance-relevant code path.

---

## 15. Source-file anchors (for whoever executes this)

- `voice/voice/webhooks.py:186-216` — caller number capture, tool dispatch.
- `voice/voice/recognition.py` (full file) — existing phone normalization + History Profile
  handshake; a model for how the new identity lookup client method should be shaped.
- `voice/voice/tools/phone_cart.py` + `voice/voice/constants.py:190-213` — `stage_phone_cart`,
  the tool this plan's order-linking work extends.
- `voice/voice/guardrails.py` (full file) — the code-owned-guard pattern (`scrub_leak`,
  `assert_no_leak`, `in_scope`) the new `identity_gate` should mirror.
- `budtender/views.py:883-921` (`ResumeByPhoneView`), `:964-1063` (`PhoneCartUpsertView`/
  `PhoneCartReleaseView`/`PhoneCartClaimView` start), `:1165-1172` (`ProfileUpsertView`).
- `budtender/models.py:91-116` (`CustomerProfile`), `:195-245` (`PhoneCartDraft`).
- `budtender/serializers.py:61-70` (`profile_summary` — the existing allowlist pattern to copy
  for the new identity-confirm response).
- `budtender/urls.py` — existing `/api/v1/` route table; new routes append here.
- `bundles/customers.py` (full file) — `lookup_by_phone`/`attach`/`ensure_customer`, the pattern
  this whole plan mirrors for voice.
- `bundles/views.py:460-530` — the web checkout call site of `customers.attach`, for reference on
  how phone/name validation is already done client-side.
- `pos/views.py:822-865` (`create_customer`, ID-scan-gated creation), `:1777-1866`
  (`phone_cart_claim`, including the `contact_phone`/`dutchie_acct_id` gate at ~L1838-1849).
- `pos_core/ratelimit.py:25-50` — the fixed-window cache-counter pattern the lockout counter
  should mirror (different key shape: `phone_hash`, not IP).
- `voice/docs/plans/29-PLAN-phone-cart-handoff.md` — the staging contract this plan builds on top
  of; do not weaken any boundary listed there.
- `voice/docs/plans/21-SPEC-budtender-contract.md` §6/§7 — the existing margin-vs-taste/
  returning-caller contract; `has_history` stays exactly as specified there.
