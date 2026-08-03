from core.settings import _prod_guard_errors


def test_prod_guard_rejects_default_secret_and_missing_token():
    assert _prod_guard_errors("insecure-dev-key-change-me", "") == [
        "SECRET_KEY",
        "HHT_BACKEND_TOKEN",
    ]


def test_prod_guard_accepts_real_secret_and_token():
    assert _prod_guard_errors("real-secret-key-value", "backend-token") == []


def test_prod_guard_rejects_missing_bundle_secret():
    # Without it every /custom-order link 400s — fail at boot, not at open rate.
    assert _prod_guard_errors("real-secret-key-value", "backend-token", "") == [
        "BUNDLE_URL_SECRET",
    ]


def test_prod_guard_accepts_a_configured_bundle_secret():
    assert _prod_guard_errors("real-secret-key-value", "backend-token", "s3cret") == []
