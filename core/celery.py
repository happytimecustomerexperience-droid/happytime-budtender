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
        "schedule": 600.0,  # every 10 minutes
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
