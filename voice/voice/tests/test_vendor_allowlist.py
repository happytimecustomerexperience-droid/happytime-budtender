"""Vendor allowlist: an allowlisted vendor's call rings the owner with no AI; everyone else, and
every failure, gets exactly today's answer (voice/vendor_allowlist.py, voice/webhooks.py)."""

from __future__ import annotations

import json
import logging

import pytest
from django.http import JsonResponse

from voice import capabilities, signing
from voice import vendor_allowlist as va

WEBHOOK_URL = "/api/voice/vapi"
SECRET = "test-webhook-secret-0123456789"
OWNER = "+15095550199"
VENDOR = "+15095557001"


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.VAPI_WEBHOOK_SECRET = SECRET
    settings.VAPI_SIGNATURE_HEADER = "X-Vapi-Signature"
    settings.HHT_DEFAULT_STORE = "yakima"
    settings.HHT_DYNAMIC_GREETING = False
    settings.VAPI_PHONE_NUMBER_STORE_MAP = ""
    settings.HHT_TRANSFER_NUMBER_YAKIMA = ""
    settings.HHT_OWNER_PHONE = OWNER
    # These pin the direct no-AI forward, kept behind HHT_TRANSFER_CONSULT=0; the consult-first
    # route (the default) is covered in test_consult_transfer.py.
    settings.HHT_TRANSFER_CONSULT = False


@pytest.fixture
def faq_rows(db):
    """The rows test_voice.py::test_assistant_request_returns_config answers from."""
    from kb.models import AgentPrompt, StoreFact

    AgentPrompt.objects.create(role="faq", body="persona", vapi_assistant_id="asst_test_123", is_active=True)
    StoreFact.objects.create(
        store="yakima", kind="hours", label="Yakima hours", value="9 AM–11 PM daily", confirmed=True
    )


# The answer today's code gives for that request (test_voice.py's expected shape, key order as built).
BASELINE = JsonResponse(
    {
        "assistantOverrides": {
            "variableValues": {
                "store_name": "Happy Time Yakima",
                "store_hours": "9 AM–11 PM daily",
                "transfer_number": "",
            }
        },
        "assistantId": "asst_test_123",
    }
).content


@pytest.fixture
def entry(db):
    from dashboard.models import VendorAllowlistEntry

    return VendorAllowlistEntry.objects.create(name="Cascade Crest", phone=VENDOR)


def _post(client, number=VENDOR, call_id="call-va-1"):
    call = {"id": call_id}
    if number is not None:
        call["customer"] = {"number": number}
    raw = json.dumps({"message": {"type": "assistant-request", "call": call}}).encode()
    sig = signing.compute_signature(raw, SECRET)
    return client.post(WEBHOOK_URL, data=raw, content_type="application/json", HTTP_X_VAPI_SIGNATURE=sig)


# ── normalisation ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    ["+15095557001", "15095557001", "5095557001", "(509) 555-7001", "509.555.7001", " +1 509 555 7001 "],
)
def test_us_numbers_normalise_to_e164(raw):
    assert va.normalize_us_e164(raw) == VENDOR


@pytest.mark.parametrize(
    "raw",
    [
        "", None, "   ", "anonymous", "Anonymous", "restricted", "unknown", "private", "+266696687",
        "555-7001", "509555700", "+509555700123", "+445095557001", "+5095557001", "25095557001",
        "0095557001", "1095557001", "5090557001", "5091557001", "+1+5095557001", "509-555-7001x2",
        "sip:vendor@example.com", "+1 509 555 7001 ext 9", "1" * 40,
    ],
)
def test_junk_withheld_short_and_foreign_caller_ids_are_not_numbers(raw):
    assert va.normalize_us_e164(raw) == ""


# ── the matcher ───────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_exact_match_only(entry):
    assert va.evaluate("(509) 555-7001").route is True
    for near in ("+15095557002", "+1509555700", "+150955570011", "+15095557000"):
        assert va.evaluate(near).route is False, near


@pytest.mark.django_db
def test_inactive_switch_off_or_no_owner_is_not_routed(entry, settings):
    entry.active = False
    entry.save()
    assert va.evaluate(VENDOR).route is False
    entry.active = True
    entry.save()
    capabilities.set_enabled("call.vendor_allowlist", False)
    assert va.evaluate(VENDOR).route is False
    capabilities.set_enabled("call.vendor_allowlist", True)
    settings.HHT_OWNER_PHONE = "not a number"
    assert va.evaluate(VENDOR).route is False
    settings.HHT_OWNER_PHONE = OWNER
    assert va.evaluate(VENDOR).route is True


def test_switch_is_declared_and_on_by_default():
    assert capabilities.BY_KEY["call.vendor_allowlist"].default is True


