# Happy Time budtender: handoff for the local agent (finish everything)

Written 2026-10-08 at the end of a long cloud session. Read this whole file before touching anything. Everything here is
based on what was actually done and verified in that session; anything not verified is marked **UNVERIFIED**.

---------------------------------------------------------------------------------------------------------------------

## 0. How to use this file (paste this block to the local agent first)

> You are finishing the Happy Time AI budtender project. Two repos, two open DRAFT pull requests, one working branch name
> `claude/practical-dijkstra-8fcrma` in both. Read `docs/NEXT-STEPS-HANDOFF.md` fully, then `docs/contracts/*.md`, then each
> repo's CLAUDE.md files. Work the tasks in section 6 in the order given. Never push to `main`, never merge a PR, never deploy,
> never paste or print a secret. After every task: run that repo's checks (section 3), commit on the working branch with a clear
> message, push, and report what was verified and what was NOT. If something here contradicts the code, trust the code and tell
> the owner. Ask the owner before any outward-facing or hard-to-reverse action.

---------------------------------------------------------------------------------------------------------------------

## 1. Ground rules (non-negotiable; they come from the repos' own invariants and the owner's instructions)

1. **No secrets in chat, tickets, commits, logs or screenshots.** The owner's Vapi key lives in `voice/.env`. Use hidden prompts
   (`read -rs`) when setting values. `.env` files are gitignored in both repos; keep it that way.
2. **No cost or margin in any customer/website-facing response** (`budtender/tests/test_no_leak.py` pins it). Never weaken it.
3. **Numbers-Guard**: the phone/chat agent never invents a price, stock count, THC figure, hours or deal. Facts come from tools or
   the knowledge base rows. **Price gate**: a price is per size; size is asked before any price (`needs_size`).
4. **Compliance** (`/home/user/happytimeweed/LCB-CONTENT-COMPLIANCE.md` in the website repo): experiential, hedged effect words only;
   never therapeutic or medical claims; no "free/BOGO" wording; under-21 is declined for retail.
5. **Identity and isolation** (see `budtender/CLAUDE.md`): identity is a phone merged only on a Dutchie id; a phone typed on the
   website is NOT proof of identity; trust tiers in `docs/contracts/customer-memory-v1.md`; session tokens must be server-minted
   shape; nothing personal ever goes to the browser; one person must never see another's data.
6. **No AI step may "think".** Every model call sets thinking off (Gemini 2.5: `thinking_budget=0`). Backend calls go through
   `budtender/llm.py` (a test fails if any call bypasses it); the voice wrapper `voice/core/services/gemini.py` defaults to 0 for
   `generate` and `generate_stream`; the website turn route sets `thinkingBudget: 0`. The one exception that cannot be fixed in
   code: the phone agent's model runs INSIDE Vapi (`google / gemini-2.5-flash`) and Vapi's Google model settings have no thinking
   switch (see Decision D1).
7. **Staff-facing summaries are staff-only.** Nothing about a customer's profile is ever said or shown to that customer.
8. **Tests must stay offline and key-free** (mock Gemini, Vapi, Dutchie, SMTP). No test may call a real service.
9. **Do not delete the old multi-agent roles/rows**; they are the rollback.
10. Do not run formatters over files you did not change. Use small targeted edits in shared files.

---------------------------------------------------------------------------------------------------------------------

## 2. Repos, branches, environments

| Thing | Value |
|---|---|
| Backend + voice repo | `github.com/happytimecustomerexperience-droid/happytime-budtender` |
| Website repo (Next.js on Vercel) | `github.com/happytimecustomerexperience-droid/happytimeweed` |
| Working branch (both repos) | `claude/practical-dijkstra-8fcrma` |
| Draft PRs | backend/voice **#1**, website **#26** (both base `main`, not merged) |
| Production branch the VPS deploy script pulls | `feat/pos-roles-queue` (identical to `main` at commit `a420220` when the work started) |
| VPS project folder | `~/happytime-budtender` (assumed from `deploy-vps.sh`: **UNVERIFIED** on the server) |
| Compose services | `db, db-backup, redis, migrate, web, pos-web, warmer, celery-worker, celery-beat, voice-db, voice-web, voice-redis, voice-worker` |
| Voice dashboard | `https://voice.happytimeweed.com/dashboard/` (staff login; superuser for owner-only pages) |
| Vapi webhook | `https://voice.happytimeweed.com/api/voice/vapi` |
| Budtender API (internal) | `http://budtender.internal:8000` (voice talks to it with `HHT_BACKEND_TOKEN`; website uses the website-scoped token) |
| Vapi squad id (not secret) | `2b132e78-6b37-4b12-b99a-17d23f8906e7` |
| Owner's local checkout | `C:\Users\vladi\OneDrive\Desktop\happytime-budtender` (OneDrive syncs the folder: `.env` files there are copied to Microsoft's cloud; recommend moving the repo out of OneDrive) |

Two Django projects live in the backend repo: **root** (`budtender/`, `bundles/`, `pos/`, `core/`) and **`voice/`** (Vapi control
plane, owner dashboard, `kb/`, `crm/`). Voice has its own database (`voice-db`) and its own tests/venv.

