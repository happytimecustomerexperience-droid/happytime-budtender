from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from budtender.models import PhoneCartDraft
from customers.models import Customer

pytestmark = pytest.mark.django_db

DAY = timedelta(days=1)


def _draft(**kw):
    data = dict(
        location_slug="yakima", source=PhoneCartDraft.Source.ONLINE, status=PhoneCartDraft.Status.RELEASED,
        contact_phone="5095550100", contact_email="jane@example.test", pickup_name="Jane Doe",
        customer_name="Jane D", phone_hash="h" * 64, phone_last4="0100",
        lines=[{"sku": "SKU-1"}], quote={"total": 20.0},
    )
    data.update(kw)
    return PhoneCartDraft.objects.create(**data)


def _run(*args):
    out = StringIO()
    call_command("purge_pii", *args, stdout=out)
    return out.getvalue()


def _contacts(d):
    d.refresh_from_db()
    return (d.contact_phone, d.contact_email, d.pickup_name, d.customer_name)


def test_blanks_contact_details_on_drafts_expired_or_claimed_more_than_30_days_ago():
    now = timezone.now()
    expired = _draft(expires_at=now - 31 * DAY)
    status_expired = _draft(status=PhoneCartDraft.Status.EXPIRED)
    old_claim = _draft(status=PhoneCartDraft.Status.CLAIMED, claimed_at=now - 40 * DAY)

    out = _run()

    for d in (expired, status_expired, old_claim):
        assert _contacts(d) == ("", "", "", ""), d.pk
    assert "3 phone-cart draft(s)" in out


def test_leaves_live_and_recent_drafts_alone():
    now = timezone.now()
    live = _draft(expires_at=now + DAY)
    just_expired = _draft(expires_at=now - 5 * DAY)       # the POS queue still lists these
    open_cart = _draft(status=PhoneCartDraft.Status.OPEN, expires_at=now + 3 * DAY)
    recent_claim = _draft(status=PhoneCartDraft.Status.CLAIMED, claimed_at=now - 10 * DAY)

    _run()

    for d in (live, just_expired, open_cart, recent_claim):
        assert _contacts(d) == ("5095550100", "jane@example.test", "Jane Doe", "Jane D"), d.pk


def test_keeps_the_cart_itself_when_it_blanks_contact_details():
    d = _draft(expires_at=timezone.now() - 31 * DAY)

    _run()

    d.refresh_from_db()
    assert d.lines == [{"sku": "SKU-1"}] and d.quote == {"total": 20.0}
    assert d.location_slug == "yakima" and d.status == PhoneCartDraft.Status.RELEASED


def test_dry_run_prints_counts_and_writes_nothing():
    d = _draft(expires_at=timezone.now() - 31 * DAY)
    c = Customer.objects.create(first_name="Old", id_number="OLD-1", raw_scan={"x": 1})
    Customer.objects.filter(pk=c.pk).update(updated_at=timezone.now() - 3 * DAY)

    out = _run("--dry-run")

    assert "Would blank contact details on 1 phone-cart draft(s)" in out
    assert "Would blank ID documents on 1 cached customer(s)" in out
    assert _contacts(d) == ("5095550100", "jane@example.test", "Jane Doe", "Jane D")
    c.refresh_from_db()
    assert c.id_number == "OLD-1" and c.raw_scan == {"x": 1}


def test_days_flag_moves_the_cutoff():
    d = _draft(status=PhoneCartDraft.Status.CLAIMED, claimed_at=timezone.now() - 10 * DAY)

    _run("--days", "7")

    assert _contacts(d) == ("", "", "", "")


def test_blanks_id_documents_on_customers_that_sat_untouched_but_keeps_what_is_read_later():
    c = Customer.objects.create(
        first_name="Jane", last_name="Doe", phone="5095550100", email="jane@example.test",
        city="Yakima", state="WA", over_21=True, id_expiration="2030-01-15", dutchie_acct_id=7,
        birth_date="1990-01-15", id_number="DL-7", mjstateidno="MJ-9", address="123 Main",
        address2="Unit 4", postal_code="98901", raw_scan={"ocr_text": "payload"},
    )
    Customer.objects.filter(pk=c.pk).update(updated_at=timezone.now() - 3 * DAY)

    out = _run()

    c.refresh_from_db()
    assert c.raw_scan == {} and c.birth_date is None
    assert (c.id_number, c.mjstateidno, c.address, c.address2, c.postal_code) == ("", "", "", "", "")
    assert (c.first_name, c.phone, c.email, c.city, c.state) == ("Jane", "5095550100", "jane@example.test",
                                                                   "Yakima", "WA")
    assert c.over_21 is True and c.id_expiration.isoformat() == "2030-01-15" and c.dutchie_acct_id == 7
    assert "1 cached customer(s)" in out


def test_does_not_touch_a_scan_that_may_be_mid_checkout():
    fresh = Customer.objects.create(first_name="Now", id_number="DL-1", raw_scan={"first_name": "Now"})

    _run()

    fresh.refresh_from_db()
    assert fresh.id_number == "DL-1" and fresh.raw_scan == {"first_name": "Now"}


def test_second_run_finds_nothing_left_to_do():
    _draft(expires_at=timezone.now() - 31 * DAY)
    c = Customer.objects.create(first_name="Old", id_number="OLD-1")
    Customer.objects.filter(pk=c.pk).update(updated_at=timezone.now() - 3 * DAY)
    _run()

    out = _run("--dry-run")

    assert "Would blank contact details on 0 phone-cart draft(s)" in out
    assert "Would blank ID documents on 0 cached customer(s)" in out
