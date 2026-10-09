# crm/ — caller identity, staff alerts, the delivery ledger

## Purpose
Peppered phone hash + customer profiles, vendor callbacks, and every outbound staff alert. One
`AlertDelivery` row per `(voice_call, sink)` is the idempotency ledger for all of them.

## Skills
`live-api-writes` (before any new outbound sink), `diagnose` (an alert that "didn't fire").

## Scripts & commands
Run from `voice/` with `HHT_TEST_SQLITE=1 DJANGO_DEBUG=1 ALLOW_NON_EU_RESIDENCY=1`:
- `python -m pytest -q -p no:cacheprovider voice/tests/test_transfer_notice.py voice/tests/test_sinks_email.py`
- `python manage.py makemigrations --check --dry-run`

## Invariants
- **End-of-call alerts**: `sinks.dispatch(voice_call)` — each sink independent, never raises, skipped
  rows for test sessions (`_TEST_SESSION_PREFIXES`) and `HHT_ALERT_SINKS=off`.
- **Transfer heads-up** (`transfer_notice.heads_up`, called from the `forwarding` status-update): the
  composed line is deterministic, ASCII, <= 300 chars; gates in order = `call.sms_on_transfer` switch
  (default OFF) -> store resolved from the DIALLED number -> test-session suppression -> ledger row
  `xfer:<n>` (dedup) -> 3 per call -> `HHT_TRANSFER_NOTICE_DAILY_CAP` per rolling 24 h. A skipped
  notice writes no row, so it never counts against a cap.
- The caller's number is used in-request (last 4 digits, known-customer lookup) and never stored.
  Still true with the dynamic greeting (`voice/caller.py`): a first name + taste summary (no number) live
  in the Django cache for <= 2 h keyed by call id (`caller:<call_id>`); the number goes to budtender only,
  and `remember_caller`'s `first_name` argument is in the tool-call audit row like `caller_name` is.
- Real SMS is unavailable to a cannabis retailer. "Text staff" = Pushover push + Slack + email.
- A new outbound channel obeys its own `alerts.*` switch and has a mocked-`urlopen` test. The
  `alerts.*` switches read ON when the switchboard is unreadable (an outage must not hide an alert);
  an explicit OFF row still wins.
- **Website-chat alerts** (`s-…` ids) are capped per visitor IP first (`HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR`,
  default 2; the IP comes from the website's `X-HHT-Client-IP` via `sinks.visitor_ip`, set by
  `voice.api.text_chat`), then per store as a backstop (`HHT_TEXT_ALERT_CAP_PER_STORE_HOUR`, default 20;
  `config/settings.py` may still set it from `.env`). The first alert the backstop holds in a store-hour
  sends ONE roll-up email. A phone call is never capped.
- Visitor text reaches staff through `sinks.defang` (email, Slack, the transfer note). The n8n webhook
  goes through `sinks.post_webhook` only: public addresses, https, no redirects.

- **`CustomerProfile` list columns** (`items`, `top_category`, `brands_text`, `first_order_date`,
  `last_order_date`) back the Customers table's sort/filter/CSV (`dashboard/customers_views.py`).
  `save()` re-derives all but `items` from the JSON/string fields (also on `update_fields` saves);
  `items` comes only from the import's `TotalUnits` and stays NULL (shown as a dash) when unknown.
  `queryset.update()` / `bulk_update()` skip `save()`, so they must set these columns themselves
  (migration 0006's backfill does). The export has no per-order history: spend in a date range
  needs a per-customer `[{date,total,units}]` (or monthly buckets) added to `customers.json`.

## Gotchas
- `AlertDelivery.sink` is `max_length=24`: `xfer:` + a count, or + the timestamp digits (<= 19).
- The `forwarding` payload shape (`destination.number`, `customer.number`, `summary`, `messages`) is
  taken from the brief, not from a live capture; check the first real transfer.
- Delivery is inline in the webhook (5 s timeout per channel; the known-customer lookup adds up to
  ~10 s when budtender hangs). Move it onto Celery if that ever shows in call latency.

## Related
`voice/voice/webhooks.py` (`handle_status_update`), `voice/voice/caller.py` (caller context + greeting), `voice/voice/capabilities.py`,
`voice/dashboard/credentials.py` (Pushover keys), `voice/voice/provision.py::_transfer_tool`.
