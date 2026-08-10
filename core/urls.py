from django.urls import include, path

urlpatterns = [
    # Website + voice API (Bearer). Matched FIRST — these paths are frozen (voice depends on them).
    path("api/v1/", include("budtender.urls")),
    # PUBLIC, unauthenticated bundle landing reached from marketing email. Mounted
    # before the POS root include so /custom-order can never be shadowed by a POS
    # route, and kept on its own prefix so the POS login gate is unaffected.
    path("custom-order/", include("bundles.urls")),
    # PUBLIC loyalty balance lookup, same rewrite treatment as /custom-order and the
    # same reason for sitting ahead of the POS root include.
    path("loyalty/", include("bundles.loyalty_urls")),
    # In-store POS screens (login-gated HTMX). The POS was built for the root namespace
    # and owns no /api/ paths, so root-mount is collision-free and avoids a /pos/pos/ route.
    path("", include("pos.urls")),
]
