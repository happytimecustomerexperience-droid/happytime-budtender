import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("budtender")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Scheduled syncs (django-celery-beat reads these into the DB scheduler).
app.conf.beat_schedule = {
    "sync-inventory-all-stores": {
        "task": "budtender.tasks.sync_inventory_all",
        # Every 10 minutes, but the task itself is a no-op outside the Pacific
        # store-sync window (STORE_SYNC_WINDOW_START/END, default 07:30-23:30) —
        # see budtender.tasks.any_store_open_or_warming. Keeps stock/availability
        # accurate while every store is open, without pulling Dutchie overnight.
        "schedule": 600.0,
    },
    # Safety net: even if the frequent sync above stalls, force a fresh pull for
    # any store whose inventory is ≥24h old, so suggestions never come from stale
    # stock. Cheap no-op (timestamp check) when everything is already fresh.
    # New Drops (website /new-drops): brands received in the last 20 days, refreshed
    # every 15 min (owner 2026-10-06: new entries must show quickly; a run takes ~3 min and
    # holds the backoffice lock, so runs never overlap); no-op outside the store-hours window.
    "refresh-new-drops": {
        "task": "budtender.tasks.refresh_new_drops_all",
        "schedule": 15 * 60.0,
    },
    # COA follow-up: returns-ops (C:\returns-ops, vercel.json crons) writes lab data + COA links into Dutchie in
    # lab-dispatch at minute 0 of every hour (lab-auto-match at :50 before it). Look again 15 and 45 minutes
    # later and refill the New Drops "View COA" buttons. Same gate/lock as the refresh above.
    "coa-followup": {
        "task": "budtender.tasks.coa_followup_all",
        "schedule": crontab(minute="15,45"),
    },
    # Lab results (terpenes + %) per in-stock batch → BatchLab and the allowlisted product info
    # → ProductDetail, so every chat pick can carry real numbers. Paced, 100 of each per store
    # per run; a no-op outside the same store-hours window and while a New Drops run holds the
    # Dutchie rate budget.
    "warm-batch-labs": {
        "task": "budtender.tasks.warm_batch_labs_all",
        "schedule": 30 * 60.0,
    },
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
    # Re-derive the online-order cap from real basket totals, so it tracks the
    # business instead of staying a number someone guessed once.
    "calibrate-order-caps-weekly": {
        "task": "budtender.tasks.calibrate_order_caps",
        "schedule": 7 * 24 * 60 * 60.0,  # weekly
    },
    # One person on two rows (a caller we created, then a Dutchie guest on another number): merge
    # only on a shared Dutchie account id — budtender.identity.merge_duplicates.
    "merge-duplicate-profiles-weekly": {
        "task": "budtender.tasks.merge_duplicate_profiles",
        "schedule": 7 * 24 * 60 * 60.0,  # weekly
    },
    # Customer memory v1: learn from website chats once they have been quiet >= 10 min (per trust
    # tier: only a carrier-caller-ID/verified session writes the profile) — budtender.memory_learn.
    "learn-idle-chat-sessions": {
        "task": "budtender.tasks.learn_idle_sessions",
        "schedule": 5 * 60.0,
    },
}
