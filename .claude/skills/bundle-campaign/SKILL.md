---
name: bundle-campaign
description: Set up and ship a Happy Time bundle campaign end-to-end — define or change bundle slots and discounts, pick live Dutchie product ids, build the signed /custom-order links, wire the AlpineIQ creative CTA, and run pre-send verification against the live storefront. Use this skill whenever the user mentions bundles, bundle campaigns, "Roll & Relax", "Vape & Munch", "Weekend Bundle", /custom-order, bundle links, BUNDLE_URL_SECRET, build_bundle_urls, "add bundle to cart", emailing product bundles, or an AlpineIQ campaign that points at a bundle landing page — and also when they ask to change a bundle's discount, swap which products a bundle advertises, or debug a bundle link that shows an error, 400s, renders unstyled, or shows the wrong price.
---

# Shipping a bundle campaign

A bundle campaign is one signed URL per recipient. The email advertises products; the
link opens a storefront that re-resolves every one of those products against live
register inventory, substitutes whatever sold out, and lets the shopper reserve the
bundle for counter pickup.

Two repos are involved and they must agree on a secret:

| Repo | Role |
|---|---|
| `alpine-automations` | **signs** links, builds the AlpineIQ campaign |
| `happytime-budtender` | **verifies** links, serves `/custom-order`, resolves inventory |

Live storefront, two addresses for the same app:

| URL | Use |
|---|---|
| `https://happytimeweed.com/custom-order/` | **put this in a creative** — on-brand, and the page carries the site's header, footer and age gate |
| `https://budtender-api.happytimeweed.com/custom-order/` | the origin behind the Next.js rewrite; what scripts fetch |

The apex is a `rewrites()` entry in the happytimeweed repo's `next.config.ts`. If links ever go
dead, check that entry before anything else.

**Verify the apex in a browser, never with curl.** Vercel answers scripted requests with a bot
checkpoint — HTTP 429 and a "Vercel Security Checkpoint" HTML body — which reads exactly like a
broken page. `preflight.py` targets the origin host for that reason.

The sending half has its own skill in the other repo — `campaign-bundle-drop` in
`alpine-automations/.claude/skills/`, which carries the AlpineIQ draft ids, the audience,
and the CTA wiring. That one is **generated**: edit its entry in
`audiences/management/commands/campaign_skill.py` and re-run
`python manage.py campaign_skill bundle-drop --write`, because hand edits to its SKILL.md
are overwritten. This file is the receiving half — the storefront, the resolver, the
signature — and is hand-maintained.

## Two things that will waste your afternoon if you don't know them

**The Dutchie cart cannot be pre-filled.** Not by API, not by URL parameter, not from
our JavaScript. The cart is `localStorage` on the `dutchie.com` origin; no `dtche[]`
param touches it, and `PersistCheckoutV2` is write-only abandoned-cart tracking (a real
cart persisted under a fresh token restores zero items). This was tested three ways —
see `docs/custom-order-bundles.md` in the budtender repo. So the storefront owns the
cart and hands off at pickup. If someone asks you to "just add the items to their
Dutchie cart," that is the conversation to have.

**The link is a coupon.** It tells a budtender someone gets 30% off, so it is HMAC
signed. Discount depth comes from the bundle slug server-side, never from the URL —
editing `b=roll-relax` to `b=weekend` fails the signature rather than upgrading anyone.

## Before anything else: check the two secrets match

This is the failure that bites. Both repos read `BUNDLE_URL_SECRET`. If they differ,
every link you send 400s — and you won't find out until recipients complain, because
locally-exported env vars mask the stored value.

```bash
python .claude/skills/bundle-campaign/scripts/preflight.py
```

It compares the stored secret in both repos, signs a link with alpine's *stored* value,
and fetches it from the live site. Run it before every send. If it reports a mismatch,
copy the budtender/VPS value into `alpine-automations/.env` — the deployed side wins,
because changing it means a container restart and any links already in flight break.

## Picking product ids

`--items` takes **Dutchie `product_id`**, not SKU, not name. Get them from live
register inventory so you never advertise something already gone:

