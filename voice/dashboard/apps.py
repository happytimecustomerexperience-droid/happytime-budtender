from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dashboard"

    def ready(self):
        # Keep dashboard-edited credentials (P6) in step with the shared version token: at the start
        # of EVERY request, re-apply the stored rows when the token changed since this web worker
        # last applied (a cheap cache.get otherwise). Not done in ready() itself — querying the DB
        # during app init is discouraged (and the DB may be unmigrated at boot). The Celery worker
        # does the same at task_prerun (core/celery.py). CLI/management commands read env/.env
        # directly, which is the documented bootstrap source.
        from django.core.signals import request_started

        def _refresh(sender, **kwargs):
            from . import credentials

            credentials.refresh_if_stale()

        request_started.connect(_refresh, weak=False, dispatch_uid="dashboard-credentials-refresh")
