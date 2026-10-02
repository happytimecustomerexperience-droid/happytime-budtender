"""Per-store warm transfer: the transferCall tool has one destination per store (described by its
store name so Vapi's model can pick), an unset number degrades per store, the end-of-call record
names the store whose number was actually dialled, and saving a transfer number republishes the two
assistants that carry it. Offline: the Vapi publish is mocked."""

from __future__ import annotations

import json

import pytest
from django.urls import reverse

from voice import capabilities as caps
from voice import constants as C
from voice import provision, signing

YAKIMA = "+15095711106"
MTVERNON = "+13604882923"
PULLMAN = "+15093342788"
SECRET = "test-webhook-secret-0123456789"


@pytest.fixture
def numbers(settings):
    settings.HHT_TRANSFER_NUMBER_YAKIMA = YAKIMA
    settings.HHT_TRANSFER_NUMBER_MTVERNON = MTVERNON
    settings.HHT_TRANSFER_NUMBER_PULLMAN = PULLMAN


# ── the tool: three destinations, one per store ──────────────────────────────────────────────────


def test_transfer_tool_has_one_described_destination_per_store(numbers):
    warnings: list[str] = []
    tool = provision._transfer_tool(warnings)

    assert tool["type"] == "transferCall"
    dests = tool["destinations"]
    assert [d["number"] for d in dests] == [YAKIMA, MTVERNON, PULLMAN]
    assert warnings == []
    stores = ["Yakima", "Mount Vernon", "Pullman"]
    for dest, own in zip(dests, stores, strict=True):
        assert own in dest["description"]  # the model picks the destination by this text
        assert all(other not in dest["description"] for other in stores if other != own)
        assert dest["transferPlan"]["mode"] == "warm-transfer-say-summary"
        assert dest["transferPlan"]["summaryPlan"]["enabled"] is True
        assert "{{transcript}}" in dest["transferPlan"]["summaryPlan"]["messages"][0]["content"]
    assert dests[0]["transferPlan"] is not dests[1]["transferPlan"]  # no shared mutable plan


def test_an_unset_number_gets_the_placeholder_and_a_warning_for_that_store_only(numbers, settings):
    settings.HHT_TRANSFER_NUMBER_PULLMAN = ""
    warnings: list[str] = []
    dests = provision._transfer_tool(warnings)["destinations"]

    assert [d["number"] for d in dests] == [YAKIMA, MTVERNON, C.TRANSFER_NUMBER_PLACEHOLDER]
    assert warnings == ["transfer number not configured for PULLMAN (using placeholder)"]


@pytest.mark.django_db
def test_a_changed_number_changes_the_publish_hash(numbers, settings):
    """The zero-drift hash must see a new number, or the republish would be skipped as 'no change'."""
    from kb import seed

    seed.seed_agent_prompts()
    before, _ = provision.build_assistant_payload("escalation")
    settings.HHT_TRANSFER_NUMBER_MTVERNON = "+13605550100"
    after, _ = provision.build_assistant_payload("escalation")
    assert provision._payload_hash(before) != provision._payload_hash(after)


@pytest.mark.django_db
def test_transfer_roles_are_told_to_pick_the_callers_store(numbers):
    from kb import seed

    seed.seed_agent_prompts()
    line = provision._TRANSFER_STORE_LINE.strip()
    assert "ask which store" in line

    def system(role):
        return provision.build_assistant_payload(role)[0]["model"]["messages"][0]["content"]

    assert line in system("escalation")
    assert line in system("vendor")
    assert line not in system("faq")  # no transferCall tool, no instruction about it

    caps.set_enabled("call.transfer", False)  # transfers off: no promise to pick a store either
    assert line not in system("escalation")
    assert provision._NO_TRANSFER_LINE.strip() in system("escalation")


# ── the end-of-call record names the store that was actually dialled ─────────────────────────────


def _post(client, payload):
    raw = json.dumps(payload).encode()
    return client.post(
        "/api/voice/vapi",
        data=raw,
        content_type="application/json",
        HTTP_X_VAPI_SIGNATURE=signing.compute_signature(raw, SECRET),
    )