Contracts written during the session (read them, they are binding for the features they describe):
`docs/contracts/search-v2.md`, `docs/contracts/customer-memory-v1.md`, `docs/contracts/suggestion-analytics-v1.md`.
Tutorial for the owner: `voice/docs/VAPI-SETUP-AND-TESTING.md` (13 sections; keep it true after every change you make).

---------------------------------------------------------------------------------------------------------------------

## 3. How to run the checks (do all of these before every push)

### Backend (root project), from the repo root
bash:
```bash
SQL_ENGINE=django.db.backends.sqlite3 SQL_DATABASE=:memory: DEBUG=1 uv run pytest budtender tests -p no:cacheprovider -W ignore -q
SQL_ENGINE=django.db.backends.sqlite3 SQL_DATABASE=:memory: DEBUG=1 uv run python manage.py makemigrations --check --dry-run
uv run ruff check budtender core
```
PowerShell equivalent: `$env:SQL_ENGINE='django.db.backends.sqlite3'; $env:SQL_DATABASE=':memory:'; $env:DEBUG='1'` then the same `uv run` commands.
(Note: the env var is `DEBUG`, not `DJANGO_DEBUG`, for the root project. `budtender/CLAUDE.md` was corrected.)
Last verified count: **966 passed, 1 skipped**. Known pre-existing ruff items: one E731 in `budtender/tasks.py` (`_fold_history`), five E702 in `budtender/facets.py` `warm()`.

### Voice, from `voice/`
```bash
cd voice
DJANGO_DEBUG=1 uv run pytest -q -p no:cacheprovider        # about 2 minutes
DJANGO_DEBUG=1 uv run python manage.py makemigrations --check --dry-run
uv run ruff check .
```
Last verified count: **2057 passed, 2 xfailed**, ruff clean.

### Website, from the website repo root (Node; run `npm ci --ignore-scripts` once)
```bash
npx tsc --noEmit            # if .next/dev/types/validator.ts is truncated, delete that generated file and re-run
npx eslint <touched files>
npx tsx scripts/test-chat-flow.ts                                   # last: ALL 160 PASSED
npx tsx --test --test-force-exit tests/similar/*.test.ts tests/questionnaire/*.test.ts   # last: 131 pass
node scripts/compliance-check.mjs                                   # 0 errors
```
Hermetic e2e (every `/api/*` is mocked in the browser; the server must hold NO live secrets):
```bash
NEXT_PUBLIC_CHATBOT_ENABLED=true GEMINI_API_KEY= VERTEX_PROJECT= HHT_BACKEND_URL= HHT_BACKEND_TOKEN= \
DUTCHIE_YAKIMA_POS_KEY= DUTCHIE_MTVERNON_POS_KEY= DUTCHIE_PULLMAN_POS_KEY= npx next dev -p 3207
npx playwright test -c tests/e2e/chat.playwright.config.ts --project=chromium-desktop
```
Gotchas that cost time: (a) Playwright 1.60 may want a Chromium build you do not have; use a throwaway config that sets
`launchOptions.executablePath` to your installed Chromium and delete it afterwards; (b) BotID's client script calls
`api.vercel.com`; the e2e mocks stub the challenge (`chat-mocks.ts`), otherwise protected fetches fail and some specs look green
because they only wait for a spinner to vanish; (c) never run two `next dev` in one folder (`.next` lock); (d) on Windows Git Bash use
`MSYS_NO_PATHCONV=1` for path env vars; (e) do not `pkill -f` with a pattern that appears in your own shell command.

Anything that needs real Vapi/Gemini/Dutchie/Postgres cannot be tested offline. Say so in your reports instead of implying it was.

---------------------------------------------------------------------------------------------------------------------

## 4. What is built and pushed (by area)

Backend/voice repo, PR #1 (14 commits; all suites green at the last run):
- **Search v2** (`budtender/ranking.py`, `facets.py`, `product_attrs.py`, `product_similarity.py`, views): filters for THC, cannabinoids, terpenes, tags, infusion, extraction, concentrate subtypes (live resin, live rosin, hash rosin, rosin, cured resin...), master Dutchie categories (Flower, Pre-roll, Infused Pre-roll, Concentrate, Vape Cartridge, Disposable Vape, Solid Edible, Liquid Edible, Topical), offset paging capped at 20, options endpoints that never list an empty choice, `products/similar` returning full cards with lab data.
- **Durability + analytics events**: Redis persistence, restart policies, nightly `db-backup` (same disk: copy off the box), append-only chat persist, whitelisted analytics events, funnel and session-timeline endpoints and dashboard pages.
- **Identity and isolation**: seven leak paths closed (guessable tokens, stranger resume, shared screens, per-IP caps, caller-ID beats stale token, HMAC phone hashes); non-identifying/shared phones never resolve a name. `voice/` secrets removed from images via `.dockerignore`.
- **Security hardening** (backend): website token cannot override ranking weights, safe JSON parser, bounded inputs, role-injection screen on tool results.
- **Customer memory v1** (`budtender/memory.py`, `memory_learn.py`, `memory_summary.py`, `llm.py`, `customer_model.py`): tier-gated brief, deterministic learning, per-conversation AI summaries and consolidation (setting `HHT_MEMORY_CONSOLIDATE_AT`, default 10; the owner wrote "1 entries", read as 10), derived profile (ratio, form, extraction, dose, price/THC bands, cadence, pairings, next-likely), tailored ranking/pairing, `manage.py audit_customer`.
- **Suggestion tracking** (`budtender/suggestions.py`, `suggestion_analytics.py`, migration 0014): full-detail snapshot on every suggestion, sibling logic, 10-day outcome attribution, hourly close job, three staff-only API endpoints, `backfill_suggestion_snapshots` and `evaluate_suggestions` commands.
- **Voice**: memory brief + style in the phone prompt (trusted callers only), end-of-call learn; **vendor allowlist** (`voice/voice/vendor_allowlist.py`, dashboard page `/dashboard/vendor-allowlist/`, owner number `HHT_OWNER_PHONE`); **`vapi_doctor`** read-only checker; **create-only `seed_kb`** (restarts no longer erase dashboard edits; `--refresh` to overwrite); `provision_vapi` adopts `VAPI_SQUAD_ID` (never creates a second squad); **single front agent ("concierge")** in a one-member squad (`HHT_SQUAD_MODE=single` default; `multi` kept and byte-identical for rollback); **consult-then-accept transfers** (`voice/voice/consult.py`, Vapi `warm-transfer-experimental`, setting `HHT_TRANSFER_CONSULT`, default on); dashboard Publish now includes the `concierge` role.

