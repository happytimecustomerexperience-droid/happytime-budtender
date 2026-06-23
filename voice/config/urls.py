"""Root URL configuration (01-ARCHITECTURE.md §2; 10-P0 §3.1)."""

from django.contrib import admin
from django.urls import include, path

# Brand the admin site chrome.
admin.site.site_header = "Happy Time Voice — Administration"
admin.site.site_title = "Happy Time Voice Admin"
admin.site.index_title = "Knowledge base, agents & call log"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/voice/", include("voice.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("", include("core.urls")),
]
