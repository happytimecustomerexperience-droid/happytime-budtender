# Happy Time Budtender — VPS Deploy Runbook

Self-contained Django + Postgres + Redis + Celery + Cloudflare Tunnel stack that
powers the website's AI budtender (suggestions, profiles, analytics, admin).
No inbound ports are opened — the only way in is the Cloudflare Tunnel.

> **This repo now hosts TWO services.** Sections 0–7 below cover the **budtender**
> backend. The **voice agent** (Vapi Squad) lives in [`voice/`](voice/) and runs
> as independent containers from this same repo + tunnel — see
> [**Voice agent**](#voice-agent-second-service--voice) at the bottom. To bring
> up **both** in one shot: `docker compose up -d --build`.

> Sized for a **2 vCPU / 4 GB VPS** (~5 gunicorn workers). Bump `GUNICORN_WORKERS`
> = `2 × vCPU + 1` for bigger boxes.

---

## 0. One-time: install Docker on the VPS
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER       # log out/in after this
docker compose version              # confirm the compose plugin is present
```

## 1. Get the code
```bash
git clone git@github.com:<you>/happytime-budtender.git
cd happytime-budtender
```

## 2. Configure secrets (never committed)
```bash
cp .env.example .env
# Edit .env and set, at minimum:
#   SECRET_KEY            50+ random chars   (python -c "import secrets;print(secrets.token_urlsafe(50))")
#   DEBUG=False
#   ALLOWED_HOSTS         budtender-api.happytimeweed.com,localhost
#   CSRF_TRUSTED_ORIGINS  https://happytimeweed.com
#   HHT_BACKEND_TOKEN     long random token  (MUST match the website's HHT_BACKEND_TOKEN)
#   SQL_PASSWORD          strong db password
#   TUNNEL_TOKEN          from Cloudflare (step 6)
nano .env

# Dutchie per-store POS keys (read-only; copy from marketing_dashboard Tenant.config):
cat > .env.dutchie <<'EOF'
DUTCHIE_YAKIMA_POS_KEY=...
DUTCHIE_MTVERNON_POS_KEY=...
DUTCHIE_PULLMAN_POS_KEY=...
EOF
```
`.env` and `.env.dutchie` are gitignored — they never leave the VPS.

## 3. Build + start the database, then the app
```bash
docker compose build
docker compose up -d db redis            # bring up datastores first
# The `migrate` service auto-runs makemigrations + migrate on a FRESH DB,
# building the entire schema from the models. Then start everything:
docker compose up -d
docker compose ps                        # web/celery-worker/celery-beat/db/redis/cloudflared up
```

## 4. Create the admin login (for the secure dashboard)
```bash
docker compose exec web python manage.py createsuperuser
```

## 5. First data load (inventory → classify, transactions → profiles)
```bash
docker compose exec web python manage.py shell -c "from budtender.tasks import sync_inventory_all, sync_transactions_all, build_copurchase_all; print(sync_inventory_all()); print(sync_transactions_all()); print(build_copurchase_all())"
```
- `sync_inventory_all` pulls per-store inventory **and** runs classification (Core/Traffic/Profit buckets).
- `sync_transactions_all` builds customer profiles (affinity, quality tier, novelty) from `/reporting/transactions?includeDetail=true`.
- `build_copurchase_all` builds the "bought together" matrix in Redis.
After this, **Celery Beat keeps them fresh**: inventory every 10 min, transactions every 6 h, co-purchase daily (see `core/celery.py`).

## 6. Cloudflare Tunnel (the only way in — no open ports)
1. In Cloudflare Zero Trust → Networks → Tunnels → **Create a tunnel** (Cloudflared).
2. Copy the tunnel token → put it in `.env` as `TUNNEL_TOKEN=...`.
3. Add a **Public Hostname**: `budtender-api.happytimeweed.com` → Service `http://web:8000`.
4. `docker compose up -d cloudflared` (or `docker compose restart cloudflared`).
5. Verify: `curl https://budtender-api.happytimeweed.com/api/v1/health/` → `{"status":"ok"}`.

### Lock the admin behind Cloudflare Access (SSO)
In Zero Trust → Access → Applications, add a **self-hosted app** for
`budtender-api.happytimeweed.com/admin*` (and `/django-admin*` if used) with a
policy allowing only your team's emails. The API paths (`/api/v1/*`) stay
Bearer-gated for the website; the admin is humans-only via SSO.

## 7. Point the website at the VPS
On the website host (Vercel/server env — **server-only, never `NEXT_PUBLIC_`**):
```
HHT_BACKEND_URL=https://budtender-api.happytimeweed.com
HHT_BACKEND_TOKEN=<the same token as the VPS .env>
```
Redeploy the website. The browser still only ever calls same-origin `/api/*`;
the website server proxies to the VPS over the tunnel with the Bearer token.

---

## Concurrency & "nothing throttled or fails"
- **No IP throttle on the API.** The VPS is called only by the website's server
  (one shared IP), Bearer-gated — `core/settings.py` has no DRF throttle, so end
  users are never collectively throttled. (Verified: 140 rapid calls → 0×429.)
- **Gunicorn**: `--workers ${GUNICORN_WORKERS:-5} --worker-class gthread --threads ${GUNICORN_THREADS:-4} --timeout 60` — threads cover I/O waits (Postgres, Dutchie) so hundreds of concurrent chat users are served without blocking.
- **Persistent DB connections**: `CONN_MAX_AGE=60` avoids per-request connect cost under bursts. Postgres default `max_connections=100` is ample for 5×4 threads + Celery.
- **Per-user rate limiting** stays on the **website** (`lib/utils/rate-limit.ts`), keyed by each visitor's IP — distinct users never share a bucket.
- **External ceiling**: the LLM turn endpoint runs on the website and calls Vertex AI; ensure your Vertex quota matches peak concurrency. The chat already retries transient 429s 3× with backoff.
- **Future lever** (only if a single box saturates): cache the candidate set per `(location, category)` in Redis in `budtender/ranking.py`, or run 2+ `web` replicas behind the tunnel.

## Day-2 operations
```bash
docker compose logs -f web                 # tail app logs
docker compose ps                          # health
docker compose pull && docker compose up -d --build   # deploy an update
docker compose exec web python manage.py shell -c "from budtender.tasks import sync_inventory_all; sync_inventory_all()"  # manual resync
docker compose exec db pg_dump -U budtender budtender > backup-$(date +%F).sql        # backup
```

## Health & security checklist
- `GET /api/v1/health/` returns `{"status":"ok"}` through the tunnel.
- No inbound ports open (`docker compose port web 8000` is internal only; the
  override that exposes `:8000` locally must NOT be used in prod).
- `git ls-files | grep -E '\.env$|\.env\.dutchie'` is **empty** (no secrets pushed).
- `/admin` only reachable behind Cloudflare Access; `/api/v1/*` only with the Bearer token.
- No `margin`/`cost`/`bucket` in any public API response (allowlist serializer + the menu/chat never receive them).
- Phone numbers are hashed in analytics; raw phone is never logged.

---

# Voice agent (second service — `./voice`)

`happytime-voice` now lives in this repo at [`voice/`](voice/) and runs as an
**independent** set of containers (its own Postgres) from the **same** repo,
behind the **same** Cloudflare tunnel. The two services talk in-cluster:
voice → budtender at `http://budtender.internal:8000` (a docker network alias of
budtender's `web` container) with the shared `HHT_BACKEND_TOKEN`. That alias is
already in budtender's `ALLOWED_HOSTS`, so calls aren't rejected under `DEBUG=0`.
(Voice's flow — suggestions / inventory / pairing / returning-caller / facets —
maps to budtender's existing `/api/v1/*` endpoints; no budtender code change is
needed to run them together. One non-blocking gap: the P4 dashboard
"ranking-weights" admin endpoint isn't in budtender yet — voice degrades
gracefully to budtender's default ranking until it's added.)

## Activate EVERYTHING — one command
After `git pull` on the VPS:
```bash
docker compose up -d --build
```
That builds + starts budtender (db, redis, migrate, web, celery-worker,
celery-beat) **and** voice (voice-db, voice-web) plus the shared `cloudflared`.
Optional voice post-call queue (Gemini summary + staff email off the webhook turn):
```bash
docker compose --profile voice-async up -d --build   # adds voice-redis + voice-worker
```
Check: `docker compose ps` — everything `running` / `healthy`.

> **voice-web is fail-closed.** With `DJANGO_DEBUG=0` (forced by compose) it
> refuses to boot until the required secrets in `voice/.env` are real. Until you
> fill them it will crash-loop — that's intentional, not a bug.

## What voice needs — fill `voice/.env`
```bash
cp voice/.env.example voice/.env
nano voice/.env
```
**Required** before it will start (the DEBUG=0 fail-closed guard):

| Var | What | Where to get it |
|---|---|---|
| `DJANGO_SECRET_KEY` | 50+ random chars, **≠** pepper | `python -c "import secrets;print(secrets.token_urlsafe(50))"` |
| `PHONE_HASH_PEPPER` | PII salt; **MUST differ** from secret | another random string |
| `POSTGRES_PASSWORD` | voice DB password (voice-db is created with it) | choose a strong one |
| `HHT_BACKEND_TOKEN` | **must equal budtender's** root `.env` token | copy it from the root `.env` |
| `VAPI_PRIVATE_KEY` | Vapi server key | Vapi dashboard → API Keys (below) |
| `VAPI_WEBHOOK_SECRET` | HMAC secret you choose (also written onto Vapi tools) | any long random string |

**Recommended** for a genuinely useful agent (not required to boot):
- **LLM for post-call summaries** — either Vertex (`GOOGLE_CLOUD_PROJECT` + a
  service-account JSON dropped in `voice/secrets/` and
  `GOOGLE_APPLICATION_CREDENTIALS=/app/secrets/<file>.json`, mounted read-only),
  **or** the cheaper key path `GEMINI_API_KEY` (AI Studio).
- **Staff alert emails** — `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` (e.g. a Gmail
  app password) + `STAFF_ALERT_EMAIL`.
- **Inbound number + transfers** — `VAPI_PHONE_NUMBER_ID` and the
  `HHT_TRANSFER_NUMBER_*` store lines (pre-filled — confirm them).
- `PUBLIC_BASE_URL` / `DJANGO_ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` — already
  defaulted to `voice.happytimeweed.com`.

> **Dutchie keys do NOT go in `voice/.env`.** They live only in the budtender
> root `.env.dutchie`. Voice gets all product/stock data by calling budtender.

## Cloudflare — add the 2nd public hostname (same tunnel)
In Cloudflare Zero Trust → Networks → Tunnels → *your existing tunnel* →
Public Hostname → **Add**:
- Host: `voice.happytimeweed.com`
- Service: `http://voice-web:8000`

budtender's `budtender-api.happytimeweed.com → http://web:8000` stays as-is.
No new tunnel, no token change, **no open ports**. Verify once up:
```bash
curl https://voice.happytimeweed.com/healthz      # -> ok
```

## One-time voice setup (after the stack is up)
```bash
docker compose exec voice-web python manage.py seed_kb                  # FAQ/returns/store-facts/WA limits
docker compose exec voice-web python manage.py provision_vapi --dry-run # preview payloads (auto when no key)
docker compose exec voice-web python manage.py provision_vapi           # create/PATCH Squad+assistants+tools (idempotent)
docker compose exec voice-web python manage.py createsuperuser          # dashboard login at /dashboard/
```
Lock `/dashboard*` behind Cloudflare Access (self-hosted app for
`voice.happytimeweed.com/dashboard*`), same as budtender's `/admin`.

## Connect Vapi — the free path
1. Sign up at **dashboard.vapi.ai** — new accounts get **$10 free credit**.
2. **API key:** dashboard → **API Keys** → copy the **Private** key → `VAPI_PRIVATE_KEY`.
3. **Free phone number:** Phone Numbers → **Free Vapi Number** tab → pick a **US**
   area code (free; up to 10 per account). Copy its id → `VAPI_PHONE_NUMBER_ID`.
4. **Webhook secret:** put any long random string in `VAPI_WEBHOOK_SECRET`;
   `provision_vapi` writes it onto each tool's `server.secret` and the Django
   webhook verifies it (constant-time, fail-closed).
5. `provision_vapi` points every tool's `server.url` at `PUBLIC_BASE_URL`
   (`https://voice.happytimeweed.com/api/voice/...`) and attaches the Squad to
   your number. Call the number → it reaches voice-web through the tunnel.

**What "free" really means:** the **number is free**, but minutes burn the $10 —
Vapi's platform fee (~$0.05/min) plus the bundled STT+LLM+TTS+telephony nets
~$0.15–0.40/min all-in. There is no perpetual free plan; add a card before the
credit runs out. (Vertex/Gemini and SMTP are billed separately by Google / your
mail provider — independent of Vapi.)

## Do you need n8n (or the n8n MCP)? No.
Vapi calls this Django backend's HTTPS webhooks/tools **directly**, and the repo
provisions Vapi itself via the Vapi REST API (`provision_vapi`). Nothing in the
Vapi ↔ voice ↔ budtender path needs n8n, and there is no official Vapi n8n node.
Keep n8n only as **optional** no-code glue if you later want a non-developer to
wire post-call automations (transcript → Sheets/CRM, scheduled outbound) without
touching code — and the repo already does staff email + Slack natively. So: don't
use the n8n MCP to set this up.

## Resource sizing (both stacks on one box)
Two web apps + 2 Postgres + Redis + Celery is heavier than budtender alone.
- **8 GB VPS recommended.** On a 4 GB box, keep it lean: set `GUNICORN_WORKERS=3`
  (budtender, root `.env`) and `VOICE_GUNICORN_WORKERS=2`, and leave the
  `voice-async` profile **off** (post-call work then runs inline — fine).
- Tunable knobs (root `.env` or shell env): `GUNICORN_WORKERS`,
  `GUNICORN_THREADS`, `VOICE_GUNICORN_WORKERS`, `VOICE_CELERY_CONCURRENCY`.

## Local dev note
The merged root `docker-compose.yml` is the **VPS / prod** topology (tunnel
ingress, DEBUG=0). For local hacking on voice alone, use its standalone compose:
`cd voice && docker compose up` (that one publishes a port + uses dev secrets).
