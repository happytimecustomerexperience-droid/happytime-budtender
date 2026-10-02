"""Beat-driven KB tasks (the schedule lives in ``core/celery.py``)."""

from __future__ import annotations

from celery import shared_task


@shared_task(name="kb.sync_deals", ignore_result=True)
def sync_deals() -> dict:
    """Every 30 min: mirror each store's current Dutchie deals into the Specials & hours rows.
    A no-op unless the ``auto.deals_sync`` switch is on (see ``kb.deals_sync``)."""
    from kb import deals_sync

    return deals_sync.sync_deals()
