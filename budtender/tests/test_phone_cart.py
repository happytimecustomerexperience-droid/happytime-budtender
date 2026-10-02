import json

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from budtender.models import PhoneCartDraft, Product
from customers.models import DutchieWriteAudit
from dutchie.session import Store
from pos import views as pos_views

pytestmark = pytest.mark.django_db

TOKEN = "test-token"


def _auth():
    return {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}


def _product(**kw):
    data = {
        "sku": "SKU-1",
        "product_id": "P1",
        "location_slug": "yakima",
        "name": "Blue Dream Cart",
        "brand": "Happy",
        "category": "cartridge",
        "price": 30,
        "price_was": 35,
        "quantity_on_hand": 8,
        "availability": True,
    }
    data.update(kw)
    return Product.objects.create(**data)


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
def test_voice_staged_draft_carries_the_callback_number_for_register_autoresolve(client):
    """A voice-staged order used to land at the register anonymous.

    The draft stored only phone_hash + last4, so the claim gate in pos/views.py (which keys on
    contact_phone / dutchie_acct_id) never fired and staff had to look the customer up by hand on
    every call. The web-order path already set contact_phone; the voice path now does too.
    """
    _product()

    resp = client.post(
        "/api/v1/phone-cart/upsert",
        data=json.dumps({
            "call_id": "call-link-1",
            "store": "yakima",
            "phone": "+15095551234",
            "action": "add_item",
            "sku": "SKU-1",
            "quantity": 1,
        }),
        content_type="application/json",
        **_auth(),
    )
    assert resp.status_code == 200

    draft = PhoneCartDraft.objects.get(call_id="call-link-1")
    assert draft.contact_phone == "+15095551234", "register cannot auto-resolve without this"
    # The hash + last4 discipline is unchanged — this adds the callback number a pending order
    # needs to be fulfillable, it does not replace the hashed identity.
    assert draft.phone_last4 == "1234"
    assert draft.phone_hash and draft.phone_hash != "+15095551234"


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
def test_phone_cart_api_stages_quotes_and_releases_without_dutchie_write(client):
    _product()

    resp = client.post(
        "/api/v1/phone-cart/upsert",
        data=json.dumps({
            "call_id": "call-1",
            "store": "yakima",
            "phone": "+15095551234",
            "action": "add_item",
            "sku": "SKU-1",
            "quantity": 2,
        }),
        content_type="application/json",
        **_auth(),
    )

    assert resp.status_code == 200
    body = resp.json()
    draft = body["draft"]
    assert draft["status"] == "open"
    assert draft["phone_last4"] == "1234"
    assert draft["lines"][0]["sku"] == "SKU-1"
    assert draft["lines"][0]["product_id"] == "P1"
    assert draft["quote"]["subtotal"] == 70.0
    assert draft["quote"]["discounts"] == 10.0
    assert draft["quote"]["total"] == 60.0

    release = client.post(
        "/api/v1/phone-cart/release",
        data=json.dumps({"call_id": "call-1", "store": "yakima"}),
        content_type="application/json",
        **_auth(),
    )

    assert release.status_code == 200
    assert release.json()["draft"]["status"] == "released"
    assert PhoneCartDraft.objects.get(call_id="call-1").status == PhoneCartDraft.Status.RELEASED
    assert DutchieWriteAudit.objects.count() == 0


def test_pos_claim_loads_released_phone_cart_into_session_without_submit(client, monkeypatch):
    user = User.objects.create_user("bud", password="pw12345!")
    client.force_login(user)
    store = Store(
        name="yakima",
        base_url="https://bo",
        pos_base_url="https://pos",
        org_id=1,
        lsp_id=1,
        loc_id=1,
        register_id=1,
        username="u",
        password="p",
        api_key="k",
    )
    monkeypatch.setattr(pos_views, "load_stores", lambda: {"yakima": store})
    monkeypatch.setattr(pos_views.catalog, "find_item", lambda store_name, product_id=None, serial=None: {
        "product_id": "P1",
        "ProductId": "P1",
        "BatchId": "B1",
        "SerialNo": "S1",
        "UnitPrice": 30.0,
        "RecUnitPrice": 35.0,
        "ProductDesc": "Blue Dream Cart",
        "CannbisProduct": "Yes",
        "brand": "Happy",
        "cat_key": "vapes",
    } if product_id == "P1" else None)
    draft = PhoneCartDraft.objects.create(
        call_id="call-claim",
        location_slug="yakima",
        status=PhoneCartDraft.Status.RELEASED,
        lines=[{"sku": "SKU-1", "product_id": "P1", "name": "Blue Dream Cart", "quantity": 2}],
    )

    resp = client.post(
        reverse("phone_cart_claim"),
        {"draft_token": draft.draft_token},
        SERVER_NAME="localhost",
    )

    assert resp.status_code == 200
    cart = client.session["cart"]
    assert cart == [{
        "ProductId": "P1",
        "BatchId": "B1",
        "SerialNo": "S1",
        "UnitPrice": 30.0,
        "RecUnitPrice": 35.0,
        "ProductDesc": "Blue Dream Cart",
        "CannbisProduct": "Yes",
        "Discount": 0.0,
        "Cnt": 2,
    }]
    draft.refresh_from_db()
    assert draft.status == PhoneCartDraft.Status.CLAIMED
    assert DutchieWriteAudit.objects.count() == 0


