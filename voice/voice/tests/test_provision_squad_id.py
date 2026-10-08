"""provision_vapi honours VAPI_SQUAD_ID (the owner's live squad): ADOPT it, never create a second.

Before this, the squad was found only by its VapiObject record or by the name "Happy Time Voice";
a live squad with another name and no record made a run POST a second squad and move the phone
number to it. Now: no record → adopt the id (PATCH only); a record with a DIFFERENT id → refuse;
no VAPI_SQUAD_ID → the old behaviour, unchanged. Vapi HTTP MOCKED (``FakeAccount``); offline.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from core.services import vapi
from voice import provision
from voice.models import VapiObject
from voice.tests.test_provision import FakeAccount, faq_prompt  # noqa: F401  (fixture)

LIVE = "2b132e78-6b37-4b12-b99a-17d23f8906e7"  # the owner's squad, named something else in Vapi
OTHER = "9f00aa11-2222-4333-8444-55556666beef"
NUMBER = "pn_main_0001"


@pytest.fixture
def acct(monkeypatch, settings):
    a = FakeAccount()
    a.squads[LIVE] = {"id": LIVE, "name": "Owner's live squad", "members": []}
    a.number_patches = []
    monkeypatch.setattr(vapi, "configured", lambda: True)
    monkeypatch.setattr(vapi, "auth_ok", lambda: {"ok": True, "configured": True, "error": ""})
    for name in (
        "find_tool_by_name", "get_tool", "create_tool", "patch_tool",
        "find_assistant_by_name", "get_assistant", "create_assistant", "patch_assistant",
        "find_squad_by_name", "create_squad", "patch_squad",
    ):
        monkeypatch.setattr(vapi, name, getattr(a, name))

    def get_squad(_id):
        if _id not in a.squads:
            raise vapi.VapiError("not found", status=404)
        return a.squads[_id]

    monkeypatch.setattr(vapi, "get_squad", get_squad)
    monkeypatch.setattr(vapi, "find_phone_number", lambda n: {"id": n})
    monkeypatch.setattr(vapi, "get_phone_number", lambda n: {"id": n})

    def patch_phone_number(n, body):
        a.number_patches.append(body)
        return {"id": n, **body}

    monkeypatch.setattr(vapi, "patch_phone_number", patch_phone_number)
    from kb import vapi_files

    monkeypatch.setattr(vapi_files, "mirror_all", lambda: {"skipped": "not configured"})
    settings.VAPI_PHONE_NUMBER_ID = NUMBER
    settings.VAPI_SQUAD_ID = ""
    return a


def _squad_result(report):
    return next(r for r in report.results if r.kind == "squad")


@pytest.mark.django_db
def test_adopts_vapi_squad_id_and_never_posts_a_second_squad(acct, faq_prompt, settings):  # noqa: F811
    settings.VAPI_SQUAD_ID = LIVE
    report = provision.provision_all(dry_run=False)
    assert report.ok, report.results
    squad = _squad_result(report)
    assert (squad.action, squad.vapi_id) == ("patched", LIVE)
    assert "adopt squad ...06e7" in squad.line()
    assert list(acct.squads) == [LIVE], "no second squad was created"
    assert acct.squads[LIVE]["name"] == provision.squad_name()  # PATCHed with our payload
    assert VapiObject.objects.get(kind="squad").vapi_id == LIVE  # recorded for the next run
    assert acct.number_patches[-1]["squadId"] == LIVE  # the number stays on the owner's squad

    # The next run reconciles the adopted record: zero drift, still no POST.
    creates = acct.creates
    again = provision.provision_all(dry_run=False)
    assert _squad_result(again).action == "nodrift" and not _squad_result(again).note
    assert acct.creates == creates and list(acct.squads) == [LIVE]


@pytest.mark.django_db
def test_mismatched_record_refuses_before_any_write(acct, faq_prompt, settings):  # noqa: F811
    VapiObject.objects.create(kind="squad", name=provision.squad_name(), vapi_id=OTHER)
    settings.VAPI_SQUAD_ID = LIVE
    report = provision.provision_all(dry_run=False)
    assert not report.ok and LIVE in report.error and OTHER in report.error
    assert "--force-squad-id" in report.error and f"VAPI_SQUAD_ID={OTHER}" in report.error
    assert report.results == [] and acct.creates == 0 and acct.patches == 0
    assert acct.number_patches == []
    assert VapiObject.objects.get(kind="squad").vapi_id == OTHER  # untouched

    # The command stops with that message (dry run too).
    with pytest.raises(CommandError, match="disagrees with the squad provision_vapi recorded"):
        call_command("provision_vapi", "--dry-run", stdout=StringIO())
    vapi.set_dry_run(False)

    # Direct callers get the same refusal.
    assert provision.ensure_squad({"faq": "asst_x"}).action == "error"
    assert provision.ensure_phone_number().action == "error"


@pytest.mark.django_db
def test_force_squad_id_rerecords_and_patches_only_vapi_squad_id(acct, faq_prompt, settings):  # noqa: F811
    acct.squads[OTHER] = {"id": OTHER, "name": provision.squad_name(), "members": []}
    VapiObject.objects.create(kind="squad", name=provision.squad_name(), vapi_id=OTHER)
    settings.VAPI_SQUAD_ID = LIVE
    report = provision.provision_all(dry_run=False, force_squad_id=True)
    assert report.ok
    assert _squad_result(report).vapi_id == LIVE
    assert VapiObject.objects.get(kind="squad").vapi_id == LIVE
    assert acct.squads[OTHER]["members"] == []  # the other squad was not touched
    assert sorted(acct.squads) == sorted([LIVE, OTHER])


@pytest.mark.django_db
def test_unknown_vapi_squad_id_is_an_error_not_a_create(acct, faq_prompt, settings):  # noqa: F811
    settings.VAPI_SQUAD_ID = "00000000-0000-0000-0000-00000000dead"
    report = provision.provision_all(dry_run=False)
    squad = _squad_result(report)
    assert squad.action == "error" and "refusing to create a second squad" in squad.error
    assert list(acct.squads) == [LIVE]
    assert not VapiObject.objects.filter(kind="squad").exists()


@pytest.mark.django_db
def test_dry_run_shows_adopt_and_records_nothing(acct, faq_prompt, settings):  # noqa: F811
    # An older dry run left a synthetic id behind; it is not a real record, so it is not a mismatch.
    VapiObject.objects.create(kind="squad", name=provision.squad_name(), vapi_id="dryrun-squad")
    settings.VAPI_SQUAD_ID = LIVE
    out = StringIO()
    call_command("provision_vapi", "--dry-run", stdout=out)
    vapi.set_dry_run(False)
    text = out.getvalue()
    assert "adopt squad ...06e7 from VAPI_SQUAD_ID, PATCH only" in text
    assert f"# PATCH /squad/{LIVE}" in text and "POST/PATCH /squad" not in text
    assert {"method": "POST", "path": "/squad"} not in [
        {"method": c["method"], "path": c["path"]} for c in vapi.recorded_calls
    ]
    assert VapiObject.objects.get(kind="squad").vapi_id == "dryrun-squad"  # dry run wrote nothing


@pytest.mark.django_db
def test_no_vapi_squad_id_keeps_todays_behaviour(acct, faq_prompt, monkeypatch):  # noqa: F811
    """Unset → found by record, else by name, else CREATED (the documented old behaviour): the
    owner's differently named squad is not looked up by id at all."""
    looked_up = []
    real_get = vapi.get_squad
    monkeypatch.setattr(vapi, "get_squad", lambda i: looked_up.append(i) or real_get(i))
    report = provision.provision_all(dry_run=False)
    squad = _squad_result(report)
    assert squad.action == "created" and squad.vapi_id != LIVE and not squad.note
    assert squad.line() == f"  {'squad':<13} {provision.squad_name():<26} {'created':<8} {squad.vapi_id}"
    assert "note" not in report.to_dict()["results"][-2]
    assert LIVE not in looked_up
    assert acct.number_patches[-1]["squadId"] == squad.vapi_id
