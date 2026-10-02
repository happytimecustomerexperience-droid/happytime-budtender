# bundles/ — the public storefront (`/custom-order`)

## Purpose
Unauthenticated menu, cart and checkout for happytimeweed.com (served through a Vercel
rewrite). The cart and the order are both `budtender.PhoneCartDraft` rows; a budtender claims
the order at the register. Nothing here ever writes to Dutchie. Contract + URL format:
`docs/custom-order-bundles.md`.

## Skills
`bundle-campaign` (building/sending signed links). `live-api-writes` before any Dutchie write.

## Scripts & commands
```
SQL_ENGINE=django.db.backends.sqlite3 SQL_DATABASE=':memory:' HHT_VOICE_BASE_URL='' REDIS_URL='' \
  python -m pytest -q -p no:cacheprovider bundles
python -m ruff check bundles
```
`bundles/` is NOT in `pyproject.toml` `testpaths`, so a bare `pytest` never runs these tests.

## Invariants (each has a test in `tests/test_storefront_hardening.py` or `test_reservations.py`)
- Every shopper reaches Django from Vercel's egress IP, so IP buckets are one bucket for the whole
  site. Per-person limits key on the thing abused — hashed phone/email via `caps.py`, never raw
  values in the cache. Per-IP throttles are site-wide ceilings, not per-person.
- GETs never create a cart row. Only a verified `landing` link or a real `cart_add` does.
- Adding to a cart reserves nothing. A RELEASED order holds stock for `DRAFT_TTL_HOURS`; checkout
  reprices with `confirm=True` and refuses what is gone. Carts never hold stock.
- Dutchie is called at most once per minute per price-check serial and per lab batch (cache).
- A customer name is letters, spaces, `'`, `-`, `.` (40 max) — it is printed in our email.
- `signing.canonical()` is pinned by a golden vector shared with alpine-automations: never change it.
  `signing.parse` rejects any value containing `&` or `=` instead.
- An expired link fills the cart but never claims the bundle. A personalised link (`c`) is honoured
  only when the checkout phone matches it; the link's `c` rides on the OPEN draft's `phone_hash`.
- `lookup-customer` answers found/new + a first initial. Never a name, never an id.
- Prices are tax-inclusive; the quote total is the pre-discount subtotal (register applies the bundle).

## Gotchas
- LocMemCache is process-global: set up with `cache.clear()` or caps/throttles leak between tests.
- `cart.confirm_live_price` returns None under pytest; tests needing it pop `pytest` from
  `sys.modules` (see `past_the_pytest_guard`).
- Names lose digits: a test fixture named `Shopper01` will not round-trip through checkout.
- `test_burst.py` needs real DB concurrency: on in-memory SQLite it 500s on "table is locked"
  (flaky); use a file-backed test DB or Postgres. `test_suggest_links` needs store config.
- Dutchie returns no product images; `resolver._public` is the only projection safe to render.

## Related
`budtender/models.py` (PhoneCartDraft), `pos/views.py` (claim), `pos_core/ratelimit.py`,
`dutchie/lab.py`, alpine-automations (signer, separate repo).