# ── the webhook answer ────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_allowlisted_caller_gets_a_destination_and_no_assistant(client, faq_rows, entry, caplog):
    from voice.models import Outcome, VoiceCall

    caplog.set_level(logging.DEBUG)
    resp = _post(client)
    assert resp.status_code == 200
    assert resp.json() == {"destination": {"type": "number", "number": OWNER, "message": va.TRANSFER_MESSAGE}}
    assert b"assistant" not in resp.content and b"squad" not in resp.content

    vc = VoiceCall.objects.get(call_id="call-va-1")
    assert vc.outcome == Outcome.VENDOR_DIRECT and vc.reason == "vendor" and vc.store == "yakima"
    assert vc.caller_phone_hash and "5557001" not in vc.caller_phone_hash
    entry.refresh_from_db()
    assert entry.match_count == 1 and entry.last_matched_at is not None
    assert "5557001" not in caplog.text and "5550199" not in caplog.text


@pytest.mark.django_db
def test_allowlisted_caller_is_routed_with_the_dynamic_greeting_on_too(client, faq_rows, entry, settings):
    settings.HHT_DYNAMIC_GREETING = True
    assert set(_post(client).json()) == {"destination"}


@pytest.mark.django_db
def test_end_of_call_report_keeps_the_vendor_direct_label(client, faq_rows, entry):
    from voice.models import VoiceCall

    _post(client)
    raw = json.dumps(
        {"message": {"type": "end-of-call-report", "call": {"id": "call-va-1", "customer": {"number": VENDOR}},
                     "endedReason": "assistant-forwarded-call", "destination": {"type": "number", "number": OWNER}}}
    ).encode()
    resp = client.post(WEBHOOK_URL, data=raw, content_type="application/json",
                       HTTP_X_VAPI_SIGNATURE=signing.compute_signature(raw, SECRET))
    assert resp.status_code == 200
    vc = VoiceCall.objects.get(call_id="call-va-1")
    assert (vc.outcome, vc.reason) == ("vendor_direct", "vendor")


@pytest.mark.django_db
def test_no_match_answers_byte_identically_to_before(client, faq_rows, entry):
    from voice.models import VoiceCall

    resp = _post(client, number="+15095551212")
    assert resp.content == BASELINE
    assert not VoiceCall.objects.filter(call_id="call-va-1").exists()
    entry.refresh_from_db()
    assert entry.match_count == 0


@pytest.mark.django_db
@pytest.mark.parametrize("number", [None, "", "anonymous", "restricted", "+445095557001", "5557001"])
def test_withheld_or_junk_caller_id_answers_byte_identically(client, faq_rows, entry, number):
    assert _post(client, number=number).content == BASELINE


@pytest.mark.django_db
def test_switch_off_answers_byte_identically(client, faq_rows, entry):
    capabilities.set_enabled("call.vendor_allowlist", False)
    assert _post(client).content == BASELINE
    entry.refresh_from_db()
    assert entry.match_count == 0


@pytest.mark.django_db
@pytest.mark.parametrize("owner", ["", "+44 20 7946 0000", "12345"])
def test_no_or_bad_owner_number_answers_byte_identically(client, faq_rows, entry, settings, owner):
    settings.HHT_OWNER_PHONE = owner
    assert _post(client).content == BASELINE


@pytest.mark.django_db
def test_inactive_entry_answers_byte_identically(client, faq_rows, entry):
    entry.active = False
    entry.save()
    assert _post(client).content == BASELINE


@pytest.mark.django_db
def test_any_error_answers_byte_identically_and_logs_no_number(client, faq_rows, entry, monkeypatch, caplog):
    def boom(_number):
        raise RuntimeError("db down")

    monkeypatch.setattr(va, "evaluate", boom)
    caplog.set_level(logging.DEBUG)
    assert _post(client).content == BASELINE
    assert "vendor allowlist check failed" in caplog.text
    assert "5557001" not in caplog.text and "5550199" not in caplog.text


@pytest.mark.django_db
def test_a_failed_call_row_write_still_routes_the_vendor(client, faq_rows, entry, monkeypatch):
    from voice.models import VoiceCall

    def boom(*a, **k):
        raise RuntimeError("write failed")

    monkeypatch.setattr(VoiceCall.objects, "update_or_create", boom)
    assert set(_post(client).json()) == {"destination"}


@pytest.mark.django_db
def test_dynamic_greeting_squad_unchanged_for_a_non_vendor(client, entry, settings):
    """With the dynamic greeting on, a non-allowlisted caller gets the same squad whether or not
    the allowlist has entries."""
    from dashboard.models import VendorAllowlistEntry
    from kb.models import AgentPrompt

    settings.HHT_DYNAMIC_GREETING = True
    capabilities.set_enabled("call.recognize_caller", False)  # no budtender lookup: stays offline
    for role, asst in (("entry_router", "a1"), ("budtender", "a2"), ("faq", "a3"), ("vendor", "a4"), ("escalation", "a5")):
        AgentPrompt.objects.create(role=role, body=f"{role} body", vapi_assistant_id=asst, is_active=True)
    with_entries = _post(client, number="+15095551212", call_id="c-a").content
    VendorAllowlistEntry.objects.all().delete()
    without = _post(client, number="+15095551212", call_id="c-a").content
    assert with_entries == without and b'"squad"' in without


@pytest.mark.django_db
def test_routing_status_needs_the_dynamic_greeting(entry, settings):
    assert va.routing_status()["active"] is False  # HHT_DYNAMIC_GREETING off in this module
    settings.HHT_DYNAMIC_GREETING = True
    assert va.routing_status()["active"] is True
