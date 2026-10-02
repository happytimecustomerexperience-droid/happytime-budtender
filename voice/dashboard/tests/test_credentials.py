"""P6: the credentials editor persists + applies a value live (os.environ + settings), is the
superuser's alone, only accepts URLs on its allowlist, never renders any part of a secret, can clear
a value, and every process (web workers AND the Celery worker) sees a save. Offline.
"""

from __future__ import annotations

import os

import pytest
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from dashboard import credentials as cred

_MISSING = object()


@pytest.fixture(autouse=True)
def isolated_process_state():
    """Credential state lives in os.environ, django settings, a module baseline and the cache —
    snapshot all of it so one test's saves never reach another, and give each test a 'fresh
    process' (nothing applied yet)."""
    names = [c["name"] for c in cred.CREDENTIAL_CATALOG]
    env = {n: os.environ.get(n) for n in names}
    conf = {n: getattr(settings, n, _MISSING) for n in names}
    cred._BASELINE.clear()
    cred._applied = cred._NEVER
    cache.delete(cred._VERSION_KEY)
    yield
    for n in names:
        if env[n] is None:
            os.environ.pop(n, None)
        else:
            os.environ[n] = env[n]
        if conf[n] is _MISSING:
            settings.__dict__.pop(n, None)
            if hasattr(settings._wrapped, n):
                delattr(settings._wrapped, n)
        else:
            setattr(settings, n, conf[n])
    cred._BASELINE.clear()
    cred._applied = cred._NEVER
    cache.delete(cred._VERSION_KEY)


@pytest.fixture
def owner(client, django_user_model):
    u = django_user_model.objects.create_user("owner", password="x", is_staff=True, is_superuser=True)
    client.force_login(u)
    return client


def _save(client, name, value):
    return client.post(reverse("dash-credentials-save"), {"name": name, "value": value})


def _stored(name) -> bool:
    from dashboard.models import Credential

    return Credential.objects.filter(name=name).exists()


# ── existing behaviour ────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_set_credential_applies_to_env_and_settings(monkeypatch):
    monkeypatch.delenv("HHT_TRANSFER_NUMBER_YAKIMA", raising=False)
    cred.set_credential("HHT_TRANSFER_NUMBER_YAKIMA", "+15090000000")
    assert os.environ["HHT_TRANSFER_NUMBER_YAKIMA"] == "+15090000000"
    assert settings.HHT_TRANSFER_NUMBER_YAKIMA == "+15090000000"

    from dashboard.models import Credential

    assert Credential.objects.get(name="HHT_TRANSFER_NUMBER_YAKIMA").value == "+15090000000"


@pytest.mark.django_db
def test_apply_all_reasserts_db_over_env(monkeypatch):
    from dashboard.models import Credential

    Credential.objects.create(name="VAPI_SQUAD_ID", value="squad_from_db")
    monkeypatch.setenv("VAPI_SQUAD_ID", "squad_from_env")
    n = cred.apply_all()
    assert n >= 1
    assert os.environ["VAPI_SQUAD_ID"] == "squad_from_db"  # DB override wins after apply


@pytest.mark.django_db
def test_save_view_applies_and_blank_keeps(owner, monkeypatch):
    monkeypatch.delenv("VAPI_SQUAD_ID", raising=False)
    resp = _save(owner, "VAPI_SQUAD_ID", "sq_123")
    assert resp.status_code == 200
    assert os.environ["VAPI_SQUAD_ID"] == "sq_123"

    # Blank submit must NOT wipe the existing value (placeholder says "leave blank to keep").
    _save(owner, "VAPI_SQUAD_ID", "")
    assert os.environ["VAPI_SQUAD_ID"] == "sq_123"


@pytest.mark.django_db
def test_save_view_rejects_unknown_credential(owner):
    assert _save(owner, "NOT_A_REAL_KEY", "x").status_code == 400
    assert owner.post(reverse("dash-credentials-clear"), {"name": "NOT_A_REAL_KEY"}).status_code == 400


