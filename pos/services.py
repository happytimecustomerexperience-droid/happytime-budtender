"""Inventory cache refresh — pull Dutchie REST inventory/products -> InventoryItem.

ponytail: idempotent per (store, product_id/serial). Run via the refresh_inventory
mgmt command (cron/Task Scheduler) so browse stays fast and Dutchie isn't hammered.
"""

from __future__ import annotations

import logging

from dutchie.pos_read import PosReadClient

from .models import InventoryItem

logger = logging.getLogger(__name__)


def refresh_inventory(store) -> int:
    """Replace the cache for one store from its REST read key. Returns row count."""
    if not store.api_key:
        logger.warning("store %s has no api_key — cannot refresh inventory", store.name)
        return 0
    client = PosReadClient(store.api_key)
    inv = client.inventory()
    catalog = {p.get("productId") or p.get("id"): p for p in client.products()
               if isinstance(p, dict)}

    rows = []
    for it in inv:
        if not isinstance(it, dict):
            continue
        pid = it.get("productId") or it.get("ProductId") or it.get("id")
        cat = catalog.get(pid, {})
        rows.append(InventoryItem(
            store=store.name,
            product_id=pid,
            batch_id=it.get("batchId") or it.get("BatchId"),
            serial_no=str(it.get("packageId") or it.get("serialNumber") or "")[:64],
            name=(it.get("productName") or cat.get("productName") or it.get("product") or "")[:300],
            category=(it.get("category") or cat.get("category") or "")[:120],
            brand=(it.get("brandName") or cat.get("brandName") or "")[:120],
            price=float(it.get("unitPrice") or it.get("price") or cat.get("unitPrice") or 0),
            available=float(it.get("quantityAvailable") or it.get("available") or 0),
            cannabis=bool(it.get("isCannabis", True)),
        ))

    # Atomic swap: delete this store's rows, bulk insert fresh.
    from django.db import transaction
    with transaction.atomic():
        InventoryItem.objects.filter(store=store.name).delete()
        InventoryItem.objects.bulk_create(rows, batch_size=1000)
    logger.info("refreshed %d inventory rows for %s", len(rows), store.name)
    return len(rows)
