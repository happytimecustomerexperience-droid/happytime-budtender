"""P7: customer purchase history is cumulative + exactly-once.

The recurring sync must fold a transaction into history at most once (watermark-gated, no
over-count), keep adding new ones, never delete, capture the Dutchie name, and the
rebuild_customer_history backfill must reset + rebuild cleanly. Dutchie is mocked; offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from budtender import tasks
from budtender.models import CustomerProfile, SyncState

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
CUSTOMERS = [{"customerId": "1", "cellPhone": "509-555-1212", "firstName": "Jane", "lastName": "Doe"}]


def _tx(cid, when, pid):
    return {"customerId": cid, "transactionDate": when.isoformat(),
            "items": [{"productId": pid, "quantity": 1, "unitPrice": 10.0}]}


@override_settings(CACHES=_LOCMEM)
@patch("budtender.tasks.classify_products", lambda *a, **k: None)
@patch("budtender.dutchie.get_customers", lambda slug: CUSTOMERS)
class CustomerHistorySyncTests(TestCase):
    def test_incremental_exactly_once_and_name(self):
        tx1 = _tx("1", T0, "P1")
        with patch("budtender.dutchie.get_transactions_detailed", lambda *a, **k: [tx1]):
            tasks.sync_transactions("yakima")
        p = CustomerProfile.objects.get()
        self.assertEqual(p.name, "Jane Doe")          # name captured from Dutchie
        self.assertEqual(p.total_orders, 1)
        self.assertEqual(len(p.purchase_history), 1)

        # Re-run with the SAME transaction → the watermark gates it → NO double count.
        with patch("budtender.dutchie.get_transactions_detailed", lambda *a, **k: [tx1]):
            tasks.sync_transactions("yakima")
        p.refresh_from_db()
        self.assertEqual(p.total_orders, 1)            # still 1, not 2

        # A genuinely NEW (later) transaction folds in and grows the history.
        tx2 = _tx("1", T0 + timedelta(days=3), "P2")
        with patch("budtender.dutchie.get_transactions_detailed", lambda *a, **k: [tx1, tx2]):
            tasks.sync_transactions("yakima")
        p.refresh_from_db()
        self.assertEqual(p.total_orders, 2)
        self.assertEqual(len(p.purchase_history), 2)   # never deletes the old entry
        self.assertIsNotNone(SyncState.objects.get(location_slug="yakima").last_tx_at)

    def test_rebuild_resets_inflated_counts_then_backfills(self):
        # First sync creates the profile, then we simulate prior over-count inflation.
        tx1 = _tx("1", T0, "P1")
        with patch("budtender.dutchie.get_transactions_detailed", lambda *a, **k: [tx1]):
            tasks.sync_transactions("yakima")
        p = CustomerProfile.objects.get()
        p.total_orders = 999
        p.purchase_history[0]["times_bought"] = 999
        p.save()

        # The backfill resets + rebuilds from the full history → correct counts, not 999.
        with patch("budtender.dutchie.get_transactions_detailed", lambda *a, **k: [tx1]):
            call_command("rebuild_customer_history", "--store", "yakima")
        p.refresh_from_db()
        self.assertEqual(p.total_orders, 1)
        self.assertEqual(p.purchase_history[0]["times_bought"], 1)