Website repo, PR #26 (Vercel previews built OK at the last check):
- Questionnaire v2 (master categories, dead-end steps skipped, concentrate types, "Specify more", "Show 5 more" up to 20, find-similar with lab data).
- Chat shell: restore across reload/navigation, picks close with animation, order-ahead deep link to the exact product (`lib/chat/order-link.ts`), pick handling and add-to-order turn, tab-scoped phone, "Not me - start fresh", forget, analytics (`lib/chat/track.ts`), security hardening of turn/persist/track/similar routes, personal-memory prompt block (`prompts/customer-memory.md`, server-only).
- **Chat transcript persistence fix**: `lib/chat/client.ts persistState` now uses a keepalive `fetch` instead of `navigator.sendBeacon`. Root cause of the empty `/dashboard/calls/chatbot/`: `/api/chat/persist` is BotID-protected and BotID only attaches its headers to fetch/XHR, so every beacon was refused with 403 while the browser reported success. **UNVERIFIED in production** (inferred from code; confirm after deploy, section 5 step 9).

**What was NEVER heard or run for real (all UNVERIFIED):** any live call; Vapi accepting the `transferAssistant` shape; voicemail detection; per-call variables reaching every squad member; the doctor against the real Vapi account; Postgres migrations (all ran on sqlite only); the compose changes (no Docker in the sandbox); the website against a live backend; the real Dutchie transaction shapes for suggestion attribution; the real analytics export shape for the customers page.

---------------------------------------------------------------------------------------------------------------------

## 5. Deploy and verify runbook (owner or a person with VPS access runs this; steps in order, stop at the first surprise)

Details and rationale are in `voice/docs/VAPI-SETUP-AND-TESTING.md`. Summary:

1. **Back up first** (both databases):
```bash
cd ~/happytime-budtender
export COMPOSE_FILE=docker-compose.yml
git status --short                      # must be empty
mkdir -p ~/backups
docker compose exec -T db pg_dump -U "${SQL_USER:-budtender}" "${SQL_DATABASE:-budtender}" | gzip > ~/backups/budtender-$(date +%F-%H%M).sql.gz
docker compose exec -T voice-db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > ~/backups/voice-$(date +%F-%H%M).sql.gz
ls -lh ~/backups
```
2. **Get the code**: either merge draft PR #1 into `main` (owner decision) and update the production branch, or test directly:
```bash
git fetch origin && git checkout claude/practical-dijkstra-8fcrma && git pull --ff-only origin claude/practical-dijkstra-8fcrma
docker compose up -d --build          # runs migrations (root 0012-0014, kb 0009, voice 0005-0006, dashboard 0005) and a create-only seed_kb
docker compose ps
```
   New migrations are additive; code can be rolled back (`git checkout feat/pos-roles-queue && docker compose up -d --build`) without reverting them.
