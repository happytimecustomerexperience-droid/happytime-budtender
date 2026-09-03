"""tests/test_store_facts_api.py — GET /api/voice/store-facts (root's read of owner-edited
store facts; P6 instant-refresh chain, kb/signals.py).

Asserts: 401 without the Bearer token; the expected JSON shape; an unconfirmed row is never
surfaced; an expired special is omitted (StoreFact.objects.current()).
"""

from __future__ import annotations

import datetime

import pytest


@pytest.mark.django_db
def test_store_facts_requires_bearer_token(client, settings):
    settings.HHT_BACKEND_TOKEN = "test-token"
    resp = client.get("/api/voice/store-facts")
    assert resp.status_code == 401
    assert resp.json()["ok"] is False


@pytest.mark.django_db
def test_store_facts_returns_expected_shape(client, settings):
    from kb.models import StoreFact

    settings.HHT_BACKEND_TOKEN = "test-token"
    StoreFact.objects.create(store="yakima", kind="hours", label="Yakima hours", value="8 AM–11:30 PM daily")
    StoreFact.objects.create(store="yakima", kind="address", label="Yakima address", value="1315 N 1st St")
    StoreFact.objects.create(store="yakima", kind="phone", label="Yakima phone", value="(509) 571-1106")
    StoreFact.objects.create(store="", kind="payment", label="Payment", value="cash, debit")
    StoreFact.objects.create(store="", kind="age", label="Age", value="21+")
    StoreFact.objects.create(store="yakima", kind="special", label="Weekly special", value="20% off flower")

    resp = client.get("/api/voice/store-facts", **{"HTTP_AUTHORIZATION": "Bearer test-token"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "stores", "global", "specials", "updated_at"}
    assert body["ok"] is True
    assert body["stores"]["yakima"] == {
        "hours": "8 AM–11:30 PM daily",
        "address": "1315 N 1st St",
        "phone": "(509) 571-1106",
    }
    assert body["global"] == {"payment": "cash, debit", "age": "21+"}
    assert body["specials"] == {"yakima": ["20% off flower"]}
    assert body["updated_at"]  # iso8601 string, non-empty


@pytest.mark.django_db
def test_store_facts_omits_unconfirmed_row(client, settings):
    from kb.models import StoreFact

    settings.HHT_BACKEND_TOKEN = "test-token"
    StoreFact.objects.create(
        store="mount-vernon", kind="hours", label="Mount Vernon hours", value="9 AM–10 PM daily",
        confirmed=False,
    )

    resp = client.get("/api/voice/store-facts", **{"HTTP_AUTHORIZATION": "Bearer test-token"})

    body = resp.json()
    assert body["stores"] == {}


@pytest.mark.django_db
def test_store_facts_omits_expired_special(client, settings):
    from kb.models import StoreFact

    settings.HHT_BACKEND_TOKEN = "test-token"
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    StoreFact.objects.create(
        store="yakima", kind="special", label="Expired special", value="30% off (expired)",
        valid_to=yesterday,
    )
    StoreFact.objects.create(
        store="yakima", kind="special", label="Current special", value="20% off flower",
    )

    resp = client.get("/api/voice/store-facts", **{"HTTP_AUTHORIZATION": "Bearer test-token"})

    body = resp.json()
    assert body["specials"] == {"yakima": ["20% off flower"]}