def _eocr(call_id, destination):
    message = {
        "type": "end-of-call-report",
        "call": {"id": call_id, "customer": {"number": "+15095551212"}},
        "endedReason": "assistant-forwarded-call",
        "transcript": "User: I want to talk to the Mount Vernon store.",
        "messages": [],
    }
    if destination is not None:
        message["destination"] = {"type": "number", "number": destination}
    return {"message": message}


@pytest.fixture
def webhook(settings, numbers):
    settings.VAPI_WEBHOOK_SECRET = SECRET
    settings.VAPI_SIGNATURE_HEADER = "X-Vapi-Signature"
    settings.VAPI_SECRET_HEADER = "X-Vapi-Secret"
    settings.HHT_DEFAULT_STORE = "yakima"  # the CALLER's store: yakima


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("destination", "label"),
    [
        (MTVERNON, "MTVERNON"),  # caller rang Yakima, was sent to Mount Vernon: the label says so
        ("(509) 334-2788", "PULLMAN"),  # a differently formatted number is still the same number
        (None, ""),  # nothing was dialled: no label, not the caller's store
        ("+15095550000", ""),  # a number that is no store's: no label, never a guess
    ],
)
def test_end_of_call_records_the_dialled_store(client, webhook, destination, label):
    from voice.models import VoiceCall

    assert _post(client, _eocr("call-label-1", destination)).status_code == 200
    assert VoiceCall.objects.get(call_id="call-label-1").transfer_number_key == label


# ── saving a transfer number republishes the assistants that carry it ────────────────────────────


@pytest.fixture
def staff_client(client, django_user_model, monkeypatch):
    # set_credential writes os.environ as well as settings; have monkeypatch undo the env writes.
    for name in ("HHT_TRANSFER_NUMBER_YAKIMA", "HHT_TRANSFER_NUMBER_MTVERNON", "HHT_TRANSFER_NUMBER_PULLMAN",
                 "SLACK_WEBHOOK_URL"):
        monkeypatch.setenv(name, "")
    client.force_login(
        django_user_model.objects.create_user("owner", password="x", is_staff=True, is_superuser=True)
    )
    return client


@pytest.fixture
def prompts(db):
    from kb.models import AgentPrompt

    return {r: AgentPrompt.objects.create(role=r, body=r, is_active=True) for r in ("escalation", "vendor", "faq")}


@pytest.fixture
def published(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "dashboard.publish.auto_publish_on_save", lambda p: calls.append(p.role) or "assistant patched"
    )
    return calls


def _save(client, name, value):
    return client.post(reverse("dash-credentials-save"), {"name": name, "value": value})


@pytest.mark.django_db
def test_saving_a_transfer_number_republishes_escalation_and_vendor_only(
    staff_client, prompts, published, numbers
):
    resp = _save(staff_client, "HHT_TRANSFER_NUMBER_MTVERNON", "+13605550100")

    assert resp.status_code == 200
    assert sorted(published) == ["escalation", "vendor"]  # faq carries no transfer tool
    assert "assistant patched" in resp["HX-Trigger"]  # the toast says what the publish did


@pytest.mark.django_db
def test_an_unchanged_or_unrelated_credential_republishes_nothing(
    staff_client, prompts, published, numbers
):
    _save(staff_client, "HHT_TRANSFER_NUMBER_YAKIMA", YAKIMA)  # same value as already set
    _save(staff_client, "SLACK_WEBHOOK_URL", "https://hooks.example.com/x")  # not a transfer number
    _save(staff_client, "HHT_TRANSFER_NUMBER_PULLMAN", "")  # blank = keep existing, not a change
    assert published == []


@pytest.mark.django_db
def test_the_toast_never_claims_live_when_auto_publish_is_off(staff_client, prompts, numbers):
    """Auto-publish is off under pytest, so the number is saved but the phone still has the old one;
    the toast must say that, not 'live now'."""
    resp = _save(staff_client, "HHT_TRANSFER_NUMBER_PULLMAN", "+15093340000")
    toast = json.loads(resp["HX-Trigger"])["toast"]["message"]
    assert "not published" in toast and "live now" not in toast


@pytest.mark.django_db
def test_the_agent_editor_no_longer_offers_a_per_agent_transfer_key(staff_client, prompts):
    for url in (reverse("dash-agents"), reverse("dash-flow"), reverse("dash-agent-detail", args=["escalation"])):
        assert "transfer_number_key" not in staff_client.get(url).content.decode()