def test_pos_queue_panel_lists_saved_phone_carts_for_budtenders(client):
    user = User.objects.create_user("bud2", password="pw12345!")
    client.force_login(user)
    session = client.session
    session["store"] = "yakima"
    session.save()
    visible = PhoneCartDraft.objects.create(
        call_id="call-visible",
        location_slug="yakima",
        status=PhoneCartDraft.Status.RELEASED,
        pickup_name="Jane Phone",
        phone_last4="1234",
        lines=[{"sku": "SKU-1", "name": "Blue Dream Cart", "quantity": 2}],
        quote={"total": 60.0, "discounts": 10.0},
    )
    PhoneCartDraft.objects.create(
        call_id="call-claimed",
        location_slug="yakima",
        status=PhoneCartDraft.Status.CLAIMED,
        pickup_name="Already Loaded",
        lines=[{"sku": "SKU-2"}],
        quote={"total": 20.0},
    )
    PhoneCartDraft.objects.create(
        call_id="call-other-store",
        location_slug="pullman",
        status=PhoneCartDraft.Status.RELEASED,
        pickup_name="Pullman Caller",
        lines=[{"sku": "SKU-3"}],
        quote={"total": 40.0},
    )

    resp = client.get(reverse("queue_panel"), SERVER_NAME="localhost")

    assert resp.status_code == 200
    body = resp.content.decode()
    # Panel now carries online orders as well as phone calls, hence the label —
    # and the raw draft token was dropped from the row in favour of the customer's
    # name and contact, which is what staff actually calls out at the counter.
    assert "Orders waiting" in body
    assert visible.draft_token in body   # still posted by the Load form
    assert "Jane Phone" in body
    assert "$60.00" in body
    assert "Already Loaded" not in body
    assert "Pullman Caller" not in body

    screen = client.get(reverse("screen"), SERVER_NAME="localhost")
    assert screen.status_code == 200
    assert visible.draft_token in screen.content.decode()


def _online_cart():
    """What bundles/cart.py creates for every web shopper: the SAME session_token for all."""
    return PhoneCartDraft.objects.create(
        location_slug="yakima", source=PhoneCartDraft.Source.ONLINE, session_token="online",
        status=PhoneCartDraft.Status.OPEN, contact_phone="5095550100", lines=[{"sku": "SKU-1"}],
    )


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
@pytest.mark.parametrize("path,extra", [
    ("/api/v1/phone-cart/release", {}),
    ("/api/v1/phone-cart/claim", {}),
    ("/api/v1/phone-cart/upsert", {"store": "yakima", "action": "quote"}),
    ("/api/v1/phone-cart/upsert", {"store": "yakima", "action": "set_quantity", "sku": "SKU-1", "quantity": 9}),
])
@pytest.mark.parametrize("sentinel", ["online", " Online "])
def test_the_shared_online_token_never_addresses_a_strangers_cart(client, path, extra, sentinel):
    """Every web cart carries session_token="online", so release/claim/upsert "by token" acted
    on whichever stranger's online cart was newest. The sentinel is refused outright."""
    cart = _online_cart()

    resp = client.post(path, data=json.dumps({"session_token": sentinel, **extra}),
                       content_type="application/json", **_auth())

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    cart.refresh_from_db()
    assert cart.status == PhoneCartDraft.Status.OPEN
    assert cart.released_at is None and cart.claimed_at is None
    assert cart.lines == [{"sku": "SKU-1"}]
    assert PhoneCartDraft.objects.count() == 1


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
def test_an_online_cart_is_still_reachable_by_its_own_draft_token(client):
    cart = _online_cart()

    resp = client.post("/api/v1/phone-cart/claim", data=json.dumps({"draft_token": cart.draft_token}),
                       content_type="application/json", **_auth())

    assert resp.status_code == 200
    cart.refresh_from_db()
    assert cart.status == PhoneCartDraft.Status.CLAIMED


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
def test_a_real_phone_session_token_still_works(client):
    draft = PhoneCartDraft.objects.create(location_slug="yakima", session_token="s-caller-1",
                                          status=PhoneCartDraft.Status.OPEN)

    resp = client.post("/api/v1/phone-cart/release", data=json.dumps({"session_token": "s-caller-1"}),
                       content_type="application/json", **_auth())

    assert resp.status_code == 200
    draft.refresh_from_db()
    assert draft.status == PhoneCartDraft.Status.RELEASED
