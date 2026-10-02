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
`bundles/` is in `pyproject.toml` `testpaths`, so a bare `pytest` runs these tests.

## Invariants (each has a test in `tests/test_storefront_hardening.py`, `test_reservations.py` or `test_order_abuse.py`)
- Every shopper reaches Django from Vercel's egress IP, so IP buckets are one bucket for the whole
  site. Per-person limits key on the thing abused — hashed phone/email via `caps.py`, never raw
  values in the cache. Per-IP throttles are site-wide ceilings, not per-person.
- GETs never create a cart row. Only a verified `landing` link or a real `cart_add` does.
- Adding to a cart reserves nothing. A RELEASED order holds stock for `DRAFT_TTL_HOURS`; checkout
  reprices with `confirm=True` and refuses what is gone. Carts never hold stock.
- A bot must not be able to hold a store's shelf. At checkout, under `cart.lock_store`, the holds are
  re-read and the order refused ("changed" = stock gone, "full" = a cap) if online orders held per
  store >= `BUNDLES_MAX_OPEN_ONLINE_ORDERS_PER_STORE` (40), units held + this order >
  `BUNDLES_MAX_HELD_UNITS_PER_STORE` (150), or orders placed site-wide in the last hour >=
  `BUNDLES_MAX_ORDERS_PER_HOUR` (120). All counted from the DB, never the cache. Read with
  `caps.limit()` at call time; no settings.py entry needed.
- The per-email cap keys on `views._email_key` (lowercase, `+tag` dropped, Gmail dots dropped), never the
  typed string; the email is still sent to the typed address. Confirmation mail has a site-wide
  `BUNDLES_MAX_CONFIRMATION_EMAILS_PER_HOUR` (120) ceiling; over it the order stands and no mail goes.
- Dutchie is called at most once per minute per price-check serial and per lab batch (cache).
  The customer lookup (`customers.attach`) stays OUTSIDE the lock; email is sent after the commit.
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
- `test_burst.py` needs real DB concurrency: on in-memory SQLite it skips. `cart.lock_store` is a
  Postgres advisory lock and a no-op on SQLite, and checkout now reads-then-writes inside one
  `atomic()`, so file-backed SQLite also 500s ("database is locked") unless the test DB sets
  `OPTIONS["transaction_mode"] = "IMMEDIATE"`; use that or Postgres. `test_suggest_links` needs store config.
- Dutchie returns no product images; `resolver._public` is the only projection safe to render.

## Related
`budtender/models.py` (PhoneCartDraft), `pos/views.py` (claim), `pos_core/ratelimit.py`,
`dutchie/lab.py`, alpine-automations (signer, separate repo).