3. **Set credentials without echoing them.** Paste the `putenv`/`askenv` helper from `voice/docs/VAPI-SETUP-AND-TESTING.md` section 3 (or write your own equivalent), then set in `voice/.env` on the VPS: `VAPI_PRIVATE_KEY`, `VAPI_WEBHOOK_SECRET` (generate with `openssl rand -hex 32` if empty), `VAPI_PHONE_NUMBER_ID`, `VAPI_SQUAD_ID=2b132e78-6b37-4b12-b99a-17d23f8906e7`, `HHT_DYNAMIC_GREETING=1`, `HHT_OWNER_PHONE=+1XXXXXXXXXX` (not the phone you test from), per-store `HHT_TRANSFER_NUMBER_*`; `chmod 600 voice/.env`. Optional in the root `.env`: `GEMINI_API_KEY` (AI summaries), `HHT_MEMORY_WEB_SUMMARIES=1` (only if the owner accepts the risk in D3). Check `HHT_BACKEND_TOKEN` is the same in root `.env` and `voice/.env`. The website's own `HHT_BACKEND_TOKEN` env var must hold the WEBSITE-scoped token value, never the master/voice one.
4. `docker compose up -d voice-web`, then `docker compose exec voice-web python manage.py provision_vapi --dry-run`. **Read it.** The squad line must say `adopt squad ...06e7` or `patched`; if it says `created`, STOP. Expect a `PATCH /phone-number/...` with `"squadId": null`, a one-member squad PATCH containing the `concierge` assistant, and prompts ending in `{{caller_context}}`.
5. `docker compose exec voice-web python manage.py provision_vapi`. If Vapi answers 400 on the transfer tool, set `HHT_TRANSFER_CONSULT=0`, restart, re-run (the consult mode `warm-transfer-experimental` / `transferAssistant` is documented in Vapi's guide but not in its published API schema).
6. `docker compose exec voice-web python manage.py vapi_doctor --fix-hints`. Everything should be PASS except one WARN about the phone model thinking (D1). Fix FAILs. If it says no webhook auth is visible AND calls 401 (`docker compose logs voice-web | grep "webhook rejected"`), create the Vapi custom credential with header `X-Vapi-Secret` (section 5.3 of the tutorial).
7. `docker compose exec voice-web python manage.py createsuperuser` if there is no dashboard login.
8. **Run the 13 test calls** in section 9 of the tutorial (accept/decline/no-answer/voicemail transfers; allowlisted vendor accept and decline; new caller with "a 1:1 gummy for tonight"; hours; deals; price asks size first; unlisted vendor; defective cart; "I'm 19"; returning caller greeted by name). Record call ids. Look at the call log, transcript, outcome.
9. **Website**: merge PR #26 (owner), let Vercel deploy, then chat once on the live site and confirm the session appears at `/dashboard/calls/chatbot/` within a minute. If it does not, check Vercel function logs for 403 `bot-detected` on `/api/chat/persist`; fallback plan: persist server-side from the turn route (append the two messages through the backend persist endpoint, idempotent by message index).
10. **Data refresh jobs** (once, then on a schedule the owner picks): `docker compose exec web python manage.py rebuild_customer_history` (wipes and refolds history; Dutchie ids survive), `docker compose exec voice-web python manage.py import_customer_profiles --customers <customers.json> [--baskets <baskets.json>]` (analytics export for the customers page), `docker compose exec web python manage.py backfill_suggestion_snapshots` (dry-run; add `--apply` after reading it), `docker compose exec web python manage.py evaluate_suggestions` (dry-run, then `--apply`), `docker compose exec web python manage.py diagnose_greeting --name Jaime` (finds why anonymous visitors were greeted "Hey Jaime"; read-only), and `audit_customer --phone <number>` on 5 real customers (the profile/ranking sanity check the owner asked for).
11. Security chores (no code needed): rebuild all images (old ones still contain `voice/.env`), rotate the Vapi key (and others in `voice/.env`) if any image/folder was ever shared, copy `~/backups` and the `pgbackups` volume off the box, never run `docker compose down -v`.

---------------------------------------------------------------------------------------------------------------------

## 6. Remaining engineering tasks (do in this order; each is independent unless stated)

For every task: add tests first or alongside, keep suites green, update `voice/docs/VAPI-SETUP-AND-TESTING.md` if the owner-visible behaviour changes, commit, push, report.

**STATUS 2026-10-08 (local agent, this branch at 8da7848; website branch at 1630740).** Suites: root 984 passed,
voice 2398 passed / 2 xfailed, migrations clean, ruff clean (root: the 6 known). Nothing deployed, nothing merged.
- T1: NOT done live (needs the section 5 deploy). Done offline: the voice simulator now runs the provisioned
  concierge (`eval_answers --live --channel voice --trace X`); 3 owner-approved real-Gemini runs: 0 calls (creds) /
  66/73 / 68/73. Fixed from them: "real person" got the refund apology; a transport question got the
  can't-answer-safely line; cannabis basics refused as medical. 8da7848 is not yet re-run live.
- T2 bulk/CSV/inline edit: done (9cf71a4). T3 customers page: done (84d0144; spend-in-range NOT possible, the export
  has no per-order history). T4 conversations + summaries + clear-memory button: done (7c21b02, name-match cap fix
  8ca4123). T5 analytics: done (f207a97, buyer links f561717). T7: done (50fa601).
- T6: phone records only the spoken picks via new `suggestions/shown` (96ba03d; the "limit 3" idea would have broken
  dedupe); website sends source chat|questionnaire (1630740). OPEN: find-similar (`/api/catalog/similar`) records
  nothing (20-item pool, no source) — moving it to budtender `products/similar` is a product call.
- T9: only the unverified-web-summary Gemini skip (bc0fe19). T8, the rest of T9, T10: not started.
- Owner questions added: should "clear memory" also delete the dashboard's AI summaries (it doesn't); the pairing
  tool records a pairing even when the phone stays silent (same over-count class as T6).

