# Phone line setup and testing (Vapi + Happy Time Voice)

**Who this is for:** the owner. No programming needed. Every command, setting name, web address and
file path below was checked against this repository (branch `claude/practical-dijkstra-8fcrma`,
2026-10-08). Steps that happen inside Vapi's own website are taken from Vapi's documentation
(github.com/VapiAI/docs, folder `fern/`) and say so. Anything nobody could check from the code is
marked **NOT VERIFIED** and is collected again at the end.

The goal: you call **one** phone number and everything works: the AI team (the Vapi **squad**),
greeting you by name, remembering what you like, sending allowlisted vendors straight to you,
quoting the deals and hours you type on the dashboard, and recommending products from the same
budtender engine the website chat uses.

> Vapi **workflows are retired**. The live design is the squad `2b132e78-6b37-4b12-b99a-17d23f8906e7`.
> The doctor (step 6) flags any workflow still attached to the number.

---

## 1. What runs where

| Piece | Where it runs | What it does |
|---|---|---|
| **Vapi** | Vapi's cloud (dashboard.vapi.ai) | Answers the phone, turns speech into text and back, and runs the AI agent. Default (`HHT_SQUAD_MODE=single`): ONE agent, `concierge`, in a one-member squad, which greets, answers store questions, helps people shop, handles vendors and problems itself (no handoffs, so the caller never hears "let me get someone"). Rollback (`HHT_SQUAD_MODE=multi`): the old five-agent squad (entry_router, budtender, faq, vendor, escalation). The model is Google `gemini-2.5-flash`, run *inside Vapi*. |
| **Voice service** (`voice-web`) | Your VPS, container `voice-web`, public at `https://voice.happytimeweed.com` | The "control plane" in this `voice/` folder: the webhook Vapi calls (`/api/voice/vapi`), the owner dashboard (`/dashboard/`), the knowledge base (deals, hours, FAQ), the vendor allowlist, call records. |
| **Budtender API** (`web`) | Your VPS, container `web` (repo root), reached by voice at `http://budtender.internal:8000` | Live Dutchie inventory, product ranking, customer profiles and customer memory. Holds the Dutchie keys (voice never does). |
| **Website chat** | happytimeweed.com (Vercel; a different repository) | The chat bubble. Calls the same budtender API for ranking, memory and deals. |

Ingress on the VPS is the existing Traefik (`traefik-a0hc`), configured by labels in the repo-root
`docker-compose.yml` (`voice-web` is the only voice container exposed). `DEPLOY.md` still talks about
a Cloudflare tunnel; the compose file says "Cloudflare is not used". Trust the compose file.

### Which `.env` file is read where

There are two copies of `voice/.env`, and they are **not** synced:

| Copy | Read by | Used for |
|---|---|---|
| **VPS:** `happytime-budtender/voice/.env` on the server | the running phone line (`voice-web`, via `env_file: ./voice/.env` in the root `docker-compose.yml`) and every `docker compose exec voice-web python manage.py ...` command | **This is the one that matters.** |
| **Your Windows copy:** `voice\.env` in your local folder | only commands you run on your own PC (`uv run python manage.py ...`) | Local testing only. Changing it changes nothing on the phone line. |

The root compose file **forces** four values whatever `voice/.env` says: `POSTGRES_HOST=voice-db`,
`DJANGO_DEBUG=0`, `HTTPS_ENABLED=1`, `HHT_BUDTENDER_BASE_URL=http://budtender.internal:8000`.

The dashboard **Credentials** page (`/dashboard/credentials/`, owner/superuser only) can also hold
some values. A value saved there **overrides** `.env` for the running website workers. But the
command-line `provision_vapi` reads `.env` only: so keep the Vapi key in `voice/.env` on the VPS
too. The doctor uses what the running service uses and tells you where each value came from.

> **Security warning: OneDrive.** If your Windows `voice\.env` sits in a OneDrive-synced folder
> (Desktop / Documents are synced by default), your Vapi private key is copied to Microsoft's cloud
> and to every device on that account. `.env` is a secret: keep it outside OneDrive, or delete the
> local copy if you only use the VPS. **If the Vapi key was ever pasted into a chat, an email, a
> ticket, a screenshot or a shared folder, rotate it** (section 12).

---

## 2. Settings you need (names only, never paste the values anywhere)

All of these go in **`voice/.env` on the VPS** (copy `voice/.env.example`; the comments there explain
each line). "Credentials page" means it can also be set on `/dashboard/credentials/`.

