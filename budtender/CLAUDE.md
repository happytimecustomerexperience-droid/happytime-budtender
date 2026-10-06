# budtender/ — menu, ranking, customer profiles, lab data (the API behind the site and the phone)

## Purpose
Django app behind `/api/v1/*`: synced Dutchie inventory, the ranker, phone-keyed customer profiles,
batch lab data for product cards, chat persistence. Called by the website's server routes
(`HHT_WEBSITE_TOKEN`) and by the voice service (`HHT_BACKEND_TOKEN`).

## Skills
`live-api-writes` (before any Dutchie write), `diagnose`, `superpowers:test-driven-development`.

## Scripts & commands
Postgres host `db` only resolves inside docker, so run tests on in-memory sqlite:
- `SQL_ENGINE=django.db.backends.sqlite3 SQL_DATABASE=:memory: DJANGO_DEBUG=1 uv run pytest budtender -q -p no:cacheprovider`
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
- No cost/margin in any response (`test_no_leak.py`).
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
