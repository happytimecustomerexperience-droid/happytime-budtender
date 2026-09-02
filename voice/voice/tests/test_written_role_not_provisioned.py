"""tests/test_written_role_not_provisioned.py — the "written" AgentPrompt role (website chat) is
NOT a squad member: provision.py's EXTRA_MEMBER_ROLES and dashboard/publish.py's MEMBER_ROLES both
omit it, so no Vapi assistant is ever created or published for it, even when a "written" row exists.
"""

from __future__ import annotations

import pytest

from core.services import vapi
from voice import provision
from voice.tests.test_provision import FakeAccount


@pytest.fixture
def fake_vapi(monkeypatch):
    acct = FakeAccount()
    monkeypatch.setattr(vapi, "configured", lambda: True)
    monkeypatch.setattr(vapi, "auth_ok", lambda: {"ok": True, "configured": True, "error": ""})
    for name in (
        "find_tool_by_name",
        "get_tool",
        "create_tool",
        "patch_tool",
        "find_assistant_by_name",
        "get_assistant",
        "create_assistant",
        "patch_assistant",
        "find_squad_by_name",
        "get_squad",
        "create_squad",
        "patch_squad",
    ):
        monkeypatch.setattr(vapi, name, getattr(acct, name))
    monkeypatch.setattr(vapi, "find_phone_number", lambda _x: None)
    from kb import vapi_files

    monkeypatch.setattr(vapi_files, "mirror_all", lambda: {"skipped": "not configured"})
    return acct


def test_written_not_in_member_role_lists():
    assert "written" not in provision.EXTRA_MEMBER_ROLES
    from dashboard.publish import MEMBER_ROLES

    assert "written" not in MEMBER_ROLES


@pytest.mark.django_db
def test_provision_all_never_creates_an_assistant_for_written(fake_vapi):
    from kb.models import AgentPrompt

    AgentPrompt.objects.create(role="faq", body="faq body", is_active=True)
    AgentPrompt.objects.create(
        role="written", body="written channel body", is_active=True
    )

    report = provision.provision_all(dry_run=False)

    assert report.ok
    assert not any(r.kind == "assistant" and r.name == "written" for r in report.results)


@pytest.mark.django_db
def test_publish_never_touches_written(fake_vapi):
    from dashboard import publish
    from kb.models import AgentPrompt

    AgentPrompt.objects.create(role="faq", body="faq body", is_active=True)
    written = AgentPrompt.objects.create(
        role="written", body="written channel body", is_active=True
    )

    results = publish.publish_all()

    assert not any(r.role == "written" for r in results)
    written.refresh_from_db()
    assert written.vapi_assistant_id == ""