### T1. (P0) Re-test the phone line after deploy and fix what the real calls show
The owner's first live test was "dog shit": the entry agent said "let me get a member that knows" and then went silent, routing was unreliable, answers felt made up. The single-agent redesign was built to remove handoffs but was never heard. After section 5, make the test calls and for each defect: read the transcript, find whether it is prompt (`kb/seed.py` concierge sections), tool (`voice/voice/tools/*`), data (KB rows) or Vapi config, fix, re-publish through `provision_vapi` (never edit the live Vapi assistant by hand). Ask the owner for a transcript/call id of any "made-up answer": without it nothing can be diagnosed. Keep the banned-phrase tests (no "member/specialist/someone who knows/transferring you to our budtender") green.

### T2. (P0) Dashboard: in-place editing, bulk edit and CSV template/upload
Nothing of this exists (an earlier agent left no files). Spec (decided):
- **Generic data layer** `voice/dashboard/bulk.py` with a DATASET registry (key/slug, label, model+queryset, natural-key columns, columns with type/required/choices/max length/help/example, row<->dict, validation reusing the existing ModelForms in `dashboard/forms.py KB_FORMS`, apply). Register: weekly specials, per-store hours and the other `StoreFact` kinds, FAQ, education docs, blog docs, policy categories, weight/type taxonomy, vendor allowlist (keep its existing bulk add working). Not bulk-editable: agent prompts, flow canvas, credentials, capabilities, calls/chats history, customers.
- **CSV round trip per dataset**: `GET /dashboard/data/<key>/template.csv` (header + 2 example rows; UTF-8 with BOM; no `#` comment lines, instructions go on the page), `GET .../export.csv`, `POST .../upload` TWO-STEP: step 1 validates everything and shows a preview (create/update/unchanged/error counts, first rows, every error with its spreadsheet line number), writes nothing; step 2 "Apply" commits in ONE transaction (valid rows; optional "stop if any error"). Upserts on the natural key; rows missing from the file are never deleted unless the `delete` column says yes AND "allow deletes" is ticked. Limits: 1 MB, 2,000 rows, decode UTF-8/UTF-8-BOM/UTF-16/Windows-1252, case-insensitive trimmed headers, unknown columns reported, duplicate keys reported.
- **Security**: staff-only + CSRF like the other pages (add routes to `dashboard/tests/test_staff_gating.py` and `test_route_coverage.py`); formula-injection safe both ways (neutralise cells starting `= + - @ tab CR`); autoescape previews (untrusted); never log file contents; audit entry (dataset, counts, user, not the data). Run side effects (KB reindex, Vapi files mirror, publish-on-save) ONCE per batch, not per row.
- **In-place editing, no redirect** (htmx 1.9.12 and Alpine 3.13.3 are already loaded in `templates/dashboard/base.html`; add no new dependency): Edit swaps the row into an inline form; Save `hx-post` returns the re-rendered row or the form with field errors; toast "Saved"; Cancel restores; Add-new inline at top; Delete with `hx-confirm`. The old standalone edit URLs keep working for non-JS users but redirect back to the originating list page (validated same-site `next`, preserving filters; never an open redirect).
- **Bulk edit**: row checkboxes + select all; action bar (activate/deactivate, delete with confirm, set one field for all selected); "Edit all" toggles every visible row to inputs with one "Save all" (per-row validation, failing rows highlighted with errors, transaction semantics documented). Specials and hours first-class on `/dashboard/specials-hours/`.
- **Toolbar partial** `dashboard/_bulk_toolbar.html` on every registered list page: `Download template | Export current | Bulk upload | Edit all`.
- Also ship a sample `voice/docs/templates/specials-template.csv` and a short "bulk upload" section in the tutorial's dashboard map (section 8).
Existing code to read first: `voice/dashboard/views.py` (`specials_hours` ~l.1234, `kb_manager/kb_source_list/kb_row_new/kb_row_edit/kb_row_delete` ~l.528-650, vendor allowlist views + `forms.py` bulk-add parser), `kb/models.py` (`StoreFact` unique_together (store, kind, label)).
Acceptance: round trip export -> upload shows "unchanged"; preview writes nothing; apply writes exactly the previewed changes; HTMX post returns the row partial and does not redirect; non-HTMX post redirects back with filters; side effect hook called exactly once per batch.

