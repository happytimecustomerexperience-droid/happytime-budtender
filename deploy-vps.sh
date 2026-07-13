#!/usr/bin/env bash
# Happy Time — one-shot VPS bootstrap for the budtender + voice stack.
# Paste on a fresh Hostinger VPS (Ubuntu). Idempotent: safe to re-run.
#   bash deploy-vps.sh
# Secrets live in .env files that are gitignored — this script NEVER contains them.
set -euo pipefail

REPO="https://github.com/happytimecustomerexperience-droid/happytime-budtender.git"
BRANCH="feat/pos-roles-queue"
DIR="$HOME/happytime-budtender"

echo "==> 1/5 Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
  echo "   Docker installed — if this is the first run, log out/in (or 'newgrp docker') then re-run."
fi
docker compose version >/dev/null

echo "==> 2/5 Code ($BRANCH)"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch origin "$BRANCH" && git -C "$DIR" checkout "$BRANCH" && git -C "$DIR" pull --ff-only origin "$BRANCH"
else
  git clone "$REPO" "$DIR" && git -C "$DIR" checkout "$BRANCH"
fi
cd "$DIR"

echo "==> 3/5 Env check"
missing=0
for f in .env .env.dutchie voice/.env; do
  [ -f "$f" ] || { echo "   MISSING: $f"; missing=1; }
done
if [ "$missing" = "1" ]; then
  cat <<'MSG'

   Provide the 3 gitignored env files, then re-run this script. Easiest — from your
   LOCAL machine (Git Bash / PowerShell), copy your working files up:

     scp .env          root@<VPS_IP>:~/happytime-budtender/.env
     scp .env.dutchie  root@<VPS_IP>:~/happytime-budtender/.env.dutchie
     scp voice/.env    root@<VPS_IP>:~/happytime-budtender/voice/.env

   Then on the VPS set the DEPLOY-ONLY values in .env:
     TUNNEL_TOKEN=<from Cloudflare>          # public ingress (see DEPLOY.md §6)
     POS_HOST=checkout.happytimeweed.com     # Traefik host for the POS
   And CONFIRM these chat vars are present (added this session):
     .env       -> HHT_VOICE_BASE_URL=http://voice-web:8000
     voice/.env -> SEMANTIC_SEARCH_ENABLED=0        # deterministic FAQ, no LLM/quota
     both       -> HHT_BACKEND_TOKEN=<same long token>   # must match each other + the website
MSG
  exit 1
fi

echo "==> 4/5 Build + up (migrate + KB self-seed run on boot)"
docker compose up -d --build
docker compose ps

echo "==> 5/5 First data load (inventory -> products; needs .env.dutchie keys)"
docker compose exec -T web python manage.py sync_inventory || echo "   (skipped/failed — check Dutchie keys)"

cat <<'DONE'

Done. Next:
  - Admin login:  docker compose exec web python manage.py createsuperuser
  - Verify chat:  docker compose exec web python manage.py check_gemini   # canned-fallback ok; grounded via voice
  - Logs:         docker compose logs -f web voice-web
The website must point HHT_BACKEND_URL at this stack (via the tunnel) with the SAME HHT_BACKEND_TOKEN.
DONE
