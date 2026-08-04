# Bundle campaign troubleshooting

Symptoms, in rough order of how often they happen. Most are config, not code — check
here before redeploying.

## The link shows "This link didn't open" (HTTP 400)

The signature didn't verify. In order of likelihood:

1. **The two secrets differ.** `BUNDLE_URL_SECRET` in `alpine-automations/.env` does
   not match the one on the VPS. Run `scripts/preflight.py`. Fix by copying the
   deployed value into alpine — changing the deployed one restarts the container and
   breaks any links already in flight.

2. **An exported env var masked the stored one.** If you built links in a shell where
   you had `export BUNDLE_URL_SECRET=...`, they were signed with *that*, not with
   `.env`. Rebuild in a clean shell. This is why preflight reads the file directly.

3. **The mail client rewrote the URL.** Some clients shorten or re-encode links.
   Repeated `i=` params are the usual casualty. Test by pasting the raw link from
   `urls.csv` into a browser — if that works and the emailed one doesn't, it's the
   client, and the creative should link plainly rather than through a shortener.

4. **The link expired.** Expiry is *not* a 400 — an expired link still renders and
   says the offer ended. If you're getting 400, expiry isn't your problem.

Diagnose live:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "<the url>"
```

## The page loads but has no styling and buttons do nothing

`collectstatic` didn't run, so WhiteNoise is serving from an empty `STATIC_ROOT` and
every asset 404s.

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  https://budtender-api.happytimeweed.com/static/bundles/bundle.css
```

404 confirms it. The `web` service runs `collectstatic` at boot in
`docker-compose.yml`; if that line is missing or the container didn't restart, redeploy
with the repo's own script and then check the log:

```bash
ssh root@<vps> 'cd ~/happytime-budtender && bash deploy-vps.sh'
```

```bash
ssh root@<vps> 'cd ~/happytime-budtender && docker compose logs web | grep -i "static files copied"'
```

**Deploy the whole stack, not one service.** `web`, `pos-web`, `celery-worker`, `celery-beat`,
`warmer` and `migrate` all build from the same root Dockerfile, and the source is baked into
the image — there is no bind mount for `/app`. `docker compose build web && up -d web` leaves
the other five on the previous image, and nothing surfaces it: every container still reports
healthy. That is how the staff-facing POS ended up a day behind the storefront. `deploy-vps.sh`
does `docker compose up -d --build`, which keeps them in step.

To check for drift:

```bash
ssh root@<vps> 'cd ~/happytime-budtender && for c in $(docker compose ps --format "{{.Name}}"); do docker inspect -f "{{.Name}} {{.Created}}" $c; done'
```

App containers should share one build timestamp. `postgres` and `redis` being weeks old is
correct — those are third-party images.

## The whole path 404s at the edge

Traefik only routes what its rule matches. The budtender router must include
`/custom-order` and `/static/bundles`:

```bash
grep -o "routers.budtender.rule=.*" docker-compose.yml
```

It should read `Host(...) && (PathPrefix(/api) || PathPrefix(/custom-order) ||
PathPrefix(/static/bundles))`. Bare `/static` would also publish the POS's own JS/CSS
on the customer-facing host — keep it scoped.

`/admin/` returning 404 publicly is correct, not a bug.

## Every slot says "Nothing in stock matched this slot"

The page couldn't reach live inventory, or nothing qualifies.

- A banner reading "couldn't reach the live menu" means the register pull failed.
  Check `docker compose logs web | grep -i inventory`. The page degrades rather than
  erroring, which is intended, but the bundle will be empty.
- Otherwise the slot's category or size gate matches nothing on the floor. Confirm the
  category vocabulary: slots use `pos.imagemap.category_key` slugs (`vapes`,
  `concentrate`), not `_norm_category` slugs (`vape-cartridges`, `concentrates`).
- `BUNDLE_MIN_STOCK` defaults to 2, so single remaining units are excluded on purpose.

Check what's actually available:

```bash
python manage.py shell -c "
from pos import catalog
inv = catalog.get_inventory('yakima')
print('rows:', len(inv))
from collections import Counter
print(Counter(p['cat_key'] for p in inv if p['qty'] >= 2))
"
```

## Everything shows as a substitution

The `--items` ids are stale — none are on the floor anymore. The resolver is doing its
job, but the email advertised products that no longer exist, which reads badly. Rebuild
the link set from current inventory.

If an id was never valid (a SKU instead of a `product_id`, say), it will also always
substitute. `--items` takes Dutchie `product_id`.

## The price on the page differs from the email

Expected. The page shows *today's* live register price; the email shows whatever was
true when it was written. The page is authoritative, and the register is authoritative
over the page. Don't hardcode prices into creative copy — say the discount depth, not
the dollar amount.

## The wrong store's products appear

`loc=` in the URL is the location slug (`yakima` / `mount-vernon` / `pullman`), while
`pos.catalog.get_inventory` takes the store key (`yakima` / `mtvernon` / `pullman`).
They differ for Mount Vernon. `bundles/catalog.py:LOCATION_TO_STORE_KEY` maps them —
if you're seeing the wrong inventory, something bypassed that mapping.

## A reservation didn't reach the POS

Reservations are `PhoneCartDraft` rows with status `released`. Staff claim them from
the POS queue panel; nobody is actively notified, so if a shopper walks in immediately
a budtender has to look.

```bash
python manage.py shell -c "
from budtender.models import PhoneCartDraft as D
for d in D.objects.order_by('-created_at')[:5]:
    print(d.draft_token, d.status, d.location_slug, d.pickup_name, d.expires_at)
"
```

Drafts expire after `BUNDLE_DRAFT_TTL_HOURS` (default 4). An expired draft is gone from
the queue — that's the design, so stale holds don't tie up inventory.

## The container won't start after a deploy

If `web` crash-loops with `ImproperlyConfigured: Missing required prod settings`,
`BUNDLE_URL_SECRET` isn't set in the VPS `.env`. This is deliberate — a missing secret
would otherwise 400 for every recipient of a live campaign, so it fails at boot instead.

```bash
ssh root@<vps> 'cd ~/happytime-budtender && grep -c "^BUNDLE_URL_SECRET=." .env'
```

## Someone asks to put items straight into the Dutchie cart

It isn't possible, and it's worth being direct about that rather than trying again.
The cart is `localStorage` on the `dutchie.com` origin; the embed is a cross-origin
iframe; no `dtche[]` parameter touches the cart; and `PersistCheckoutV2` is
abandoned-cart tracking that restores nothing. All three were tested against the live
site — the evidence is in `docs/custom-order-bundles.md`.

The genuine one-tap routes are Dutchie Plus (partner-gated, sunsetting 2026) and the
PreOrder scope on the POS key (`GET /preorder/Status` currently 403s — worth
requesting). Everything else is the reserve-at-counter flow that already ships.
