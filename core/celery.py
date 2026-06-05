import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("budtender")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Scheduled syncs (django-celery-beat reads these into the DB scheduler).
app.conf.beat_schedule = {
    "sync-inventory-all-stores": {
        "task": "budtender.tasks.sync_inventory_all",
        "schedule": 600.0,  # every 10 minutes — keeps stock/availability accurate
    },
    # Safety net: even if the frequent sync above stalls, force a fresh pull for
    # any store whose inventory is ≥24h old, so suggestions never come from stale
    # stock. Cheap no-op (timestamp check) when everything is already fresh.
    "ensure-inventory-fresh-daily": {
        "task": "budtender.tasks.ensure_inventory_fresh",
        "schedule": 60 * 60.0,  # hourly check; pulls only when ≥24h stale
    },
    "sync-transactions-nightly": {
        "task": "budtender.tasks.sync_transactions_all",
        "schedule": 6 * 60 * 60.0,  # every 6 hours
    },
    "build-copurchase-nightly": {
        "task": "budtender.tasks.build_copurchase_all",
        "schedule": 24 * 60 * 60.0,  # daily
    },
}
