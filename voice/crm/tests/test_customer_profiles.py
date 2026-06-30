"""P6: customer-intelligence import + personalized feed + dashboard browse. Offline; a tiny
synthetic customers.json (NOT the real 13 MB export) drives the import test.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse


# ── suggestion feed ───────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_feed_replenishes_favorites_first():
    from crm import suggestions
    from crm.models import CustomerProfile

    c = CustomerProfile.objects.create(
        customer_key="Jane Doe", name="Jane Doe", orders=12, total_spend=900,
        favorites=[{"product": "Blue Dream 3.5g", "units": 8, "orders": 6}],
        tier_by_category={"Flower": "Middle"},
    )
    feed = suggestions.build_feed(c, baskets_index={"Blue Dream 3.5g": [{"with": "OG Pre-roll", "lift": 3.1}]})
    kinds = [s["kind"] for s in feed]
    assert feed[0]["kind"] == "favorite"  # favorites lead
    assert "pair" in kinds  # basket cross-sell surfaced
    assert feed[0]["confidence"] == "high"  # 12 orders → high confidence


@pytest.mark.django_db
def test_feed_cold_start_for_new_customer():
    from crm import suggestions
    from crm.models import CustomerProfile

    c = CustomerProfile.objects.create(
        customer_key="New Guy", name="New Guy", orders=1,
        top_categories=[{"category": "Edibles", "share": 60}],
    )
    feed = suggestions.build_feed(c)
    assert feed and feed[0]["kind"] == "cold_start"
    assert "Edibles" in feed[0]["title"]


# ── import command ────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_import_customer_profiles(tmp_path):
    from django.core.management import call_command

    from crm.models import CustomerProfile

    src = tmp_path / "customers.json"
    src.write_text(json.dumps({
        "customerProfiles": {
            "Jane Doe": {"Orders": 10, "TotalSpend": 800, "AOV": 80, "Recency": 14,
                          "FirstOrder": "2026-01-01", "LastOrder": "2026-04-01",
                          "Segment": "Loyalist", "PersonaName": "Connoisseur", "MedicalShare": 0.0,
                          "TopCategories": [{"category": "Flower", "share": 70}],
                          "TierByCategory": {"Flower": "Top"}, "TopBrand": "Acme", "TopVendor": "V1"},
            "Bob": {"Orders": 1, "TotalSpend": 40, "AOV": 40, "Recency": 100, "Segment": "Lost"},
        },
        "customerRichDetail": {
            "Jane Doe": {"topSkus": [{"product": "Blue Dream 3.5g", "units": 6}]},
        },
    }), encoding="utf-8")

    call_command("import_customer_profiles", "--customers", str(src))
    assert CustomerProfile.objects.count() == 2
    jane = CustomerProfile.objects.get(customer_key="Jane Doe")
    assert jane.orders == 10
    assert jane.segment == "Loyalist"
    assert jane.favorites[0]["product"] == "Blue Dream 3.5g"
    assert jane.cadence_days and jane.cadence_days > 0  # computed from first→last span

    # Idempotent re-import: no duplicate rows.
    call_command("import_customer_profiles", "--customers", str(src))
    assert CustomerProfile.objects.count() == 2


# ── dashboard browse ──────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_customers_pages_render(client, django_user_model):
    from crm.models import CustomerProfile

    staff = django_user_model.objects.create_user("st", password="x", is_staff=True, is_superuser=True)
    client.force_login(staff)
    c = CustomerProfile.objects.create(customer_key="Jane Doe", name="Jane Doe", orders=5,
                                       total_spend=400, segment="Active",
                                       favorites=[{"product": "X", "units": 3}])

    resp = client.get(reverse("dash-customers"))
    assert resp.status_code == 200
    assert b"Jane Doe" in resp.content

    resp = client.get(reverse("dash-customer-detail", args=[c.pk]))
    assert resp.status_code == 200
    assert b"Personalized feed" in resp.content
