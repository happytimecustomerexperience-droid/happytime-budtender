import pytest

from customers.models import Customer, DutchieWriteAudit
from customers.services import record_write, upsert_customer


@pytest.mark.django_db
def test_upsert_customer_creates_then_updates():
    scan = {"first_name": "Jane", "last_name": "Doe", "phone": "5095551234", "over_21": True}
    c = upsert_customer(scan, dutchie_acct_id=42)
    assert c.pk is not None
    assert c.first_name == "Jane"
    assert c.over_21 is True
    assert Customer.objects.count() == 1

    # Same acct_id -> update existing, fill a previously-blank field.
    c2 = upsert_customer({"email": "jane@example.com"}, dutchie_acct_id=42)
    assert c2.pk == c.pk
    assert c2.email == "jane@example.com"
    assert c2.first_name == "Jane"  # preserved
    assert Customer.objects.count() == 1


@pytest.mark.django_db
def test_upsert_matches_by_phone_when_no_acct():
    upsert_customer({"first_name": "Bob", "phone": "5095559999"})
    again = upsert_customer({"last_name": "Smith", "phone": "5095559999"})
    assert Customer.objects.count() == 1
    assert again.first_name == "Bob"
    assert again.last_name == "Smith"


@pytest.mark.django_db
def test_upsert_dedupes_same_phone_across_dutchie_accounts():
    old = upsert_customer({"first_name": "Jane", "phone": "(509) 555-1212"}, dutchie_acct_id=1)
    Customer.objects.create(first_name="J.", last_name="Doe", phone="1-509-555-1212", dutchie_acct_id=2)

    merged = upsert_customer({"phone": "509.555.1212", "email": "jane@example.com"}, dutchie_acct_id=3)

    assert merged.pk == old.pk
    assert merged.phone == "5095551212"
    assert merged.dutchie_acct_id == 3
    assert merged.last_name == "Doe"
    assert merged.email == "jane@example.com"
    assert Customer.objects.count() == 1


@pytest.mark.django_db
def test_record_write_creates_row_and_scrubs_pii():
    a = record_write(
        store="Yakima", action="submit", ok=True, acct_id=42, shipment_id=7,
        summary="checkout for dob 1990-01-15", username="op1",
    )
    assert DutchieWriteAudit.objects.count() == 1
    assert a.ok is True
    assert a.acct_id == 42
    assert "1990-01-15" not in a.summary
    assert "[redacted]" in a.summary
