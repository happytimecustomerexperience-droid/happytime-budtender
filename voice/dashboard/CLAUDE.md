# dashboard/ — staff console (Django app)

## Purpose
The owner's browser console for the voice stack: agents, flow, KB, specials/hours, policies, vendor
allowlist, calls, publish-to-Vapi. Every view is staff-only (`@staff_member_required`).

## Bulk tools (`bulk.py` logic, `bulk_views.py` HTTP)
One registry (`bulk.DATASETS`) drives: CSV template/export/upload, inline row editing, "Edit all",
and row actions. Validation ALWAYS goes through the dataset's existing ModelForm (`forms.py`), so a
bulk path cannot accept what the single-row editor refuses.
- Add a dataset: a `_register(Dataset(...))` in `bulk.py` (key fields = the natural key, 2 *inactive*
  example rows), put `{% include "dashboard/_bulk_toolbar.html" %}` + a `<div id="data-list">` on its
  list page, add the page's context via `bulk_views.list_context`.
- Upload is two-step: preview writes nothing and returns a signed token holding the decoded file;
  Apply re-parses and re-validates from scratch and compares a plan digest (stale preview = no write).
- `bulk.apply_plan` is the only batch writer: one transaction, per-row signals held
  (`kb.signals.bulk(publish=False, nudges=())`), then `bulk.run_side_effects` ONCE. Edit-all and row
  actions are all-or-nothing; CSV Apply commits the valid rows (or nothing with "stop if any error").
- Formula safety: export prefixes `' ` on cells starting `= + - @ TAB CR`; import strips that one
  apostrophe and rejects unmarked `=`/`@`-leading (and non-numeric `+`/`-`) cells.
- Never log file contents; `BulkBatchLog` keeps counts + username only.

## Analytics page (`analytics_views.py`, `analytics_calls.py`)
One page, one GET filter row (days 7/30/90/custom N, store, channel). Budtender sections (suggestions,
chat funnel, zero-result searches) call `views._budtender_post` in parallel and render an "analytics service
unreachable" note on failure; an authoritative empty says "no suggestions" instead. Calls come from `VoiceCall`
in six grouped queries (TIME_ZONE buckets). Charts are server-built inline SVG (`stacked_columns`, `heatmap`).
- Conversion = bought / (bought + not bought); pending and unattributable are shown, never folded in.
- Customer links need a dashboard `voice_id`; the budtender customer id is another key space, so no link is built.
- Chosen from a short operator-KPI read (Cova, Korona, Indica Online, 2026-10): conversion of recommendations
  by channel/store/rank, products pushed vs never bought, hour x weekday call load (peak hours drive staffing),
  transfer outcomes, search misses. Left out because the data cannot back them: per-budtender sales, basket
  size, retention, revenue/margin per suggestion, period-over-period deltas, FAQ gaps and deals asked.
- Gotcha: the funnel API has no channel filter, so the chat funnel is not narrowed by channel.
- Tests: `DJANGO_DEBUG=1 uv run pytest -q -p no:cacheprovider dashboard/tests/test_analytics_page.py`

## Scripts & commands
    cd voice
    DJANGO_DEBUG=1 uv run pytest -q -p no:cacheprovider dashboard/tests/test_bulk.py
    DJANGO_DEBUG=1 uv run python manage.py makemigrations --check --dry-run
    uv run ruff check .
    # regenerate the shipped sheet after changing a dataset's columns/examples:
    DJANGO_DEBUG=1 uv run python manage.py shell -c "from dashboard import bulk; open('docs/templates/specials-template.csv','wb').write(bulk.get_dataset('specials').template_csv())"

## Invariants
- Old standalone edit URLs (`dash-kb-row-*`, allowlist, policy-category) keep working for no-JS users
  and return to the page they came from via `views._back` / `bulk_views.same_site_path`
  (same-site `/dashboard/` paths only; `//host` and `https://host` fall back to the list page).
- Inline endpoints answer HTMX with a `<tr>` partial (200, no redirect) and others with a redirect.
- Templates: `{# #}` comments are single-line only; use `{% comment %}` for more.

## Gotchas
- Rows labelled `Dutchie #...` are owned by `kb/deals_sync.py` and are overwritten by the sync.
- `StoreFact` datasets nudge budtender "store-facts" once per batch; other KB datasets send nothing
  (retrieval is content-hash cached; the Vapi files mirror is the KB "Reindex" button).

## Related
`kb/signals.py` (per-row nudges, `bulk()`), `kb/models.py`, `docs/VAPI-SETUP-AND-TESTING.md` section 8.6.
