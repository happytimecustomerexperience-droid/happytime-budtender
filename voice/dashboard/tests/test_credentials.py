"""P6: the credentials editor persists + applies a value live (os.environ + settings) and never
renders or wipes a secret by accident. Offline.
"""

from __future__ import annotations

import os

import pytest
from django.conf import settings
from django.urls import reverse


def test_mask_never_shows_full_secret():
    from dashboard import credentials as cred

    assert cred.mask("") == ""
    assert cred.mask("short") == "••••"
    masked = cred.mask("sk-supersecretvalue12345")
    assert "supersecret" not in masked
    assert masked.startswith("sk-")


@pytest.mark.django_db
def test_set_credential_applies_to_env_and_settings(monkeypatch):
    from dashboard import credentials as cred

    monkeypatch.delenv("HHT_TRANSFER_NUMBER_YAKIMA", raising=False)
    cred.set_credential("HHT_TRANSFER_NUMBER_YAKIMA", "+15090000000")
    assert os.environ["HHT_TRANSFER_NUMBER_YAKIMA"] == "+15090000000"
    assert settings.HHT_TRANSFER_NUMBER_YAKIMA == "+15090000000"

    from dashboard.models import Credential

    assert Credential.objects.get(name="HHT_TRANSFER_NUMBER_YAKIMA").value == "+15090000000"


@pytest.mark.django_db
def test_apply_all_reasserts_db_over_env(monkeypatch):
    from dashboard import credentials as cred
    from dashboard.models import Credential

    Credential.objects.create(name="VAPI_SQUAD_ID", value="squad_from_db")
    monkeypatch.setenv("VAPI_SQUAD_ID", "squad_from_env")
    n = cred.apply_all()
    assert n >= 1
    assert os.environ["VAPI_SQUAD_ID"] == "squad_from_db"  # DB override wins after apply


@pytest.mark.django_db
def test_save_view_applies_and_blank_keeps(client, django_user_model, monkeypatch):
    staff = django_user_model.objects.create_user("s", password="x", is_staff=True, is_superuser=True)
    client.force_login(staff)

    monkeypatch.delenv("VAPI_SQUAD_ID", raising=False)
    # Save a value → applied + masked status set.
    resp = client.post(reverse("dash-credentials-save"), {"name": "VAPI_SQUAD_ID", "value": "sq_123"})
    assert resp.status_code == 200
    assert os.environ["VAPI_SQUAD_ID"] == "sq_123"

    # Blank submit must NOT wipe the existing value (placeholder says "leave blank to keep").
    client.post(reverse("dash-credentials-save"), {"name": "VAPI_SQUAD_ID", "value": ""})
    assert os.environ["VAPI_SQUAD_ID"] == "sq_123"


@pytest.mark.django_db
def test_save_view_rejects_unknown_credential(client, django_user_model):
    staff = django_user_model.objects.create_user("s2", password="x", is_staff=True, is_superuser=True)
    client.force_login(staff)
    resp = client.post(reverse("dash-credentials-save"), {"name": "NOT_A_REAL_KEY", "value": "x"})
    assert resp.status_code == 400


@pytest.mark.django_db
def test_credentials_page_renders_grouped_catalog(client, django_user_model):
    staff = django_user_model.objects.create_user("s3", password="x", is_staff=True, is_superuser=True)
    client.force_login(staff)
    resp = client.get(reverse("dash-credentials"))
    assert resp.status_code == 200
    assert b"VAPI_PRIVATE_KEY" in resp.content  # the catalog renders
    assert b"N8N_WEBHOOK_URL" in resp.content