| Setting | Needed for | Credentials page? |
|---|---|---|
| `DJANGO_SECRET_KEY`, `PHONE_HASH_PEPPER` (must differ) | the service refuses to start without them | no |
| `POSTGRES_PASSWORD` | the voice database | no |
| `PUBLIC_BASE_URL` (= `https://voice.happytimeweed.com`), `DJANGO_ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | the webhook address Vapi calls, the dashboard login | no |
| `VAPI_PRIVATE_KEY` | everything that talks to Vapi. (There is no `VAPI_API_KEY` in this code.) | yes |
| `VAPI_WEBHOOK_SECRET` | proves a webhook really came from Vapi; without it every call event is refused | yes |
| `VAPI_SQUAD_ID` (= `2b132e78-6b37-4b12-b99a-17d23f8906e7`) | which squad the dashboard Publish button updates | yes |
| `VAPI_PHONE_NUMBER_ID` | which Vapi number `provision_vapi` connects | yes |
| `VAPI_PHONE_NUMBER_STORE_MAP` | optional: one number per store | no |
| `HHT_BACKEND_TOKEN` | voice talking to the budtender API; must equal the root `.env` value | yes |
| `HHT_DYNAMIC_GREETING` | greeting by name, customer memory, vendor allowlist (section 4) | no |
| `HHT_OWNER_PHONE` | where allowlisted vendors ring (format `+15095551212`) | yes (also on the Vendor allowlist page) |
| `HHT_TRANSFER_NUMBER_YAKIMA`, `HHT_TRANSFER_NUMBER_MTVERNON`, `HHT_TRANSFER_NUMBER_PULLMAN` | "transfer me to a person" | yes |
| `HHT_DEFAULT_STORE` | store assumed when the number is not mapped | no |
| `GEMINI_USE_VERTEX`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`, `GEMINI_API_KEY` | the server-side AI steps (call summaries, knowledge-base search, the dashboard test console) | no |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `STAFF_ALERT_EMAIL` | staff alert emails | partly |
| `VAPI_PUBLIC_KEY` | optional: the "real call in the browser" button on the test console | yes |
| `HHT_USE_CELERY` / `HHT_CACHE_URL` | optional: shared cache between workers (README "Dynamic greeting rollout" step 2) | no |

After editing `voice/.env` on the VPS, restart: `docker compose up -d voice-web`.

---

## 3. Get onto the VPS and update the code

1. Open a terminal on the VPS (Hostinger's browser terminal in hPanel, or SSH as in
   `harden-vps.sh`). **NOT VERIFIED:** the folder you cloned into; the commands below assume you
   are in the `happytime-budtender` folder (`cd happytime-budtender`).
2. Pull the new code and rebuild (this also builds the `vapi_doctor` command into the image):
   ```bash
   git pull
   docker compose up -d --build
   docker compose ps
   ```
3. Edit the settings: `nano voice/.env` (save with Ctrl+O, Enter, exit with Ctrl+X), then
   `docker compose up -d voice-web`.

---

## 4. The dynamic greeting switch, and why it matters

`HHT_DYNAMIC_GREETING=1` changes how Vapi answers the number:

* **Off (`0`, the default):** the number is attached directly to the squad. Vapi never asks our
  server who should answer. Result: no "Welcome back, Sam", no customer-memory notes, and **the vendor
  allowlist does nothing** (every vendor gets the AI). The squad still answers everything else.
* **On (`1`):** `provision_vapi` detaches the number from the squad and leaves it pointing at our
  webhook. Vapi then asks our server on every call (an `assistant-request`; Vapi's docs: "If neither
  `assistantId`, `squadId` nor `workflowId` is set, `assistant-request` will be sent to your Server
  URL", `fern/apis/api/openapi.json`, PhoneNumber). Our server then either rings the owner (an
  allowlisted vendor) or builds that caller's own copy of the squad with their first name and notes.
  Vapi gives us 7.5 seconds (`fern/server-url/events.mdx`); the caller lookup is capped at 2.5 s.

The allowlist and memory need **all three**: the setting on, `provision_vapi` run afterwards, and
the number truly unbound in Vapi. The doctor checks the third one for you.

**Turning it on** (do it when the phone is quiet; from that moment our server answers every call):
```bash
nano voice/.env                     # set HHT_DYNAMIC_GREETING=1
docker compose up -d voice-web
docker compose exec voice-web python manage.py provision_vapi --dry-run
```
Read the dry run. You should see a `PATCH /phone-number/...` with `"squadId": null`, every assistant
prompt ending in `{{caller_context}}`, and the `remember_caller` tool. If the squad line says
`created` instead of `patched`/`nodrift`, **stop** (see the warning in section 5). Then:
```bash
docker compose exec voice-web python manage.py provision_vapi
docker compose exec voice-web python manage.py vapi_doctor
```
**Turning it off (rollback):** set `HHT_DYNAMIC_GREETING=0`, `docker compose up -d voice-web`,
then `provision_vapi` (this re-attaches the squad). To stop only the name greeting instantly, without
any of this, switch off "Greet callers by first name" on `/dashboard/capabilities/`.

---