### T3. (P0) Dashboard: customers page upgrade
Today `voice/dashboard/views.py customers_list` (~l.1020) and `templates/dashboard/customers.html` show only name, orders, last purchase, price tier, top category, with a name search. Data lives in `voice/crm/models.py CustomerProfile` (fields already present: `orders, total_spend, aov, cadence_days, recency_days, segment, persona, cohort_month, top_brand, favorite_brands, top_categories, first_order, last_order, is_medical, favorites, store_affinity`), imported by `import_customer_profiles` from an analytics export (`customers.json`, `baskets.json`). Fallback source when no snapshot: budtender's live roster.
Build (decided):
- Columns, all visible, all sortable (click header toggles asc/desc, stable secondary sort, nulls last): Customer, Orders, **Lifetime spend**, **Avg cart**, **# items** (total units; plus items/order), **Frequency** ("every ~N days" + band), Last order, Customer since, **Favorite brand**, **Favorite category**. Money as $1,234.56, empty as an em dash. No raw phone numbers anywhere.
- Filters (single GET form, all combinable, preserved by sort/paging): name search; **brand** (matches favorite brand AND any `favorite_brands`; datalist of distinct brands); favorite category select; frequency band (weekly <=9d, 2 weeks 10-19, monthly 20-45, occasional 46-120, rare >120, unknown); avg cart min/max; items min/max; orders min/max; spend min/max; segment; **date range**: "last order between" and "first order between" with presets (30 days, 90 days, this year, all time); page size 25/50/100; "Clear filters"; summary line "312 of 4,880 customers".
- htmx in-place refresh of just the table (`hx-get`, `hx-push-url`), plain GET fallback. CSV export of the CURRENT filtered+sorted result (cap 50,000, streaming, BOM, formula-neutralised, staff-only).
- Data model: sorting/filtering by JSON is unreliable and production is Postgres while tests are sqlite, so denormalise on `crm.CustomerProfile`: `top_category` (indexed), `items` (int), `brands_text` (lower-case delimiter-wrapped string of top brand + favorite brands) with a migration that RunPython-backfills from the JSON, updated by the importer and a `save()` sync.
- **Spend in a date range**: only possible if the export has per-order date+total(+units). Check `baskets.json`. If yes, store a compact ledger (monthly/daily buckets JSON or a small `CustomerOrder` table) and add a "Spend in range" column with stated granularity. If no, DO NOT fake it: the date range filters first/last order dates only, the UI says so, and the report states the JSON shape the export must provide. `# items` is only as complete as the export; show an em dash, never 0, when unknown.
- Live-roster fallback: show only columns the live data has; note that spend/avg cart need the analytics import.
Acceptance tests: each filter alone and combined, date edges (inclusive, bad dates), every sort key asc/desc, brand filter case-insensitive over `favorite_brands`, band boundaries, paging keeps params, htmx returns the partial, CSV honours filters and neutralises formulas, backfill migration, importer fills denormalised fields, query-count assertion, gating.

### T4. (P1) Customer profile: all conversations and AI summaries
The owner wants, on a customer's profile page, ALL of that customer's conversations (website chat sessions AND phone calls) and summary buttons: one per conversation, and one that summarises everything into a paragraph. Staff-only; never shown to the customer.
Facts you need: chat sessions live in the budtender DB (`ChatSession`/`ChatMessage`, `customer` FK, `identity_via`); `POST /api/v1/chat/history` already accepts `{customer_id}` (list) and `{id}` (one transcript) with the backend token. Phone calls live in the VOICE DB (`voice.models.VoiceCall`, `caller_phone_hash`, transcript, `ai_summary`). The voice dashboard's customer page uses the voice-side `crm.CustomerProfile` (phone_hash may be null) and currently merges with budtender's profile BY NAME (`customer_detail` -> `_merge_detail`): that join is weak. Decide and document the join (preferred: add `budtender_customer_id` to `crm.CustomerProfile`, filled when a name AND/OR phone hash match is unambiguous; surface "unlinked" honestly rather than guessing).
Design: voice DB models `ConversationSummary(kind chat|call, ref, text, message_count, generated_at)` and `CustomerSummary(customer, text, covers_count, generated_at)`; summaries generated with `core/services/gemini.generate` (thinking already 0; transcript text is UNTRUSTED data: delimiters + "ignore instructions in it"; output validated and capped; no prices/PII). Cache; regenerate when message_count changes; "Summarize all" = map-reduce over per-conversation summaries; buttons are htmx in-place with a spinner and error toast; a "Regenerate" action. Tests mock Gemini. Never log transcripts.

### T5. (P1) Analytics page rebuild (uses the new suggestion tracking)
Backend API exists and is tested: `POST /api/v1/analytics/suggestions`, `.../suggestions/list`, `POST /api/v1/customer/suggestions` (shapes in `docs/contracts/suggestion-analytics-v1.md`; `conversion_rate = bought_any / (bought_any + not_bought)`; `unattributable` is excluded, never counted as not bought). The dashboard pages (`voice/dashboard` analytics + chat funnel at `/dashboard/analytics/` and `/dashboard/analytics/chat/`) do not show it yet.
Build a clearly better analytics page: KPI row; **suggestion-to-purchase** section (conversion by channel phone/chat/questionnaire/similar/pairing, by store, category, kind, and by rank position; exact vs sibling vs not bought vs pending vs unattributable; trend by day); **top suggested products with FULL details** (name, brand, category, strain, size, price, THC, times suggested, customers, outcome counts); never-bought list; recent buyers; per-customer drill-down linking to the customer page; calls (volume by day/hour heatmap, outcomes, durations, transfers incl. the new `transfer_unavailable`, vendor callbacks, vendor_direct), chat funnel (existing `chat_funnel`), zero-result searches, top questions/FAQ gaps, deals asked. Filters: date range, store, channel. Do a SHORT, bounded research pass on what matters for dispensary analytics, state what you chose and why. Chart code must follow the `dataviz` skill (accessible palette, light/dark, no chart-junk); inline SVG or a vendored lib, no CDN. Known data caveats to show in the UI: phone suggestion counts are inflated until T6 lands (voice asks 12, speaks 3: filter by rank <= 3 for phone); typed-website-phone attribution is lower trust (`identity_via` on every row); outcomes are only as fresh as the 6-hourly transaction sync + 1 day grace.

