from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dashboard"

    def ready(self):
        # Re-assert dashboard-edited credentials over the .env defaults (P6). Applied ONCE on the
        # first request rather than in ready() itself — querying the DB during app init is
        # discouraged (and the DB may be unmigrated at boot). CLI/management commands read env/.env
        # directly, which is the documented bootstrap source.
        from django.core.signals import request_started

        def _apply_once(sender, **kwargs):
            request_started.disconnect(_apply_once)
            from . import credentials

            credentials.apply_all()

        request_started.connect(_apply_once, weak=False)
