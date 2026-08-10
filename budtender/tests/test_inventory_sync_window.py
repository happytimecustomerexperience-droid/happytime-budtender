"""
Open-hours gate for the FREQUENT inventory sync (`sync_inventory_all`), so it's a
cheap no-op overnight when every store is closed — cutting ~48 of the 144 daily
Dutchie pulls (outside 07:30-23:30 Pacific, every 10 min = 8.5h * 6/hr = 51 pulls,
~35% of the day).

Store hours (America/Los_Angeles), seeded StoreFact rows: yakima 08:00-23:30,
mount-vernon 09:00-22:00, pullman 09:00-22:00. Union + 30-min pre-open warm-up
margin -> effective sync window 07:30-23:30 Pacific.

TIMEZONE CORRECTNESS IS THE POINT: every instant below is expressed in UTC (as
`datetime.now(timezone.utc)` would return on a UTC server) and the expected
skip/sync outcome is derived from what that instant actually is in Pacific time.
A naive `datetime.now()` implementation (server-local, e.g. UTC) would get the
boundary cases and the DST case wrong — see
`test_naive_utc_time_would_wrongly_skip_during_open_hours` for the case that
catches "go quiet during business hours".
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from budtender import tasks


class AnyStoreOpenOrWarmingTests(SimpleTestCase):
    """Direct tests of the helper — timezone math only, no DB."""

    # ── standard time (PST, UTC-8) — Jan 15, 2026 ──────────────────────────────
    def test_pst_just_before_window_opens_is_closed(self):
        # Pacific 07:29 == UTC 15:29 (PST = UTC-8)
        now = datetime(2026, 1, 15, 15, 29, tzinfo=timezone.utc)
        self.assertFalse(tasks.any_store_open_or_warming(now))

    def test_pst_just_after_window_opens_is_open(self):
        # Pacific 07:31 == UTC 15:31
        now = datetime(2026, 1, 15, 15, 31, tzinfo=timezone.utc)
        self.assertTrue(tasks.any_store_open_or_warming(now))

    def test_pst_mid_afternoon_is_open(self):
        # Pacific 14:00 == UTC 22:00
        now = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
        self.assertTrue(tasks.any_store_open_or_warming(now))

    def test_pst_just_after_last_close_is_closed(self):
        # Pacific 23:31 == UTC 07:31 the NEXT day
        now = datetime(2026, 1, 16, 7, 31, tzinfo=timezone.utc)
        self.assertFalse(tasks.any_store_open_or_warming(now))

    def test_pst_deep_overnight_is_closed(self):
        # Pacific 03:00 == UTC 11:00
        now = datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)
        self.assertFalse(tasks.any_store_open_or_warming(now))

    def test_naive_utc_time_would_wrongly_skip_during_open_hours(self):
        """UTC 05:00 is Pacific 21:00 the PRIOR day (PST = UTC-8) — well inside
        the 07:30-23:30 window, so the real store is open and this must sync.
        A buggy implementation that read `datetime.now()` naively on a UTC
        server would treat this instant as local 05:00, which falls OUTSIDE
        07:30-23:30 -> wrongly skip during business hours. This is the failure
        mode the task explicitly calls out as worse than syncing all night."""
        now = datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)
        self.assertTrue(tasks.any_store_open_or_warming(now))

    # ── DST (PDT, UTC-7) — Jul 15, 2026 ─────────────────────────────────────────
    def test_pdt_just_before_window_opens_is_closed(self):
        # Pacific 07:29 == UTC 14:29 (PDT = UTC-7)
        now = datetime(2026, 7, 15, 14, 29, tzinfo=timezone.utc)
        self.assertFalse(tasks.any_store_open_or_warming(now))

    def test_pdt_just_after_window_opens_is_open(self):
        # Pacific 07:31 == UTC 14:31
        now = datetime(2026, 7, 15, 14, 31, tzinfo=timezone.utc)
        self.assertTrue(tasks.any_store_open_or_warming(now))

    def test_pdt_just_after_last_close_is_closed(self):
        # Pacific 23:31 == UTC 06:31 the NEXT day
        now = datetime(2026, 7, 16, 6, 31, tzinfo=timezone.utc)
        self.assertFalse(tasks.any_store_open_or_warming(now))

    # ── override setting ─────────────────────────────────────────────────────
    def test_override_setting_narrows_the_window(self):
        # Pacific 14:00 is normally well inside the default window (sync)...
        now = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)  # Pacific 14:00 PST
        self.assertTrue(tasks.any_store_open_or_warming(now))
        # ...but with a narrowed override window that excludes 14:00, it must skip.
        with override_settings(STORE_SYNC_WINDOW_START="09:00", STORE_SYNC_WINDOW_END="11:00"):
            self.assertFalse(tasks.any_store_open_or_warming(now))

    def test_override_setting_widens_the_window(self):
        # Pacific 02:00 is normally outside the default window (skip)...
        now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)  # Pacific 02:00 PST
        self.assertFalse(tasks.any_store_open_or_warming(now))
        # ...but with a widened override that includes 02:00, it must sync.
        with override_settings(STORE_SYNC_WINDOW_START="00:00", STORE_SYNC_WINDOW_END="23:59"):
            self.assertTrue(tasks.any_store_open_or_warming(now))


class SyncInventoryAllGateTests(TestCase):
    """Wiring: `sync_inventory_all` must consult the gate and skip/sync accordingly."""

    def test_skips_as_a_clear_structured_noop_when_all_stores_closed(self):
        with mock.patch("budtender.tasks.any_store_open_or_warming", return_value=False), \
             mock.patch("budtender.tasks.sync_inventory") as m_sync, \
             mock.patch("budtender.tasks.classify_products_all") as m_classify:
            result = tasks.sync_inventory_all()
        self.assertEqual(result, {"skipped": "stores_closed"})
        m_sync.assert_not_called()
        m_classify.assert_not_called()

    def test_syncs_every_store_when_any_store_is_open(self):
        with mock.patch("budtender.tasks.any_store_open_or_warming", return_value=True), \
             mock.patch("budtender.tasks.sync_inventory", side_effect=lambda s: 1) as m_sync, \
             mock.patch("budtender.tasks.classify_products_all") as m_classify:
            result = tasks.sync_inventory_all()
        self.assertEqual(result, {s: 1 for s in tasks.STORE_SLUGS})
        self.assertEqual(m_sync.call_count, len(tasks.STORE_SLUGS))
        m_classify.assert_called_once()

    def test_ensure_inventory_fresh_is_not_gated_by_store_hours(self):
        """The ≥24h safety net must still be able to rescue stale inventory at any
        hour — it is already a no-op when data is fresh, so it needs no gate."""
        with mock.patch("budtender.tasks.any_store_open_or_warming", return_value=False), \
             mock.patch("budtender.tasks.inventory_is_stale", return_value=True), \
             mock.patch("budtender.tasks.sync_inventory", side_effect=lambda s: 1) as m_sync, \
             mock.patch("budtender.tasks.classify_products_all") as m_classify:
            result = tasks.ensure_inventory_fresh()
        self.assertEqual(result, {s: 1 for s in tasks.STORE_SLUGS})
        self.assertEqual(m_sync.call_count, len(tasks.STORE_SLUGS))
        m_classify.assert_called_once()
