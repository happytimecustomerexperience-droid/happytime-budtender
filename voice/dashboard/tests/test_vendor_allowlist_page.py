"""/dashboard/vendor-allowlist/: list, add, bulk add, edit, deactivate, delete, the owner phone,
the routing status line and the 'would this number be routed?' box."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

# Credential saves write os.environ + settings: reuse that module's snapshot/restore (autouse).
from dashboard.tests.test_credentials import isolated_process_state  # noqa: F401

OWNER = "+15095550199"


@pytest.fixture(autouse=True)
def _owner(settings):
    settings.HHT_OWNER_PHONE = OWNER
    settings.HHT_DYNAMIC_GREETING = False


@pytest.fixture
def staff_client(client, db):
    client.force_login(User.objects.create_user("staffer", password="x", is_staff=True))
    return client


@pytest.fixture
def owner_client(client, db):
    client.force_login(User.objects.create_superuser("owner", password="x", email="o@example.com"))
    return client


def _entries():
    from dashboard.models import VendorAllowlistEntry

    return list(VendorAllowlistEntry.objects.order_by("phone").values_list("name", "phone", "active"))


@pytest.mark.django_db
def test_page_renders_status_and_nav(staff_client):
    resp = staff_client.get(reverse("dash-vendor-allowlist"))
    body = resp.content.decode()
    assert resp.status_code == 200
    assert 'href="/dashboard/vendor-allowlist/"' in body  # nav link
    assert "HHT_DYNAMIC_GREETING" in body and "routing cannot work without it" in body
    assert "ending 0199" in body and OWNER not in body  # plain staff see the last 4 only


@pytest.mark.django_db
def test_add_normalises_and_refuses_bad_or_duplicate(staff_client):
    url = reverse("dash-vendor-allowlist-add")
    resp = staff_client.post(url, {"name": "Cascade Crest", "phone": "(509) 555-7001", "active": "on"})
    assert resp.status_code == 302
    assert _entries() == [("Cascade Crest", "+15095557001", True)]

    resp = staff_client.post(url, {"name": "Dup", "phone": "509.555.7001", "active": "on"})
    assert resp.status_code == 200 and b"already exists" in resp.content
    resp = staff_client.post(url, {"name": "Short", "phone": "555-7001", "active": "on"})
    assert resp.status_code == 200 and b"Not a full US phone number" in resp.content
    assert len(_entries()) == 1


@pytest.mark.django_db
def test_names_are_autoescaped(staff_client):
    staff_client.post(
        reverse("dash-vendor-allowlist-add"), {"name": "<script>alert(1)</script>", "phone": "5095557001"}
    )
    body = staff_client.get(reverse("dash-vendor-allowlist")).content.decode()
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


@pytest.mark.django_db
def test_bulk_add_saves_good_lines_and_reports_each_bad_one(staff_client):
    from dashboard.models import VendorAllowlistEntry

    VendorAllowlistEntry.objects.create(name="Existing", phone="+15095550001")
    lines = "\n".join(
        [
            "Cascade Crest, (509) 555-7001",
            "",
            "no comma here",
            "Fern Farms, 555-1234",
            "Old Friend, 509-555-0001",
            "Twice, 5095557001",
            "Green Valley, Inc., +1 360 555 2000",
        ]
    )
    resp = staff_client.post(reverse("dash-vendor-allowlist-bulk"), {"lines": lines})
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "Added 2." in body
    assert "Line 3: Write it as: Name, number" in body
    assert "Line 4 (Fern Farms): Not a full US phone number" in body
    assert "Line 5 (Old Friend): Already on the list" in body
    assert "Line 6 (Twice): Same number as an earlier line" in body
    assert ("Green Valley, Inc.", "+13605552000", True) in _entries()
    assert ("Cascade Crest", "+15095557001", True) in _entries()
    assert len(_entries()) == 3


@pytest.mark.django_db
def test_bulk_add_all_good_redirects(staff_client):
    resp = staff_client.post(reverse("dash-vendor-allowlist-bulk"), {"lines": "A, 5095557001\nB, 5095557002"})
    assert resp.status_code == 302 and len(_entries()) == 2


@pytest.mark.django_db
def test_edit_toggle_delete(staff_client):
    from dashboard.models import VendorAllowlistEntry

    e = VendorAllowlistEntry.objects.create(name="Cascade", phone="+15095557001")
    assert staff_client.get(reverse("dash-vendor-allowlist-edit", args=[e.pk])).status_code == 200
    resp = staff_client.post(
        reverse("dash-vendor-allowlist-edit", args=[e.pk]),
        {"name": "Cascade Crest", "phone": "509 555 7009", "store": "pullman", "note": "rep: Marcus", "active": "on"},
    )
    assert resp.status_code == 302
    e.refresh_from_db()
    assert (e.name, e.phone, e.store, e.note) == ("Cascade Crest", "+15095557009", "pullman", "rep: Marcus")

    staff_client.post(reverse("dash-vendor-allowlist-toggle", args=[e.pk]))
    e.refresh_from_db()
    assert e.active is False
    staff_client.post(reverse("dash-vendor-allowlist-toggle", args=[e.pk]))
    e.refresh_from_db()
    assert e.active is True

    staff_client.post(reverse("dash-vendor-allowlist-delete", args=[e.pk]))
    assert not VendorAllowlistEntry.objects.exists()


@pytest.mark.django_db
def test_get_on_post_only_actions_is_refused(staff_client):
    assert staff_client.get(reverse("dash-vendor-allowlist-delete", args=[1])).status_code == 405
    assert staff_client.get(reverse("dash-vendor-allowlist-add")).status_code == 405


@pytest.mark.django_db
def test_csrf_is_enforced(db):
    from django.test import Client

    c = Client(enforce_csrf_checks=True)
    c.force_login(User.objects.create_user("s2", password="x", is_staff=True))
    resp = c.post(reverse("dash-vendor-allowlist-add"), {"name": "X", "phone": "5095557001"})
    assert resp.status_code == 403 and not _entries()


@pytest.mark.django_db
def test_test_box_uses_the_matcher_and_records_nothing(staff_client):
    from dashboard.models import VendorAllowlistEntry
    from voice.models import VoiceCall

    e = VendorAllowlistEntry.objects.create(name="Cascade", phone="+15095557001")
    url = reverse("dash-vendor-allowlist-test")
    body = staff_client.post(url, {"number": "(509) 555-7001"}).content.decode()
    assert "routed to owner" in body and "dynamic greeting is off" in body
    body = staff_client.post(url, {"number": "(509) 555-7002"}).content.decode()
    assert "Not on the allowlist" in body
    body = staff_client.post(url, {"number": "anonymous"}).content.decode()
    assert "Not a full US phone number" in body
    e.refresh_from_db()
    assert e.match_count == 0 and e.last_matched_at is None
    assert not VoiceCall.objects.exists()


@pytest.mark.django_db
def test_owner_phone_is_owner_only(staff_client):
    resp = staff_client.post(reverse("dash-vendor-allowlist-owner"), {"owner_phone": "5095550111"})
    assert resp.status_code == 403


@pytest.mark.django_db
def test_owner_saves_normalised_number_and_clears_it(owner_client, settings):
    from dashboard.models import Credential
    from voice import vendor_allowlist as va

    settings.HHT_OWNER_PHONE = ""
    url = reverse("dash-vendor-allowlist-owner")
    resp = owner_client.post(url, {"owner_phone": "(509) 555-0111", "action": "save"})
    assert resp.status_code == 302
    assert Credential.objects.get(name="HHT_OWNER_PHONE").value == "+15095550111"
    assert va.owner_number() == "+15095550111"
    assert "+15095550111" in owner_client.get(reverse("dash-vendor-allowlist")).content.decode()

    owner_client.post(url, {"owner_phone": "+44 20 7946 0000", "action": "save"})
    assert va.owner_number() == "+15095550111"  # refused, unchanged

    owner_client.post(url, {"action": "clear"})
    assert not Credential.objects.filter(name="HHT_OWNER_PHONE").exists()
    assert va.owner_number() == ""


def test_credentials_page_refuses_a_non_us_owner_phone():
    from dashboard import credentials as cred

    assert cred.validate("HHT_OWNER_PHONE", "+15095550111") is None
    assert cred.validate("HHT_OWNER_PHONE", "(509) 555-0111")  # must be written as E.164 there
    assert cred.validate("HHT_OWNER_PHONE", "+442079460000")