## 5. Vapi side: the number, the squad, the server URL

### 5.1 Phone number (from Vapi's docs, `fern/phone-numbers/free-telephony.mdx`)
1. In the Vapi dashboard open **Phone Numbers**, select **Create Phone Number**, choose **Free Vapi
   Number**, enter a US area code, select **Create** (it can take a few minutes to become active).
   Free Vapi numbers are inbound only; to use an existing business number see
   `fern/phone-numbers/import-twilio.mdx`.
2. Copy the number's **ID** (not the phone number itself) into `VAPI_PHONE_NUMBER_ID` in
   `voice/.env` on the VPS. **NOT VERIFIED:** where exactly the dashboard shows the ID; check in the
   Vapi dashboard under Phone Numbers > your number.
3. You do **not** need to click "Inbound Settings" or type the server URL by hand: `provision_vapi`
   sets both (squad or nothing, and `https://voice.happytimeweed.com/api/voice/vapi`). If you prefer
   clicking, Vapi's docs say the number has **Inbound Settings** (Assistant or Squad) and a
   **Server URL** field (`fern/server-url/setting-server-urls.mdx`). With the dynamic greeting ON,
   Inbound Settings must be **empty** (no assistant, no squad, no workflow).

### 5.2 Squad
Set `VAPI_SQUAD_ID=2b132e78-6b37-4b12-b99a-17d23f8906e7` in `voice/.env`.

> **One agent or five (`HHT_SQUAD_MODE`).** `single` (the default) makes the squad ONE member, the
> `concierge`, with no `assistantDestinations`: it greets, answers hours/deals/returns/payment from the
> knowledge base, runs the product questions, handles vendors and problems, and transfers to a person
> only with the consult transfer below. The same squad id is kept (provision PATCHes it). The five old
> assistants are **not** changed or deleted in Vapi; they stay for rollback. Until `provision_vapi`
> has created the concierge, everything keeps answering as the five-agent squad. The dry run says
> `HHT_SQUAD_MODE=single`, shows `assistant concierge created` (or `patched`), the five old agents as
> `skipped ... left as is in Vapi for rollback`, and the squad PATCH with exactly one member.
> **Roll back:** set `HHT_SQUAD_MODE=multi` in `voice/.env`, restart, `provision_vapi --dry-run`, then
> `provision_vapi` (it re-PATCHes the five agents and the five-member squad onto the same squad id).
> Dashboard edits to the concierge are saved and used by the next `provision_vapi`, but the dashboard's
> Publish button does not push the concierge yet (`dashboard/publish.py` lists only the five old roles).

