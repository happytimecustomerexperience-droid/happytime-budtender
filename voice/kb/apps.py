from django.apps import AppConfig


class KbConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "kb"

    def ready(self):
        from kb import signals  # noqa: F401 — registers the P6 instant-refresh signal handlers
