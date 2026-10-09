# budtender/ — menu, ranking, customer profiles, lab data (the API behind the site and the phone)

## Purpose
Django app behind `/api/v1/*`: synced Dutchie inventory, the ranker, phone-keyed customer profiles,
batch lab data for product cards, chat persistence. Called by the website's server routes
(`HHT_WEBSITE_TOKEN`) and by the voice service (`HHT_BACKEND_TOKEN`).

## Skills
`live-api-writes` (before any Dutchie write), `diagnose`, `superpowers:test-driven-development`.

## Scripts & commands
Postgres host `db` only resolves inside docker, so run tests on in-memory sqlite:
- `SQL_ENGINE=django.db.backends.sqlite3 SQL_DATABASE=:memory: DEBUG=1 uv run pytest budtender -q -p no:cacheprovider`
- `... uv run python manage.py makemigrations --check --dry-run`
- Weekly merge by hand: `... python manage.py shell -c "from budtender import identity; print(identity.merge_duplicates())"`

## Invariants
- **Identity is a phone, merged only on a Dutchie account id** (`identity.py`). Never a name. A merge
  folds a never-purchased shell into the Dutchie row and leaves its phone as a `merged_into`
  pointer; every phone lookup goes through `identity.profile_for_phone` / `follow`.
- Profiles we create (`source` "voice"/"web") never reach Dutchie. A name is stored only when the row
  has none; Dutchie's name overwrites ours on sync.
- **Greeting data is first name + taste only** (`identity.context`): no phone, no history lines, no
  cost/margin. `first_name()` returns "" rather than guess.
- **Website phone** is identity ONLY through `SessionContextView` (owner decision 2026-10-05,
  `HHT_WEB_PHONE_IDENTITY`, default on): it links the session (`identity_via="web_phone"`) and search
  personalises from that link. A phone in any other website request body is still ignored. The view is
  capped per session and site-wide (number-enumeration guard). `caller-context`/`profile-upsert`
  are backend-token only (`test_website_token.py` pins both lists).
- **Nobody is named unless one real person's number was given.** `identity.profile_for_phone`/`ensure_profile`
  return None for a blank/short/junk number, a store's own line, `HHT_NON_IDENTIFYING_PHONES`, and a "shared"
  row (>= `SHARED_DUTCHIE_IDS` Dutchie customer ids folded into one phone). On the website a typed number is
  unverified, so `context(web=True)` returns a first name only for a row with purchases or a name the visitor
  typed in that same request. `manage.py diagnose_greeting --name X` lists the offending rows (read-only).
  Pinned by `test_anonymous_never_named.py`.
- **Sessions never mix** (`test_session_isolation.py`): an unknown token creates a session only as
  `s-` + 10-62 url-safe chars (`_mintable`; "s-1"/"s-undefined" would be shared by strangers);
  `resume-by-phone` hands back only a `caller_id`-linked session of that same customer (never a
  website session, whose phone was typed; never for a shared row); a typed junk/shared number or
  `{"forget": true}` on `session-context` unlinks the session's `web_phone` link; a backend caller-ID
  phone that names someone other than the session's customer ranks for the caller and writes no
  picks into that session (`_own_session`). `session-context` is also capped per `X-HHT-Client-IP`.
  Analytics `phone_hash` is an HMAC under SECRET_KEY, not a bare sha256.
- **Menu reads are capped per shopper** (`_menu_throttled`, 180/min per `X-HHT-Client-IP`, one bucket for search,
  facets/categories/specify-more and similar; website token only, no header = uncapped, backend token never).
  **Under-21 carries** (`age_gate.py`): a first-person "I'm 19" in any earlier user turn of the session makes a
  later SHOPPING ask in `ChatReplyView` get the fixed decline (`source: guard`); general questions still reach the brain.
- **Conversations are kept forever.** `ChatSession`/`ChatMessage`/`SuggestedProduct`/`AnalyticsEvent`/`Feedback`
  are Postgres rows (named volume `pgdata`, nightly `db-backup` dump). `PersistView` is append-only; no job
  deletes them; `reset_analytics --yes` also needs `HHT_ALLOW_ANALYTICS_RESET=1`; `purge_pii` never touches
  them. Only `prune_site_noise` deletes (web-vitals/scroll beacons older than 90 days). Redis holds only
  disposable state (caps, caches, broker) and runs with AOF. The owner's ranking-weight override is the
  `Setting(key="ranking_weights")` row (audited `ranking_weights.set`); the cache is only a copy of it.
- `TrackView` stores only `analytics.EVENT_WHITELIST` names (contract events + names the site sends today);
  add a new event name there. `analytics/funnel` and `analytics/session` (backend token only) feed the
  voice dashboard's "Chat funnel" pages.
- No cost/margin in any response (`test_no_leak.py`).
- **Every model call goes through `llm.py`, thinking OFF** (owner rule; `test_memory_summaries.py` fails on a
  `generate_content` anywhere else). Customer memory is never read back to the customer (`memory.echoes`
  guards `chat/message`); conversation summaries reach the website brief only with `HHT_MEMORY_WEB_SUMMARIES`.
- Request paths read the DB only (labs, product details); Dutchie is called by Celery tasks, never in
  a request.

## Gotchas
- `rebuild_customer_history` wipes every row's history and refolds; `dutchie_ids` survive it.
- `_normalize_phone` drops anything that is not a US 10/11-digit number; callers must handle `""`.
- A sync only records `dutchie_ids` for customers with a transaction in the pulled window; the
  merge needs them, so run `rebuild_customer_history` once after deploy for the full set.

## Related
`../voice/crm/CLAUDE.md` (caller hash, alerts), `../voice/voice/recognition.py` (caller lookup),
`core/celery.py` (beat schedule), `../docs/lessons.md`.