### T6. (P1) Small wiring gaps for tracking (needed for honest analytics)
- **Voice** (`voice/voice/tools/suggest.py`, `voice/voice/budtender_client.py`): send `source:"phone"` and a `limit` equal to what is spoken (3) in the search call; anonymous calls currently send no session token.
- **Website**: the search body should carry `source` (`chat` for free-text chat, `questionnaire`, `similar`, `pairing`) so channels are correct. Today web suggestions default to the session channel (mostly mislabelled `questionnaire`). Touch `lib/chat/client.ts runSearch`, `components/features/chat/Questionnaire.tsx`, `SimilarFinder.tsx`, the pairing call, and let `app/api/chat/search/route.ts` + `lib/chat/search-slots.ts` pass the allowlisted value. Add tests.

### T7. (P2) Dashboard gaps left by the single-agent work
- `voice/dashboard/views.py` capabilities page (~l.1698-1725): list the `concierge` member and show the transfer tool for `C.TRANSFER_ROLES` (currently only vendor/escalation).
- `voice/dashboard/monitor.py _OUTCOME_BADGE`: add `transfer_unavailable` (and check `vendor_direct`) so they are not raw grey text.
- `voice/dashboard/flowgraph.py`: add `concierge` to the role list (canvas is documentation only).
- ~~Add a "Clear this customer's memory" button on the customer page~~ DONE (T4): `voice/dashboard/customer_conversations.py`, with the customer's chats/calls and AI summaries.

### T8. (P2) Product decisions that need code once the owner answers (section 7)
- D2 SMS verification (new `identity_via="web_verified"`: send/verify endpoints, per-number and per-IP rate limits, code expiry, provider adapter, no code in logs, UI step in the chat, tests).
- D1 phone model switch (set per agent on the dashboard Agents page; it survives restarts because seeding is create-only).

### T9. (P2) Security and operations follow-ups (from the reviews; none done)
- Add Traefik protection for `voice.happytimeweed.com/admin` and `/dashboard` (ipAllowList or forwardAuth); `DEPLOY.md` still describes a Cloudflare tunnel that the compose file says is not used (trust compose, fix the doc).
- Redis: `--requirepass`, `--maxmemory 512mb --maxmemory-policy volatile-lru`; compose `SQL_PASSWORD:-change-me` -> required (`:?`); `harden-vps.sh` has no ufw/unattended-upgrades; voice Dockerfile runs as root; pin the `uv` image instead of `latest`.
- Server-side rate caps for search/facets/similar (per `X-HHT-Client-IP`), `analytics/summary` is open to the website token (aggregates only: decide), `ChatReplyView` does not carry an "I'm 19" from an earlier turn, the owner's ranking-weights override is cache-only (`AdminRankingWeightsView`): store it in the `Setting` model.
- Unverified web chats still run a Gemini summary that nothing reads: skip the call unless `HHT_MEMORY_WEB_SUMMARIES` is on (one-line change in `memory_summary.py` queueing).
- Website: `npm audit --omit=dev` flagged `next` 16.2.6 critical (non-major 16.x fix available), plus protobufjs, sharp, source-map-js, postcss, nanoid; tailwind needs a major bump (skip). Upgrade `next` within 16.x, run all website checks and the e2e. Also: the old `/api/track` route shares the chatbot rate bucket; PostHog session recording masks inputs but not chat bubble text (consider masking); `vercel.json` header rule `/api/chat/:path*` -> `private, no-store` may or may not override per-route headers on Vercel (check with `curl -I` on production).
- `lib/chat/CLAUDE.md` IDENTITY text and `budtender/CLAUDE.md` should be re-read and kept in sync with the new trust tiers, `llm.py` rule, suggestion tracking, concierge mode (partly done).

### T10. (P3) Cleanups
- `provision_vapi --dry-run` with no `VAPI_SQUAD_ID` set used to persist fake `dryrun-` ids; the single-agent work stopped that for assistants and the pinned squad path, re-check tools.
- New seed text added to `kb/seed.py` only reaches existing rows through `seed_kb --refresh`, which also wipes dashboard edits: consider a per-row "reset to default" button instead.
- `VAPI_ASSISTANT_MODEL` is read but unused. Remove or wire.
- The `store` field on vendor allowlist entries is a label only (does not restrict which store line).

---------------------------------------------------------------------------------------------------------------------

## 7. Owner decisions still open (ask the owner; do not decide silently)

