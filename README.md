# Happy Time AI Budtender — VPS service

Self-contained, Dockerized Django service that powers the website chatbot's
**high-margin, in-stock, city-specific** suggestions + the **pairing/upsell**
("Budtender suggestion") card. It owns its own Postgres and syncs Dutchie
itself. The website talks to it **only server-side** (Cloudflare tunnel +
service token), so nothing sensitive appears in the browser network tab and
**no cost/margin ever leaves the server**.

## What it does
- Syncs Dutchie inventory (incl. cost) per store → ranks **margin-first**, then
  by customer affinity / effect / budget.
- Builds a customer profile from Dutchie purchase history (brand/category/strain
  affinity, repeat purchases) keyed by **phone**.
- Returns ONE pairing per anchor product, preferring items the customer
  **bought 2+ times** or **bought before but not recently**, else a
  complementary high-margin item.
- Persists sessions + suggested products per session AND per customer; resumes
  by phone (anonymous sessions always start fresh).

## Architecture
```
Next.js (server-side /api/* proxy)  ──Bearer token, TLS──►  Cloudflare Tunnel ─► web (gunicorn, DRF /api/v1)
                                                                                   ├─ postgres (own DB)
                                                                                   ├─ redis (cache + Celery)
                                                                                   └─ celery worker + beat ─► Dutchie (read-only)
```

## API (`/api/v1`, Bearer `HHT_BACKEND_TOKEN`; health is open)
| Endpoint | Purpose |
|---|---|
| `GET  /health/` | liveness |
| `POST /chat/session/start` | mint a fresh session token |
| `POST /products/search/` | margin-first ranked picks (no cost/margin in body) |
| `POST /pairing/for-sku` | one "Budtender suggestion" |
| `POST /chat/resume-by-phone` | resume prior session + generic profile hints |
| `POST /chat/persist/` | upsert session + messages, link phone |
| `POST /customer/profile-upsert` | (re)compute affinity for a phone |

## Deploy (VPS)
1. Install Docker + Docker Compose.
2. `cp .env.example .env` and fill in:
   - `SECRET_KEY`, `HHT_BACKEND_TOKEN` (must match the website's `HHT_BACKEND_TOKEN`),
   - `SQL_PASSWORD`,
   - Dutchie per-store keys/ids — **copy from marketing_dashboard's `Tenant.config`**
     (`dutchie_loc_id`, `dutchie_lsp_id`, POS API keys, backoffice user pool),
   - `TUNNEL_TOKEN` from your Cloudflare tunnel (route your hostname → `web:8000`).
3. `make build && make up` (the `migrate` service auto-creates migrations + schema).
4. Point the website's `HHT_BACKEND_URL` at the tunnel hostname and set the same
   `HHT_BACKEND_TOKEN`. The site uses this automatically; if it's ever
   unreachable, the site falls back to its local catalog (no outage).
5. Wire the Dutchie inventory query: copy the exact `getPackagesV5` query +
   variable mapping from
   `marketing_dashboard/.../apps/automation/ingestion/dutchie_runner.py` into
   `budtender/dutchie.py::fetch_inventory` (left as an integration hook). Same
   for POS `register-transactions` field names in `get_register_transactions`.
6. `make sync` to do a first inventory pull; Celery beat then keeps it fresh
   (inventory every 10 min, transactions + co-purchase nightly).

## Security
- Bearer service token (constant-time compare); browsers never see it.
- Client responses are built from an **allowlist** — cost/margin are never
  referenced. `make test` runs a regression test asserting no `margin`/`cost`
  substring in any client response.
- The LLM (on the website) only emits validated slots; this service exposes only
  fixed, parameterized read queries (no free-form query/exec) — injection-safe,
  view-only.
- Non-root container, no published ports (Cloudflare tunnel only), `.env` gitignored.

## Local dev
```
cp .env.example .env       # set CELERY_EAGER=True for inline tasks if desired
make build && make up
make test
curl -H "Authorization: Bearer <token>" -X POST localhost:8000/api/v1/products/search/ \
  -H 'Content-Type: application/json' -d '{"slots":{"store":"yakima","category":"flower"},"limit":5}'
```

## Getting the Dutchie credentials (one-time)
The credential **values** are NOT in any file in the dashboard repo — they live
in its database (`Tenant.config` JSONField), entered via its onboarding form.
Export them once from the dashboard and paste into THIS service's `.env`:

```bash
# Run inside the marketing_dashboard container/shell:
python manage.py shell -c "from apps.tenants.models import Tenant; import json; t=Tenant.objects.get(slug='happy-time'); print(json.dumps(t.config.get('dutchie'),indent=2)); print(json.dumps(t.config.get('dutchie_users'),indent=2)); print(json.dumps(t.config.get('dutchie_pos_api'),indent=2))"
```
Map them to `.env`:
- `dutchie.dutchie_lsp_id` → each store's `DUTCHIE_*_LSP_ID`
- `dutchie_pos_api.locations.<store>.loc_id` → `DUTCHIE_*_LOC_ID`
- `dutchie_pos_api.locations.<store>.api_key` → `DUTCHIE_*_POS_KEY`
- `dutchie_users` (whole array) → `DUTCHIE_BACKOFFICE_USERS`

## Status / next
- ✅ Service skeleton, models, API, ranking + pairing, sessions/resume, no-leak test, Docker.
- ✅ **Credentials loaded** into `.env` (own secrets) + `.env.dutchie` (3 POS keys
  exported from the dashboard). All 3 keys validated live against `/whoami`
  (Yakima / Mt. Vernon / Pullman → HTTP 200).
- ✅ **Inventory wiring validated on live data**: `fetch_inventory` pulls
  `/reporting/inventory` per store (Yakima = 7,504 sellable in-stock items,
  ~7,454 with `unitCost` → margin computable). Categories normalized to our
  slugs; trade samples dropped. Customers + register-transactions implemented.
- 🔌 First-run check: confirm the buyer link field on `register-transactions`
  (`customerId` vs parent) for purchase-history attribution — one line in
  `tasks.sync_transactions`. Everything else is wired.
- ▶ To go live: `make build && make up` on the VPS, set `TUNNEL_TOKEN`, then point
  the website's `HHT_BACKEND_URL`/`HHT_BACKEND_TOKEN` at it.