```bash
python manage.py shell -c "
from pos import catalog
for p in catalog.get_inventory('yakima'):
    if p['cat_key'] == 'flower' and p['qty'] >= 2 and p['subcategory'] == '3.5g':
        print(p['product_id'], p['qty'], p['brand'], p['name'], p['price'])
" | head -20
```

Store keys for `get_inventory` are `yakima` / `mtvernon` / `pullman`. The URL's `loc=`
uses the *location slug* instead — `yakima` / `mount-vernon` / `pullman`. They differ
for Mount Vernon; `bundles/catalog.py` holds the mapping.

You do **not** need to rebuild links when stock moves. Every id is re-resolved at open
time and anything sold out is substituted (same category and size, any brand). Pick
products that represent the offer well; the resolver handles the churn.

## Building the links

From `alpine-automations`:

```bash
python manage.py build_bundle_urls --bundle roll-relax --store yakima \
  --items 3483543:1,3554685:2,3601122:1 \
  --contacts contacts.csv --write urls.csv
```

`contacts.csv` needs a `phone` column (`contact_id` is passed through when present).
Output is `contact_id,phone,url` — upload as an AlpineIQ custom field and point the
creative's CTA at that field. AlpineIQ cannot compute an HMAC, which is why links are
pre-baked per contact rather than assembled from merge tags at send time.

Drop `--contacts/--write` for a single anonymous link (fine for a non-personalised
blast; you lose personalised substitution ranking).

Useful flags: `--phone` prints one link for eyeballing, `--ttl-days` sets lifetime
(default 14 — make it outlive the send window), `--base` overrides the landing host.

## Changing a bundle's slots or discount

Edit `BUNDLES` in `happytime-budtender/bundles/catalog.py`. A `Slot` is a category set
plus an optional size lock:

```python
"roll-relax": Bundle(
    slug="roll-relax", name="Roll & Relax Bundle", discount_pct=20,
    slots=(
        Slot("flower", "3.5g flower", 1, FLOWER, ("3.5g",), strict_size=True),
        Slot("preroll", "1pk pre-roll (regular or infused)", 2, PREROLL),
        Slot("edible", "10pk edible or drink", 1, EDIBLE),
    ),
),
```

`strict_size=True` makes size a hard gate. Use it wherever size carries the price —
flower and pre-rolls especially. Without it a 1g cart can land in a 3.5g slot and the
discount math stops matching what the email promised.

Categories are `pos.imagemap.category_key` slugs (`vapes`, `concentrate`), **not**
`budtender.dutchie._norm_category` slugs (`vape-cartridges`, `concentrates`). The two
vocabularies disagree and a slot written against the wrong one silently never matches.
`canon_category()` bridges them — go through it rather than comparing raw strings.

Changing a discount changes what a budtender honours at the register. Tell the store
before the send, and check the matching Dutchie discount is live if one exists.

After editing, run the tests — the bundle definitions are asserted against:

```bash
python -m pytest bundles/tests -q
```

## Pre-send checklist

Run `scripts/preflight.py`, then confirm by hand:

- Secrets match across both repos, and a stored-secret link returns 200 live.
- Every `--items` id is in stock at ≥ `BUNDLE_MIN_STOCK` (default 2) *today*.
- `loc=` matches the store the email targets. A Pullman customer sent to Yakima is a
  dead end — they cannot buy any of it.
- Open one real link and read it as a customer. Check the discount badge, the store
  name, and that the total matches the offer.
- Confirm nothing else is messaging the same audience that week (AlpineIQ collisions
  make results unattributable and double-send to the overlap).
- If the bundle relies on a Dutchie discount, confirm it is unpaused.

## When something looks wrong

`references/troubleshooting.md` maps each symptom to its cause — 400s, unstyled pages,
404s, wrong prices, empty slots, substitutions that look off. Read it before
re-deploying anything; most of these are config, not code.

## What good looks like

The shopper opens the link on their phone, sees their bundle with today's prices at
their store, swaps the one item they don't want using the same filters the POS menu
has, taps *Reserve at the counter*, and gives their first name in store. A budtender
claims the draft in the POS, the register applies the discount, done.

Nothing in that flow writes a Dutchie order — the register stays the only thing that
can check out. That boundary is deliberate; keep it.
