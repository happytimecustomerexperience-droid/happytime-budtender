from django.urls import include, path

urlpatterns = [
    # Website + voice API (Bearer). Matched FIRST — these paths are frozen (voice depends on them).
    path("api/v1/", include("budtender.urls")),
    # In-store POS screens (login-gated HTMX). The POS was built for the root namespace
    # and owns no /api/ paths, so root-mount is collision-free and avoids a /pos/pos/ route.
    path("", include("pos.urls")),
]
