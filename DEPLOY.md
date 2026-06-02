# Happy Time Budtender — VPS Deploy Runbook

Self-contained Django + Postgres + Redis + Celery + Cloudflare Tunnel stack that
powers the website's AI budtender (suggestions, profiles, analytics, admin).
No inbound ports are opened — the only way in is the Cloudflare Tunnel.

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