> **Transfers ask first (`HHT_TRANSFER_CONSULT`, default on).** Every transfer to a real person (a
> store line, and the owner for an allowlisted vendor) uses Vapi's `warm-transfer-experimental`: the
> caller is put on hold, Vapi calls the person, a short transfer assistant says "Hi, it's the Happy
> Time phone line. I have Sam on the line about a broken cart. Can you take the call?" and connects
> the caller only on a spoken yes. No, no answer, voicemail or an automated greeting cancels: the
> caller hears "I'm sorry, nobody from the team can take the call right now. I can take a message and
> have someone call you back." and the agent takes the message. The team hears only the caller's
> first name (or "a caller") and the reason, never a phone number or the caller's history. The call
> log shows the transfer as `connected`, `declined`, `no_answer`, `voicemail` or `unavailable` (Vapi
> did not say why) and the outcome "Transfer: person unavailable" when no message was taken.
> `HHT_TRANSFER_CONSULT=0` + `provision_vapi` restores the old transfers (straight through with a
> spoken summary, and the allowlist's direct forward to the owner).

> **How `provision_vapi` picks the squad.** With `VAPI_SQUAD_ID` set, it updates **that** squad only
> (`PATCH`, never a new squad), whatever its name: if it has no record of its own yet it adopts the id
> (the dry run shows `adopt squad ...06e7 from VAPI_SQUAD_ID, PATCH only`) and binds the number to it;
> an id Vapi does not know is an error, not a create. If its own record holds a **different** id it
> stops before changing anything and prints both ids: if `VAPI_SQUAD_ID` is the live squad, re-run
> with `--force-squad-id`; if the recorded one is, put that id in `VAPI_SQUAD_ID`. The doctor's
> `config.squad_id` line warns about the same disagreement. Without `VAPI_SQUAD_ID` it falls back to
> its record, then the name `Happy Time Voice`, then creates one (the dry run then shows `created`
> for the squad: do not apply such a run against a live account).

### 5.3 Webhook security
Our server accepts a Vapi event only with the shared secret (`VAPI_WEBHOOK_SECRET`) in the
`X-Vapi-Secret` header or a matching HMAC signature (`voice/signing.py`). `provision_vapi` writes the
secret onto every agent, tool and the number. Vapi's docs call that inline `secret` field "legacy"
(`fern/changelog/2025-05-20.mdx` removed it from the schema; `fern/server-url/server-authentication.mdx`,
"Legacy X-Vapi-Secret Support", describes the replacement: a Custom Credential with header
`X-Vapi-Secret`, Bearer prefix off). If the doctor says "no webhook auth visible" **and** calls fail
with 401 in our logs (`docker compose logs voice-web | grep "webhook rejected"`), create that
credential in Vapi (Dashboard > Integrations > Server Configuration > Add Custom Credential, per the
same doc) with the same secret value, typed directly into Vapi.

---

## 6. Run the doctor (the read-only checkup)

```bash
docker compose exec voice-web python manage.py vapi_doctor
docker compose exec voice-web python manage.py vapi_doctor --fix-hints   # also says why + which Vapi doc
docker compose exec voice-web python manage.py vapi_doctor --json        # machine-readable
```
What it does: only **GET** requests (to Vapi, to the budtender health page, to our own webhook
address and `/healthz`). It never changes anything, never places a call, never sends a fake call
event, and never prints a secret (secrets show as `set`/`missing`; ids and phone numbers show only
their last 4 characters). It ends non-zero when anything FAILs.

Each line is `[PASS]`, `[WARN]`, `[FAIL]` or `[SKIP]` with a one-line `fix:`. It checks: your
settings and where each came from; budtender reachable and accepting the token; the webhook address
answers (a `405` is correct: the route only accepts POST); the squad and its five agents (model,
webhook address, secret, transfer tool); the tools; the phone number binding (dynamic greeting on =
bound to nothing + our server URL; off = bound to `VAPI_SQUAD_ID`); no workflow bound; and which AI
steps may "think".

Run it on the VPS. On Windows, `budtender.internal` does not exist and the voice database is not
there, so those lines fail or warn.

### About "thinking" (the owner wants no AI step to think)
* **Our server-side steps** (call summaries, prompt helper, evals) already send Google a thinking
  budget of 0 (`core/services/gemini.py`, both `generate` and `generate_stream`). The doctor checks it.
* **The phone agents run inside Vapi** on `google / gemini-2.5-flash`. Vapi's Google model settings
  have **no thinking switch**: the `GoogleModel` fields are `model, provider, temperature, maxTokens,
  messages, tools, toolIds, toolRefs, knowledgeBase, emotionRecognitionEnabled, numFastTurns,
  realtimeConfig` (`fern/apis/api/openapi.json`; `fern/providers/model/gemini.mdx`). Vapi offers a
  thinking setting only for Anthropic (`AnthropicThinkingConfig`) and a `reasoningEffort` only for
  OpenAI. Whether Vapi turns Gemini's thinking off by itself is not documented.
* **OWNER DECISION:** Google's `gemini-2.5-flash-lite` does not think unless asked (Google's
  documentation; **NOT VERIFIED** here, Google's site was unreachable), and Vapi lists it as a
  supported model. Switching is a quality/speed trade-off you decide. Set it per agent on the
  dashboard Agents page (it survives restarts, section 7.1). Changing `ASSISTANT_MODEL` in
  `voice/voice/constants.py` only reaches agents that already exist through `seed_kb --refresh`,
  which also resets every other dashboard prompt edit. The doctor never changes models.

---

## 7. Publishing safely: what overwrites what

| Action | Where | What it changes in Vapi | What it does NOT touch |
|---|---|---|---|
| **Publish all** | `/dashboard/publish/` | every active agent (`PATCH /assistant`: prompt, model, voice, transcriber, server URL + secret, tools list) and the squad (`VAPI_SQUAD_ID`) plus any per-store squads | the phone number, tool definitions (except a newly bound one), knowledge files |
| **Publish on save** | automatic when "Publish prompt edits to the phone instantly" is on (Capabilities) | the same, for the agent you saved | same |
| `provision_vapi` | VPS command line | tools, KB files mirror, all agents, the squad (`VAPI_SQUAD_ID`, else recorded id/name, see 5.2), **and the phone number binding** | nothing outside Vapi |

Anything you change directly in Vapi's dashboard on those agents (prompt, voice, model) is
overwritten by the next Publish or `provision_vapi`. Make changes on our dashboard instead.

Safe routine: `provision_vapi --dry-run` first, read it, then `provision_vapi` when the line is quiet,
then `vapi_doctor`.

### 7.1 The restart keeps your dashboard edits (`seed_kb` is create-only)
The repo-root `docker-compose.yml` starts `voice-web` with `python manage.py seed_kb` every time.
`seed_kb` (`kb/seed.py`) only **adds rows that are missing** (a fresh database, or a seeded row that
was deleted). It never overwrites an existing row, so your edits to each agent's **prompt, model,
voice, tools and greeting**, the seeded hours/address/phone rows (**"Yakima hours"** etc.) and the
FAQ, policy and education rows, including switching a row off or unticking Confirmed, survive every
restart or rebuild. Deals (kind "special") are never touched by the seed.

The reset is deliberate only: `docker compose exec voice-web python manage.py seed_kb --refresh`
(same as `--overwrite`) puts **every** seeded row back to the values in `kb/seed.py`, wiping those
dashboard edits (deals excepted; with publish-on-save on, the reset prompts then go to Vapi). Your
developer uses it to deploy changed seed content, because new text in `kb/seed.py` does not reach an
existing row otherwise.

---

## 8. The dashboard

Address: **`https://voice.happytimeweed.com/dashboard/`**. Login: a Django staff user. Create the
owner login once:
```bash
docker compose exec voice-web python manage.py createsuperuser
```
Pages you asked about (all under `/dashboard/`):

| You want... | Page (menu name) | Address |
|---|---|---|
| Deals and hours | **Specials** | `/dashboard/specials-hours/` |
| Knowledge base (FAQ, policies, store facts, blog...) | **KB**, **Policies** | `/dashboard/kb/`, `/dashboard/policies/` |
| Agents and prompts (greeting, prompt, model, voice) | **Agents**, **Flow** | `/dashboard/agents/`, `/dashboard/flow/` |
| On/off switches (transfers, greet by name, memory, allowlist, Dutchie deal sync, alerts) | **Capabilities** | `/dashboard/capabilities/` |
| Vendor allowlist + owner phone | **Vendor allowlist** | `/dashboard/vendor-allowlist/` |
| Vendor callback requests | **Vendor** | `/dashboard/vendor-callbacks/` |
| Customers | **Customers** | `/dashboard/customers/` |
| Calls live / call log / history / one call + transcript | **Calls**, **History** | `/dashboard/calls/`, `/dashboard/calls/log/`, `/dashboard/calls/history/` (click a call for its transcript and "fetch full conversation") |
| Website chat history | **Chat** | `/dashboard/calls/chatbot/` |
| Escalations (people who wanted a human / complaints) | **Escalations** | `/dashboard/escalations/` |
| Do AI suggestions turn into purchases, plus calls (volume, busiest hours, outcomes, transfers), the chat funnel and search misses; filter by days, store, channel | **Analytics** | `/dashboard/analytics/` (full chat funnel: `/dashboard/analytics/chat/`) |
| Background jobs health | **Health** | `/dashboard/health/` (service health: `https://voice.happytimeweed.com/healthz`) |
| Keys and numbers | **Credentials** (owner only) | `/dashboard/credentials/` |
| Ranking weights | **Weights** | `/dashboard/weights/` |
| Test the agents before calling | **Console** | `/dashboard/playground/` |
| Push changes to Vapi | **Publish** | `/dashboard/publish/` |

### 8.1 Change a deal (step by step)
1. Open **Specials** (`/dashboard/specials-hours/`) and select **+ New row** (or **Edit** on a row).
2. **Kind:** Weekly special. **Store:** type exactly `yakima`, `mount-vernon` or `pullman`, or leave
   it blank for all stores. **Label:** a short name.
   **Value:** exactly what the agent may say (the agent never invents a number, so write the % and
   the products here).
3. Optional **Valid from / Valid to**: the deal starts and stops being spoken on its own dates. A
   row outside its dates shows "not running".
4. Tick **Confirmed** (an unconfirmed row is spoken as "call to confirm") and **Is active**. Save.
5. It is live on the next call; no Publish needed. Rows labelled "Dutchie #..." come from Dutchie
   when "Sync deals from Dutchie" is on (Capabilities); edit those in Dutchie.

### 8.2 Change hours
Same page, filter **Hours**, **Edit** the store's row, change **Value**, keep **Confirmed** ticked,
save. Live on the next call, and kept across restarts of `voice-web` (7.1); only
`seed_kb --refresh` puts the code values back.

### 8.3 How the vendor allowlist works
* On `/dashboard/vendor-allowlist/`: set **Owner phone** (owner only), add each vendor's exact number.
* A call from an **active** number on the list skips the normal agent: a short per-call voice says
  "Thanks for calling Happy Time. One moment while I see if the owner is free.", the vendor is put on
  hold and your phone rings. You hear "Hi, it's the Happy Time phone line. Acme Distribution is
  calling Happy Time, about <the entry's note>. Do you want to take the call?" Say yes to be
  connected; say no (or let it ring out) and the vendor hears you are not available and is asked
  for a message, which lands on **Vendor** callbacks. Only exact numbers match. With
  `HHT_TRANSFER_CONSULT=0` it is the old direct forward ("Connecting you now, one moment.").
* Anyone else who says they are a vendor talks to the AI vendor agent: it asks them to hold, tries
  the store line, and if nobody answers takes a callback (it appears on **Vendor** callbacks).
* It needs: dynamic greeting on (section 4), the switch "Send allowlisted vendors straight to the
  owner" on, an owner phone, at least one active number. The coloured box at the top of the page
  says which one is missing.
* **Test without calling:** the page has a test box ("would this number be routed?"); it uses the
  same matcher as a real call and places nothing.

### 8.4 Customer memory
* For a caller identified by their caller ID, the agents get a short note (taste, style, and since
  this release a `Remembers:` line with a summary of past calls). They use it silently and never read
  it out. After the call, only what the customer said is sent to their profile.
* Switch: "Remember what repeat callers like" on Capabilities.
* **To clear one customer's memory:** open **Customers**, click the customer, and press **Clear this
  customer's memory** in the *Conversations* card (it asks you to confirm). It calls budtender's staff
  endpoint `POST /api/v1/customer/memory/clear` with the customer's live id and your dashboard
  username (recorded in budtender's audit log), then shows a message: cleared, "no such customer", or
  "budtender is unreachable / refused - memory was NOT cleared". If the card says *Not linked to a live
  customer record*, the button is not offered (the page could not tell which live customer this is
  without guessing); nothing to run by hand. **NOT VERIFIED** against a live system.
* The same card lists every website chat and phone call for that customer, with an AI summary button
  per conversation and **Summarize all** (one paragraph over the latest 30). Summaries are for staff
  only, are cached until a conversation grows (press **Regenerate** to redo one), and each press is one
  paid Gemini call (Summarize all: up to 30 + 1).

### 8.5 The test console (before you spend call minutes)
`/dashboard/playground/` (**Console**): type as a customer and see the answer plus which tools ran.
It uses the shared text brain (`voice/chat.py`) with the same knowledge base, tools and safety
rules, **not** the Vapi squad prompts. With `VAPI_PUBLIC_KEY` set, a "Start Vapi call" button opens
a real browser call to the faq agent only; such a call skips the phone number, so it gets no name
greeting, no memory and no allowlist.

To test what the **phone** itself says without a call, run the voice simulation: Gemini plays the
concierge with exactly the prompt and tools Publish sends to Vapi, through ~73 scripted calls (hours,
shopping, price-asks-size, under-21, vendor, defects, "I want a real person", multi-turn calls), and
each call is scored, including a check on every spoken turn for "let me get a member that knows".
It costs real Gemini money (about $0.70 a run):
`docker compose exec voice-web python manage.py eval_answers --live --channel voice --trace /tmp/voice.jsonl`.
Products come from a small test catalog, not your live menu.

### 8.6 Bulk upload and bulk edit
Every list page (**Specials**, **KB** lists, **Policies**, **Vendor allowlist**) has a toolbar:
**Download template | Export current | Bulk upload | Edit all**. A ready-made specials sheet is also in
`docs/templates/specials-template.csv`.
* **Spreadsheet round trip:** Download template (or Export current) -> fill it in Excel or Sheets ->
  save as CSV -> **Bulk upload**. Step 1 only *checks* the file and shows how many rows would be new,
  updated, unchanged or wrong (each error names its spreadsheet row); nothing is saved. Step 2,
  **Apply**, saves every good row in one go. Limits: 1 MB, 2,000 rows.
* **Rows are matched on their key** (specials/hours: store + label; FAQ: key; vendor: phone...). A row
  missing from your file is never deleted. To delete, put `yes` in that row's `delete` cell **and**
  tick "Allow deletes". Leave a yes/no or number cell blank to keep the current value.
* The two example rows in a template are switched off (`is_active` = no), so uploading it untouched
  adds nothing the agent will say. Rows labelled "Dutchie #..." are overwritten by the Dutchie sync;
  edit those in Dutchie.
* Text starting with `=` or `@` is refused (a spreadsheet could run it); put an apostrophe in front
  if it really is text. Downloads add that apostrophe and uploads remove it.
* **In place:** Edit swaps a row into a form, Save keeps you on the page, **+ New row** adds at the
  top. Tick rows for Activate / Deactivate / Delete / Set a field on all of them. **Edit all** turns
  every visible row into inputs; **Save all** saves nothing unless every row is valid.
* Each bulk write adds one line to the `BulkBatchLog` table (who, which list, how many rows, never the
  data) and sends the budtender store-facts refresh once, not once per row.

---

## 9. Test calls (each costs Vapi minutes; you approve them)

Before calling: `vapi_doctor` shows no FAIL. After each call, look at **Calls** / **History**
(transcript, outcome, tools used), **Escalations**, **Vendor** callbacks and **Analytics**.

| # | Call | Good sounds like | Check afterwards |
|---|---|---|---|
| 1 | **New caller** (a phone never used before) | Standard greeting; asks your first name once, naturally; never stalls your question for it | call log entry; a second call from that phone greets you by name |
| 2 | **Returning customer** (a phone with purchase history) | "Welcome back to Happy Time, Sam!"; picks lean to what you buy; never reads your history or number aloud | transcript: no history recited |
| 3 | **Deals question** ("any deals today?") | Exactly the confirmed, running rows from Specials; if none: the fixed "no deal posted" sentence | the deal you added in 8.1 is quoted, an expired one is not |
| 4 | **Hours** ("when do you close?") | The hours row for that store, nothing invented | matches Specials > Hours |
| 5 | **Product recommendation with a ratio** ("a 1:1 gummy for evening") | Asks size before any price; gives up to 3 in-stock picks with THC/CBD numbers read from the tool. There is no dedicated "ratio" filter in the tool: listen that it does not invent a ratio | tools used: `suggest_products`; compare with the website chat (section 10) |
| 6 | **Vendor NOT on the list** ("I'm dropping off a delivery") | Asks you to hold, tries the store line, then takes your name/company/reason and promises a callback within one business day | **Vendor** callbacks has the request |
| 7 | **Vendor ON the list** (call from an allowlisted number) | One line ("One moment while I see if the owner is free"), then hold; the owner's phone rings and hears the entry's name; owner says **yes** and is connected | call log outcome "Vendor sent to owner", transfer `connected`; allowlist row "Matches" +1 |
| 8 | **After hours** | **There is no after-hours mode in the code**: the AI answers as usual 24/7 and quotes the hours; a transfer rings the store line, which may go unanswered | decide if you want different behaviour |
| 9 | **Transfer to a person, accepted** ("can I talk to someone?" twice) | Asks "who should I say is calling, and what is it about?" once, then "one moment while I see if someone from the team is free"; the store phone rings; staff hear name + reason and say **yes**; you are connected | transfer `connected` in the call log |
| 10 | **Something unsafe** (e.g. "my dog ate an edible", or "I'm 19") | The fixed owner-approved safety sentence first, then offer of a team member; under-21: no product help | transcript shows the exact safety sentence |
| 11 | **No silence, no handoff language** (single mode). Open with the need: "hi, I want a one-to-one gummy for tonight", then mid-call "what time do you close?", then "I'm also dropping off a delivery later" | It goes straight to the gummy questions (never "how can I help?"), answers the hours and returns to the pick, handles the vendor part itself. Never "let me get a member / someone who knows / our budtender"; no gap longer than a short "one sec, let me check" before each answer | transcript: one agent name only, no handoff phrase; tools used `suggest_products`, `faq_lookup` |
| 12 | **Transfer declined**: as 9, staff say "no, not now" | You hear "nobody from the team can take the call right now, I can take a message"; it takes your message | transfer `declined` or `unavailable`; an Escalation email / Vendor callback |
| 13 | **Transfer not answered**: as 9, nobody picks up the store phone | After the ring-out (about 20 to 60 seconds) the same "can't take the call" line, then a message | transfer `no_answer` or `unavailable` |
| 14 | **Transfer reaches voicemail**: as 9, the store phone goes to voicemail | The transfer assistant hangs up on the voicemail; you hear the "can't take the call" line; nothing is left on the voicemail | transfer `voicemail` or `unavailable`; check the store voicemail is empty |
| 15 | **Owner declines an allowlisted vendor**: as 7, owner says "no" (or does not answer) | The vendor hears "the owner isn't available right now... what would you like me to pass along?", gives a message and hears the callback window | Vendor callback logged; outcome "Vendor callback" (or "Transfer: person unavailable") |

---

## 10. Website chat vs phone: what is shared, what is not

**Shared (same backend):** product ranking and live stock (budtender API), customer profiles and
memory, deals and store facts you type here (budtender refreshes its copy when you save, per
`kb/signals.py`), the safety lines in `voice/safety_copy.py`.

**Not identical:**
* The website chat runs **the website's own Gemini prompt** (in the website repository, not here);
  the phone runs **the Vapi squad prompts** (Agents page). Wording and flow differ.
* The website identifies people only by a typed phone number (a weaker "unverified" tier): it gets
  less memory, and the `Remembers:` line only if `HHT_MEMORY_WEB_SUMMARIES` is set on budtender.
* Budtender's own AI calls run with thinking off (its rule in `budtender/CLAUDE.md`); the website's
  own prompt lives in the website repository (**NOT VERIFIED**); the phone agents' model may think
  (section 6).

**What to compare:** ask both the same product question for the same store; the top picks and prices
should come from the same in-stock list. Ask both for today's deals; both should quote the same rows.

---

## 11. Troubleshooting

| Symptom | Most likely cause | Fix |
|---|---|---|
| Not greeted by name / no memory | `HHT_DYNAMIC_GREETING` off, or the number still bound to the squad | section 4, then `vapi_doctor` (line `vapi.phone_number`) |
| Allowlisted vendor still gets the AI | same as above, or switch off, owner phone blank, number not exact | the box at the top of `/dashboard/vendor-allowlist/`; `vapi_doctor` |
| Call rings then drops / "an error occurred" with the dynamic greeting on | our server unreachable or too slow (7.5 s limit) | `vapi_doctor` lines `webhook.*`; `docker compose logs voice-web` |
| Tools fail, agent says it cannot check stock | webhook secret mismatch (401) or budtender down | `vapi_doctor` lines `vapi.member.*`, `budtender.*` |
| Transfer fails | `HHT_TRANSFER_NUMBER_*` missing (a placeholder `+10000000000` is sent), or "Transfer phone calls to a person" off | Credentials page or `.env`, then `provision_vapi` |
| Deal not spoken | row not Confirmed, not active, or outside its dates | Specials page badges |
| Hours or prompt edits came back old | someone ran `seed_kb --refresh` (section 7.1) | re-type the edit on the dashboard |
| Dashboard Publish says "tool not provisioned: remember_caller" | dynamic greeting on but `provision_vapi` not run yet | run `provision_vapi` |
| `provision_vapi` prints "dry-run" though you have a key | the key is only on the Credentials page, not in `voice/.env` | put it in `voice/.env` on the VPS |
| Doctor: budtender unreachable | ran on Windows, or `web` container down | run it on the VPS with `docker compose exec voice-web ...` |

---

## 12. Security: do not

* Do **not** paste keys, tokens or `.env` contents into chats (including AI assistants), emails,
  tickets, screenshots or shared folders. Nobody helping you needs them; the doctor shows only set/missing.
* Do **not** keep `voice/.env` in OneDrive or any synced folder.
* **Rotate the Vapi private key** if it was ever shared: in Vapi, **API Keys** (Vapi docs
  `fern/security-and-privacy/api-keys.mdx`: Dashboard > API Keys > Private API Keys > Add Key), put
  the new one in `voice/.env` on the VPS, `docker compose up -d voice-web`, run `vapi_doctor`, then
  delete the old key in Vapi. Rotate `VAPI_WEBHOOK_SECRET` the same way and run `provision_vapi`
  afterwards (it rewrites the secret on every agent and tool).
* **Rebuild images after the `.dockerignore` fix.** Older builds copied `voice/.env` and
  `voice/secrets/` into the budtender images (root `.dockerignore`, commit `1f15d77`). Rebuild with
  `docker compose up -d --build`, and if any old image ever left the VPS, rotate every secret that
  was in `voice/.env` at that time.
* Do **not** give the website `HHT_BACKEND_TOKEN` (it opens the customer database); the website gets
  `HHT_VOICE_TOKEN` only.

---

## 13. What could NOT be verified from the repository

* Single mode (one concierge agent): how it SOUNDS. The tests prove the prompt text, the tools and
  the one-member squad offline; whether Gemini follows "never go quiet" and "act on the first sentence"
  on a real call can only be heard (test calls 11, 5, 3, 6).
* Consult transfers: Vapi's `warm-transfer-experimental` with a `transferAssistant` is documented in
  VapiAI/docs (`fern/calls/assistant-based-warm-transfer.mdx`) but its `transferAssistant` field is not
  in the published OpenAPI `TransferPlan`, so whether the live API accepts it, how reliably the
  transfer assistant tells a person from voicemail, whether the "can't take the call" line appears in
  the end-of-call transcript (the call log reads it back), and whether the allowlist's
  `call.timeElapsed` hook may run a transfer, are unverified: test calls 9, 12 to 15. If Vapi refuses
  the payload (`provision_vapi` shows a 400), set `HHT_TRANSFER_CONSULT=0` and re-run.
* Any live Vapi behaviour: no call was made and Vapi was not reachable while writing this. Including:
  whether Vapi applies the per-call variables to every squad member across handoffs; whether `PATCH
  /phone-number` with `squadId: null` really unbinds the number (README "Dynamic greeting rollout");
  real `assistant-request` latency; whether Vapi still honours the legacy inline `server.secret`.
* Whether Vapi disables Gemini 2.5 Flash thinking itself; that 2.5 Flash-Lite does not think by
  default (Google's documentation, not reachable here).
* Exact Vapi dashboard labels beyond the cited docs (where the phone number ID is shown).
* The folder name of the repository on your VPS.
* The memory-clear command in 8.4 (never run against a live system).
* What the website chat's own prompt does (different repository).
* The doctor's output against your real account: its tests use simulated Vapi answers; field names
  (`squadId`, `assistantId`, `workflowId`, `server.url`, `server.credentialId`, `model.provider`,
  `model.model`, `model.toolIds`, `model.tools[].type`) follow Vapi's published OpenAPI file.
