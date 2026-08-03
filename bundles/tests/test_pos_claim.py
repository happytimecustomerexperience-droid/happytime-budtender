"""The POS side of an online order: claiming it must leave a customer selected.

`cart_submit` refuses to run without `session["acct_id"]`, so if the claim doesn't
select someone the budtender is stuck re-finding them with the shopper waiting.
"""
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

from budtender.models import PhoneCartDraft
from bundles.tests.test_resolver import live

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
pytestmark = pytest.mark.django_db


def _draft(**kw):
    d = dict(location_slug="yakima", source=PhoneCartDraft.Source.ONLINE,
             status=PhoneCartDraft.Status.RELEASED, pickup_name="Sam Reyes",
             contact_phone="5095551212", phone_last4="1212",
             lines=[{"product_id": "1", "sku": "1", "name": "Blue Dream 3.5g", "quantity": 1}],
             quote={"total": 25.0})
    d.update(kw)
    return PhoneCartDraft.objects.create(**d)


def _staff_client():
    User.objects.create_user("bud-claim", password="pw12345!")
    c = Client()
    c.login(username="bud-claim", password="pw12345!")
    s = c.session
    s["store"] = "yakima"
    s.save()
    return c


def _claim(client, draft, guest_client):
    # _active_store reads load_stores(), which is empty without Dutchie creds — so
    # without this the line-loading branch is skipped and the cart silently stays
    # empty. Stub a store so the real path runs.
    store = MagicMock()
    store.name = "yakima"
    with patch("bundles.customers._client", return_value=guest_client), \
         patch("pos.views._active_store", return_value=store), \
         patch("pos.views.catalog.find_item", return_value=live(product_id="1")):
        return client.post(reverse("phone_cart_claim"),
                           {"draft_token": draft.draft_token}, SERVER_NAME="localhost")


@override_settings(CACHES=CACHES_LOCMEM)
def test_claim_auto_selects_the_matched_customer():
    client = _staff_client()
    draft = _draft(dutchie_acct_id="4242", customer_name="Sam Reyes",
                   customer_status=PhoneCartDraft.Customer.MATCHED)
    guest = MagicMock()
    resp = _claim(client, draft, guest)

    assert resp.status_code == 200
    assert str(client.session["acct_id"]) == "4242"
    assert client.session["acct_name"] == "Sam Reyes"
    guest.create_guest.assert_not_called()


@override_settings(CACHES=CACHES_LOCMEM)
def test_claim_creates_an_account_when_there_is_none_then_selects_it():
    client = _staff_client()
    draft = _draft(customer_status=PhoneCartDraft.Customer.NEW)
    guest = MagicMock()
    guest.guest_search.return_value = {"Data": []}
    guest.create_guest.return_value = 8080

    resp = _claim(client, draft, guest)

    assert resp.status_code == 200
    assert str(client.session["acct_id"]) == "8080"
    assert "Created a new account" in resp.content.decode()
    kwargs = guest.create_guest.call_args.kwargs
    assert kwargs["first_name"] == "Sam"
    assert kwargs["last_name"] == "Reyes"
    assert kwargs["phone"] == "5095551212"
    draft.refresh_from_db()
    assert draft.dutchie_acct_id == "8080"


@override_settings(CACHES=CACHES_LOCMEM)
def test_claim_tells_staff_when_the_customer_cannot_be_resolved():
    client = _staff_client()
    draft = _draft()
    guest = MagicMock()
    guest.guest_search.side_effect = RuntimeError("dutchie down")

    resp = _claim(client, draft, guest)

    assert resp.status_code == 200
    assert "acct_id" not in client.session
    assert "look the customer up" in resp.content.decode()
    guest.create_guest.assert_not_called()


@override_settings(CACHES=CACHES_LOCMEM)
def test_claim_still_loads_the_cart_lines():
    client = _staff_client()
    draft = _draft(dutchie_acct_id="4242", customer_status=PhoneCartDraft.Customer.MATCHED)
    _claim(client, draft, MagicMock())
    assert len(client.session["cart"]) == 1
    draft.refresh_from_db()
    assert draft.status == PhoneCartDraft.Status.CLAIMED