- **D1. Phone model and thinking.** The phone agent runs `google / gemini-2.5-flash` inside Vapi. Vapi exposes a thinking setting only for Anthropic and `reasoningEffort` only for OpenAI models, none for Google (read from VapiAI/docs `fern/apis/api/openapi.json`, 2026-10-08). The owner wants no thinking anywhere. Option: `gemini-2.5-flash-lite` (does not think by default per Google; **UNVERIFIED**, Google's site was unreachable) at some quality/speed trade-off; or an Anthropic model via Vapi with thinking off. Run the 13 test calls on both and let the owner choose.
- **D2. Typed phone numbers identify people (owner's 2026-10-05 design).** Anyone who types a purchaser's number gets a greeting by first name (for purchase-backed rows) and taste-ranked picks. A one-time SMS code would close it; the repo has no SMS sender for customers, and the owner has not named a provider.
- **D3. Website chat reading conversation summaries.** The owner asked that chat can read summaries; the safe default keeps them off for unverified (typed-phone) sessions (`HHT_MEMORY_WEB_SUMMARIES` default off). Turning it on lets a stranger who types someone's number make the bot act on that person's history.
- **D4. "Laya (decision model)".** The owner mentioned it; it does not exist anywhere in either repo and nothing was built. Needs: what it is, where it runs, how to call it, and what training data exists. Training/fine-tuning needs the owner's real transaction data and compute.
- **D5. Consolidation threshold.** The owner wrote "when they reach 1 entries consolidate into 1"; implemented as 10 (`HHT_MEMORY_CONSOLIDATE_AT`). Confirm.
- **D6. Size step skip** in the questionnaire when the backend says no size can narrow (departs from the older rule in `lib/chat/CLAUDE.md`). Confirm or revert.
- **D7. Merging and branches.** Both PRs are drafts against `main`; the VPS script pulls `feat/pos-roles-queue` (same commit as `main` at the start). Decide merge order (backend/voice first, then website) and keep the production branch in sync.
- **D8. Where the local repo lives**: moving it out of OneDrive.

---------------------------------------------------------------------------------------------------------------------

## 8. Key file map

Backend (root): `budtender/{ranking,facets,product_attrs,product_similarity,engine,deals,serializers,views,urls,tasks,models,identity,auth}.py`; memory: `memory.py, memory_learn.py, memory_summary.py, llm.py, customer_model.py`; tracking: `suggestions.py, suggestion_analytics.py, analytics.py`; commands in `budtender/management/commands/` (`audit_customer, backfill_suggestion_snapshots, evaluate_suggestions, diagnose_greeting, prune_site_noise, clear_memory, rebuild_customer_history, ...`); beat schedule in `core/celery.py`; settings in `core/settings.py`; tests in `budtender/tests/`.

Voice: `voice/voice/{provision,constants,caller,webhooks,api,consult,vendor_allowlist,vendor_flow,outcomes,doctor,capabilities,budtender_client,chat,guardrails}.py`, tools in `voice/voice/tools/`, management commands `provision_vapi`, `vapi_doctor`, `seed_kb`; dashboard in `voice/dashboard/{views,urls,forms,publish,monitor,credentials,flowgraph}.py` + `voice/templates/dashboard/*.html`; KB in `voice/kb/{models,seed}.py`; CRM in `voice/crm/`; docs in `voice/docs/`.

Website: chat logic `lib/chat/*` (state, session, client, customer, prompts, schema, search-slots, backend-proxy, order-link, track, reply-guard), UI `components/features/FloatingChatbotV2.tsx` and `components/features/chat/*`, routes `app/api/chat/*` and `app/api/catalog/similar`, prompts `prompts/*.md` (incl. `customer-memory.md`), tests `scripts/test-chat-flow.ts`, `tests/e2e/*`, `tests/similar`, `tests/questionnaire`, BotID list in `instrumentation-client.ts`, helper `lib/utils/bot-protection.ts`.

Env var names that matter (names only, never values): `VAPI_PRIVATE_KEY, VAPI_WEBHOOK_SECRET, VAPI_SQUAD_ID, VAPI_PHONE_NUMBER_ID, VAPI_PUBLIC_KEY, HHT_BACKEND_TOKEN, HHT_WEBSITE_TOKEN, HHT_BUDTENDER_BASE_URL, HHT_DYNAMIC_GREETING, HHT_SQUAD_MODE (single|multi), HHT_TRANSFER_CONSULT, HHT_OWNER_PHONE, HHT_TRANSFER_NUMBER_{YAKIMA,MTVERNON,PULLMAN}, HHT_NON_IDENTIFYING_PHONES, HHT_WEB_PHONE_IDENTITY, HHT_MEMORY_SUMMARIES, HHT_MEMORY_CONSOLIDATE_AT, HHT_MEMORY_WEB_SUMMARIES, HHT_MEMORY_LLM, HHT_MEMORY_LLM_MODEL, HHT_SUGGESTION_WINDOW_DAYS, GEMINI_API_KEY / GOOGLE_API_KEY, GEMINI_USE_VERTEX, GOOGLE_CLOUD_PROJECT, REDIS_URL, POSTGRES_PASSWORD, DJANGO_SECRET_KEY, PHONE_HASH_PEPPER`.

---------------------------------------------------------------------------------------------------------------------

## 9. Definition of done for the whole project

1. Deploy runbook (section 5) completed on the VPS; `vapi_doctor` has no FAIL; the 13 test calls pass or have a ticket each.
2. T1-T7 done, each with tests and the tutorial updated; suites green in all three places (section 3).
3. PRs #1 and #26 reviewed and merged by the owner (not by the agent), production branch updated, services rebuilt.
4. The owner has answered D1-D8 or consciously deferred them.
5. A final report lists, per task, what was verified, what could not be verified, and exact commands the owner can re-run.