@pytest.mark.django_db
def test_credentials_page_renders_grouped_catalog(owner):
    resp = owner.get(reverse("dash-credentials"))
    assert resp.status_code == 200
    assert b"VAPI_PRIVATE_KEY" in resp.content  # the catalog renders
    assert b"N8N_WEBHOOK_URL" in resp.content


# ── fix 1: superuser only ─────────────────────────────────────────────────────
@pytest.mark.django_db
def test_staff_who_is_not_superuser_gets_403_everywhere(client, django_user_model):
    staff = django_user_model.objects.create_user("clerk", password="x", is_staff=True)
    client.force_login(staff)
    assert client.get(reverse("dash-credentials")).status_code == 403
    assert _save(client, "VAPI_SQUAD_ID", "sq_evil").status_code == 403
    assert client.post(reverse("dash-credentials-clear"), {"name": "VAPI_SQUAD_ID"}).status_code == 403
    assert not _stored("VAPI_SQUAD_ID")  # and nothing was saved


@pytest.mark.django_db
def test_superuser_can_view_and_save(owner):
    assert owner.get(reverse("dash-credentials")).status_code == 200
    assert _save(owner, "VAPI_SQUAD_ID", "sq_ok").status_code == 200
    assert _stored("VAPI_SQUAD_ID")


# ── fix 2: URL allowlist ──────────────────────────────────────────────────────
URL_CASES = [
    # (name, value, accepted)
    ("HHT_BUDTENDER_BASE_URL", "http://web:8000", True),
    ("HHT_BUDTENDER_BASE_URL", "https://api.happytimeweed.com", True),
    ("HHT_BUDTENDER_BASE_URL", "https://budtender.happytimeweed.com/api", True),
    ("HHT_BUDTENDER_BASE_URL", "https://evil.example.com", False),
    ("HHT_BUDTENDER_BASE_URL", "http://web:8001", False),
    ("HHT_BUDTENDER_BASE_URL", "http://evil.com:8000", False),
    ("HHT_BUDTENDER_BASE_URL", "http://api.happytimeweed.com", False),  # http only for web:8000
    ("HHT_BUDTENDER_BASE_URL", "https://happytimeweed.com.evil.com", False),
    ("HHT_BUDTENDER_BASE_URL", "https://evilhappytimeweed.com", False),
    ("HHT_BUDTENDER_BASE_URL", "https://api.happytimeweed.com@evil.com", False),
    ("HHT_BUDTENDER_BASE_URL", "https://evil.com@api.happytimeweed.com", False),
    ("HHT_BUDTENDER_BASE_URL", "https://evil.com\\@api.happytimeweed.com", False),
    ("N8N_WEBHOOK_URL", "https://n8n.example.com/webhook/abc", True),
    ("N8N_WEBHOOK_URL", "https://hooks.n8n.cloud:5678/webhook/abc", True),
    ("N8N_WEBHOOK_URL", "http://n8n.example.com/webhook/abc", False),
    ("N8N_WEBHOOK_URL", "https://127.0.0.1/webhook", False),
    ("N8N_WEBHOOK_URL", "https://localhost/webhook", False),
    ("N8N_WEBHOOK_URL", "https://app.localhost/webhook", False),
    ("N8N_WEBHOOK_URL", "https://10.0.0.5/webhook", False),
    ("N8N_WEBHOOK_URL", "https://192.168.1.10/webhook", False),
    ("N8N_WEBHOOK_URL", "https://172.16.0.1/webhook", False),
    ("N8N_WEBHOOK_URL", "https://169.254.169.254/latest/meta-data", False),  # cloud metadata
    ("N8N_WEBHOOK_URL", "https://8.8.8.8/webhook", False),  # any IP literal, even public
    ("N8N_WEBHOOK_URL", "https://[::1]/webhook", False),
    ("N8N_WEBHOOK_URL", "https://[fd00::1]/webhook", False),
    ("N8N_WEBHOOK_URL", "https://[::ffff:10.0.0.1]/webhook", False),
    ("N8N_WEBHOOK_URL", "https://2130706433/webhook", False),  # 127.0.0.1 as one integer
    ("N8N_WEBHOOK_URL", "https://127.1/webhook", False),
    ("N8N_WEBHOOK_URL", "https://0x7f.0.0.1/webhook", False),
    ("N8N_WEBHOOK_URL", "https://metadata.internal/webhook", False),
    ("N8N_WEBHOOK_URL", "https://printer.local/webhook", False),
    ("N8N_WEBHOOK_URL", "https://intranet/webhook", False),
    ("N8N_WEBHOOK_URL", "https://user:pw@n8n.example.com/webhook", False),
    ("N8N_WEBHOOK_URL", "https://n8n.example.com:notaport/webhook", False),
    ("N8N_WEBHOOK_URL", "https://n8n.example.com/web hook", False),
    ("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T000/B000/XXXX", True),
    ("SLACK_WEBHOOK_URL", "http://hooks.slack.com/services/T000/B000/XXXX", False),
    ("SLACK_WEBHOOK_URL", "https://hooks.slack.com.evil.com/services/T000/B000/XXXX", False),
    ("SLACK_WEBHOOK_URL", "https://evil.com/https://hooks.slack.com/", False),
    ("SLACK_WEBHOOK_URL", "https://hooks.slack.com@evil.com/services/x", False),
    ("SLACK_WEBHOOK_URL", "https://slack.com/services/x", False),
    ("SLACK_WEBHOOK_URL", "https://hooks.slack.com", False),  # no trailing slash = not the prefix
]


