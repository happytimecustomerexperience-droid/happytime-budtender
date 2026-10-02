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


FULL_SCAN = {
    "first_name": "Jane", "middle_name": "Q", "last_name": "Doe",
    "phone": "5095550100", "birth_date": "1990-01-15", "id_number": "DL-7",
    "mjstateidno": "MJ-9", "id_expiration": "2030-01-15", "id_type": "driver_license", "gender": "female",
    "address": "123 Main", "address2": "Unit 4", "city": "Yakima", "state": "WA",
    "postal_code": "98901-1234", "email": "jane@example.test", "over_21": True,
    "ocr_text": "@ANSI 636000 DAQ DL-7 DAC JANE DBB 19900115",
}


@pytest.mark.django_db
def test_a_resolved_customer_keeps_age_and_expiry_but_not_the_id_document():
    customer = upsert_customer(dict(FULL_SCAN), dutchie_acct_id=7)

    customer.refresh_from_db()
    # what the code reads later survives: names, phone, contact, city/state, over_21, ID expiry
    assert (customer.first_name, customer.middle_name, customer.last_name) == ("Jane", "Q", "Doe")
    assert customer.phone == "5095550100" and customer.email == "jane@example.test"
    assert (customer.city, customer.state) == ("Yakima", "WA")
    assert customer.over_21 is True and customer.id_expiration.isoformat() == "2030-01-15"
    # the identity document, home address, DOB and verbatim scan do not
    assert customer.raw_scan == {}
    assert customer.birth_date is None
    assert (customer.id_number, customer.mjstateidno) == ("", "")
    assert (customer.address, customer.address2, customer.postal_code) == ("", "", "")


@pytest.mark.django_db
def test_a_pending_scan_is_kept_for_the_pos_create_step_then_dropped_when_the_account_exists():
    pending = upsert_customer(dict(FULL_SCAN))          # scanned, no Dutchie account yet
    pending.refresh_from_db()
    assert pending.raw_scan["id_number"] == "DL-7" and pending.raw_scan["address"] == "123 Main"
    assert pending.birth_date.isoformat() == "1990-01-15"

    resolved = upsert_customer({**pending.raw_scan, "phone": "5095550100"}, dutchie_acct_id=11)

    resolved.refresh_from_db()
    assert resolved.pk == pending.pk
    assert resolved.raw_scan == {} and resolved.id_number == "" and resolved.birth_date is None
    assert resolved.over_21 is True


@pytest.mark.django_db
def test_resolving_a_legacy_row_clears_what_an_older_version_stored():
    legacy = Customer.objects.create(
        first_name="Old", phone="5095550111", dutchie_acct_id=5, id_number="OLD-1", address="9 Elm",
        birth_date="1985-05-05", raw_scan={"id_number": "OLD-1", "ocr_text": "payload"},
    )

    upsert_customer({"phone": "5095550111"}, dutchie_acct_id=5)

    legacy.refresh_from_db()
    assert legacy.id_number == "" and legacy.address == "" and legacy.raw_scan == {}
    assert legacy.birth_date is None and legacy.first_name == "Old"


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
