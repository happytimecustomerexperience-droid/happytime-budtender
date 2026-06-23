# Happy Time Voice

Production voice-agent stack for **Happy Time Weed** (cannabis retail — Yakima / Mt Vernon / Pullman, WA).
A Django control plane that drives a **Vapi Squad** of focused assistants: it answers FAQ/return-policy
questions grounded in a knowledge base, recommends in-stock products via the `happytime-budtender` ranking
engine (margin-first for unknown callers, taste-first for recognized ones), routes vendor calls, escalates
to a human with a warm transfer, and emails staff a durable record of every call — all editable from a
branded dashboard and auto-deployable to Vapi over the REST API.

> Built from the executable plan suite in [`docs/plans/`](docs/plans/) — start with
> [`00-MASTER-ROADMAP.md`](docs/plans/00-MASTER-ROADMAP.md) and [`02-DECISIONS.md`](docs/plans/02-DECISIONS.md).

## Architecture (one Squad, five assistants)

```
Vapi Squad "Happy Time Voice"
 ├─ entry_router   greets as "Koptza", classifies intent
 │    ├─ budtender   slot-fill + suggest_products / check_inventory / pair_upsell  → budtender service
 │    ├─ faq         faq_lookup over the KB (embeddings + keyword fallback)
 │    ├─ vendor      detect → warm transfer → on no-answer collect reason → VendorCallback + staff email
 │    └─ escalation  ≥2 human asks / dispute / defective-return → warm transfer + transcript summary
```

- **Control plane:** Django (`config/`, `core/`, `voice/`, `kb/`, `crm/`, `dashboard/`), forked from the
  `swedish-bot` chassis; `core/services/gemini.py` lifted verbatim (Vertex-preferred).
- **Data plane:** product suggestions call the separate **`happytime-budtender`** microservice over HTTP
  (Bearer); cost/margin can never be spoken (leak-guard + allowlist).
- **KB:** `kb/` models (FAQ / policy / store-facts / weights-types taxonomy / education / blog) with Gemini
  768-dim embeddings + cosine retrieval and a deterministic keyword fallback; mirrored to Vapi Files.
- **Deploy:** Vapi assistants/tools/squad are provisioned **as code** (`provision_vapi`, idempotent,
  zero-drift). Web + webhook served by gunicorn behind Caddy (`docker-compose.prod.yaml`).

## Quickstart (dev)

```bash
uv sync
cp .env.example .env            # fill secrets (see "Owner checklist")
uv run python manage.py migrate
uv run python manage.py seed_kb        # FAQ / returns / store-facts / WA limits / weights-types
uv run python manage.py runserver
# dashboard at http://localhost:8000/dashboard/  (admin-only)
```

Verify:

```bash
uv run ruff check
uv run python manage.py makemigrations --check
uv run pytest -q                # 363 tests, offline / key-free
```

## Provision the Vapi stack

```bash
uv run python manage.py provision_vapi --dry-run   # prints payloads, no API call (auto when key unset)
uv run python manage.py provision_vapi             # create-or-PATCH the Squad + assistants + tools (idempotent)
```

Re-running is a no-op (zero-drift, tracked by `VapiObject.last_provision_hash`). Edit prompts/model/voice
in the dashboard, then **Publish to Vapi** to PATCH the live assistants/squad.

## Owner checklist (fill before going live)

`.env` placeholders the owner supplies — see [`docs/plans/03-CONVENTIONS.md`](docs/plans/03-CONVENTIONS.md) §3:

- `VAPI_PRIVATE_KEY`, `VAPI_WEBHOOK_SECRET`, `VAPI_PHONE_NUMBER_ID`, `PUBLIC_BASE_URL` (HTTPS base Vapi calls back to).
- `HHT_BUDTENDER_BASE_URL` + `HHT_BACKEND_TOKEN` (must match the budtender service).
- `GOOGLE_CLOUD_PROJECT` / `GOOGLE_APPLICATION_CREDENTIALS` (Vertex) or `GEMINI_API_KEY`.
- Transfer numbers (`HHT_TRANSFER_NUMBER_*`) are pre-filled with the published store lines — confirm.
- **Dutchie POS keys live in the `happytime-budtender` service**, not here.
- **Budtender-side TODOs:** the voice repo codes against the contract in
  [`docs/plans/21-SPEC-budtender-contract.md`](docs/plans/21-SPEC-budtender-contract.md); any endpoints
  marked "budtender-side TODO" must be added in that repo before live suggestions work.
- **Brand assets DEFERRED:** real hex/fonts/logo per [`brand/CAPTURE.md`](brand/CAPTURE.md) (blocked by the
  site's Vercel checkpoint — needs a manual browser capture).
- **Mt Vernon hours** are seeded `confirmed=False` (the `/contact` vs `/mount-vernon` pages disagree).

## Status

P0–P5 code-complete and green (ruff + `manage.py check` + 363 offline tests). Not yet exercised against a
live Vapi number / real Dutchie keys / a real outbound transfer — that's the live-smoke step after the
owner checklist is filled.