@pytest.mark.django_db
@pytest.mark.parametrize("name,value,accepted", URL_CASES, ids=[f"{n}-{v}" for n, v, _ in URL_CASES])
def test_url_allowlist_on_save(owner, name, value, accepted):
    resp = _save(owner, name, value)
    assert resp.status_code == 200
    if accepted:
        assert _stored(name)
        assert os.environ[name] == value
        assert b"Not saved" not in resp.content
    else:
        assert not _stored(name)  # nothing saved…
        assert os.environ.get(name, "") != value  # …nor applied to this process
        assert getattr(settings, name, "") != value
        assert b"Not saved" in resp.content  # and the staff user is told why, inline
        assert "error" in resp["HX-Trigger"]


@pytest.mark.django_db
def test_rejected_url_keeps_the_previous_value(owner):
    _save(owner, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/GOOD")
    _save(owner, "SLACK_WEBHOOK_URL", "https://evil.example.com/steal")
    assert os.environ["SLACK_WEBHOOK_URL"] == "https://hooks.slack.com/services/GOOD"


# ── fix 3: secrets are write-only ─────────────────────────────────────────────
@pytest.mark.django_db
def test_secret_is_never_shown_not_even_a_preview(owner):
    secret = "sk-supersecretvalue12345"
    resp = _save(owner, "VAPI_PRIVATE_KEY", secret)
    page = owner.get(reverse("dash-credentials"))
    for html in (resp.content.decode(), page.content.decode()):
        assert secret not in html
        assert secret[:3] + "…" not in html  # the old mask: first 3 chars + ellipsis + last 2
        assert "…" + secret[-2:] not in html
        assert "sk-" not in html
    assert b'badge green">set<' in resp.content  # it says only "set"


@pytest.mark.django_db
def test_secret_slack_url_is_not_previewed_but_non_secrets_are(owner):
    _save(owner, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/PRIVATEPART")
    _save(owner, "VAPI_SQUAD_ID", "squad_visible_123")
    html = owner.get(reverse("dash-credentials")).content.decode()
    assert "PRIVATEPART" not in html and "hooks.slack.com/services" not in html
    assert "htt…RT" not in html  # the old mask of this value
    assert "squad_visible_123" in html  # a non-secret keeps showing its value


def test_mask_helper_is_gone():
    assert not hasattr(cred, "mask")


# ── fix 4: clear ──────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_clear_deletes_the_row_and_falls_back_to_env(owner, monkeypatch):
    monkeypatch.setenv("VAPI_SQUAD_ID", "squad_from_env")
    monkeypatch.setattr(settings, "VAPI_SQUAD_ID", "squad_from_env", raising=False)
    _save(owner, "VAPI_SQUAD_ID", "squad_from_dashboard")
    assert os.environ["VAPI_SQUAD_ID"] == "squad_from_dashboard"
    assert b"cred-clear-VAPI_SQUAD_ID" in owner.get(reverse("dash-credentials")).content

    resp = owner.post(reverse("dash-credentials-clear"), {"name": "VAPI_SQUAD_ID"})
    assert resp.status_code == 200
    assert not _stored("VAPI_SQUAD_ID")
    assert os.environ["VAPI_SQUAD_ID"] == "squad_from_env"
    assert settings.VAPI_SQUAD_ID == "squad_from_env"
    assert b"cred-clear-VAPI_SQUAD_ID" not in resp.content  # nothing stored → no Clear button


@pytest.mark.django_db
def test_clear_with_no_env_default_leaves_it_unset(owner, monkeypatch):
    monkeypatch.delenv("HHT_TRANSFER_NUMBER_PULLMAN", raising=False)
    monkeypatch.setattr(settings, "HHT_TRANSFER_NUMBER_PULLMAN", "", raising=False)
    _save(owner, "HHT_TRANSFER_NUMBER_PULLMAN", "+15095550100")
    owner.post(reverse("dash-credentials-clear"), {"name": "HHT_TRANSFER_NUMBER_PULLMAN"})
    assert cred.current_value("HHT_TRANSFER_NUMBER_PULLMAN") == ""


@pytest.mark.django_db
def test_clear_is_post_only_and_csrf_protected(django_user_model):
    from django.test import Client

    c = Client(enforce_csrf_checks=True)
    c.force_login(django_user_model.objects.create_user("o2", password="x", is_staff=True, is_superuser=True))
    cred.set_credential("VAPI_SQUAD_ID", "keep_me")
    assert c.post(reverse("dash-credentials-clear"), {"name": "VAPI_SQUAD_ID"}).status_code == 403  # no CSRF token
    assert c.get(reverse("dash-credentials-clear")).status_code == 405
    assert _stored("VAPI_SQUAD_ID")


# ── fix 5: every process sees a save ──────────────────────────────────────────
def _become_stale_process(monkeypatch, name, env_value):
    """Turn this process into a DIFFERENT one that has not seen the latest save: it still has the
    env/.env value and is in step with the cache as it was BEFORE the save (no version token yet) —
    so only a fresh token published by the save can make it re-apply."""
    monkeypatch.setenv(name, env_value)
    monkeypatch.setattr(settings, name, env_value, raising=False)
    cred._BASELINE.clear()
    cred._applied = None


@pytest.mark.django_db
def test_save_publishes_a_fresh_version_token():
    cred.set_credential("VAPI_SQUAD_ID", "a")
    first = cache.get(cred._VERSION_KEY)
    cred.set_credential("VAPI_SQUAD_ID", "b")
    assert first and cache.get(cred._VERSION_KEY) not in (None, first)


@pytest.mark.django_db
def test_a_save_in_one_process_reaches_another_web_worker_on_its_next_request(client, monkeypatch):
    cred.set_credential("STAFF_ALERT_EMAIL", "owner@example.com")  # "worker A" handles the save
    _become_stale_process(monkeypatch, "STAFF_ALERT_EMAIL", "old@example.com")  # now we are "worker B"
    assert settings.STAFF_ALERT_EMAIL == "old@example.com"

    client.get(reverse("dash-credentials"))  # any request (here: redirected to login)

    assert os.environ["STAFF_ALERT_EMAIL"] == "owner@example.com"
    assert settings.STAFF_ALERT_EMAIL == "owner@example.com"


@pytest.mark.django_db
def test_a_save_reaches_the_celery_worker_at_task_prerun(monkeypatch):
    """The staff alert email is read by the worker that sends alerts — a different process from the
    web worker that took the save (the bug seen in prod: dashboard value != worker value)."""
    from core.celery import debug_task
    from crm.sinks import _recipients_for

    cred.set_credential("STAFF_ALERT_EMAIL", "owner@example.com")
    _become_stale_process(monkeypatch, "STAFF_ALERT_EMAIL", "old@example.com")
    assert _recipients_for("yakima")[0] == "old@example.com"

    debug_task.apply()  # eager run: Celery fires task_prerun exactly as a real worker does

    assert _recipients_for("yakima")[0] == "owner@example.com"


@pytest.mark.django_db
def test_a_clear_in_one_process_reaches_another(client, monkeypatch):
    monkeypatch.setenv("VAPI_SQUAD_ID", "squad_from_env")
    monkeypatch.setattr(settings, "VAPI_SQUAD_ID", "squad_from_env", raising=False)
    cred.set_credential("VAPI_SQUAD_ID", "squad_from_db")
    cred.refresh_if_stale()  # this process is in step
    assert os.environ["VAPI_SQUAD_ID"] == "squad_from_db"

    # another process clears it: the row is deleted and a new token published — not by us
    from dashboard.models import Credential

    Credential.objects.filter(name="VAPI_SQUAD_ID").delete()
    cache.set(cred._VERSION_KEY, "cleared-elsewhere", None)

    client.get(reverse("dash-credentials"))
    assert os.environ["VAPI_SQUAD_ID"] == "squad_from_env"
    assert settings.VAPI_SQUAD_ID == "squad_from_env"


@pytest.mark.django_db
def test_unchanged_version_costs_no_db_read(monkeypatch):
    calls = []
    real = cred.apply_all
    monkeypatch.setattr(cred, "apply_all", lambda: calls.append(1) or real())
    cred.refresh_if_stale()
    cred.refresh_if_stale()
    cred.refresh_if_stale()
    assert len(calls) == 1  # first request applies; the rest are one cache.get each
    cache.set(cred._VERSION_KEY, "someone-saved", None)
    cred.refresh_if_stale()
    cred.refresh_if_stale()
    assert len(calls) == 2


@pytest.mark.django_db
def test_no_usable_cache_behaves_as_before(monkeypatch):
    """A cache that is down must neither break a request/task nor a save: apply once, as before."""
    from dashboard.models import Credential

    def boom(*a, **k):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(cache, "get", boom)
    monkeypatch.setattr(cache, "set", boom)
    Credential.objects.create(name="VAPI_SQUAD_ID", value="squad_from_db")
    cred.refresh_if_stale()  # does not raise
    assert os.environ["VAPI_SQUAD_ID"] == "squad_from_db"
    cred.set_credential("VAPI_SQUAD_ID", "squad_new")  # a save still works and applies here
    assert os.environ["VAPI_SQUAD_ID"] == "squad_new"


@pytest.mark.django_db
def test_vapi_public_key_is_configurable_and_not_secret(owner):
    entry = cred._CATALOG_BY_NAME["VAPI_PUBLIC_KEY"]
    assert entry["group"] == "Vapi" and entry["secret"] is False
    _save(owner, "VAPI_PUBLIC_KEY", "pk_test_console")
    assert settings.VAPI_PUBLIC_KEY == "pk_test_console"  # what dashboard/playground.py reads
    assert "pk_test_console" in owner.get(reverse("dash-credentials")).content.decode()
