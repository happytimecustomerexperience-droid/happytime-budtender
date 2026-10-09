"""``provision_vapi --dry-run`` writes NOTHING to the database and never stores a ``dryrun-`` id.

A dry run records Vapi writes in memory and answers each with a synthetic ``dryrun-…`` id. Those ids
once reached the DB (``VapiObject`` rows, and from them the assistant ids the live call path builds
squads from). Every write path is covered: tools, the KB-file mirror + Query Tool, assistants, the
squad (pinned and unpinned), the phone number, and ``--per-store``. Vapi HTTP is stubbed (offline).
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from core.services import vapi
from kb.models import AgentPrompt
from voice import constants as C
from voice.models import VapiObject
from voice.tests.test_provision import faq_prompt  # noqa: F401  (fixture)

PINNED = "2b132e78-6b37-4b12-b99a-17d23f8906e7"
NUMBER = "pn_main_0001"


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers = status, body, {}
        self.content = b"x" if body is not None else b""
        self.text = ""

    def json(self):
        return self._body


class _StubClient:
    """A Vapi account with nothing in it: lists are empty, every object id is a 404."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, path, params=None, json=None):
        assert method == "GET", f"a dry run issued a real {method} {path}"
        return _Resp(200, []) if path.count("/") == 1 else _Resp(404, {})


def _rows():
    return (
        sorted(VapiObject.objects.values_list("kind", "name", "vapi_id", "last_provision_hash")),
        sorted(AgentPrompt.objects.values_list("role", "vapi_assistant_id", "body", "is_active")),
    )


def _seed(role_ids: dict[str, str]):
    for role in (C.CONCIERGE_ROLE, "entry_router", "budtender", "escalation", "vendor"):
        AgentPrompt.objects.get_or_create(role=role, defaults={"body": f"{role} prompt", "is_active": True})
    for role, vid in role_ids.items():
        AgentPrompt.objects.filter(role=role).update(vapi_assistant_id=vid)


@pytest.fixture
def dry(settings, monkeypatch):
    settings.VAPI_PHONE_NUMBER_ID = NUMBER
    settings.VAPI_SQUAD_ID = ""
    yield
    vapi.set_dry_run(False)
    vapi.recorded_calls.clear()


def _run(*args):
    vapi.recorded_calls.clear()
    try:
        call_command("provision_vapi", "--dry-run", *args, stdout=StringIO())
    except CommandError:  # a reported object error is not what this file is about; the rows are
        pass
    vapi.set_dry_run(False)


@pytest.mark.django_db
@pytest.mark.parametrize("pinned", ["", PINNED])
@pytest.mark.parametrize("seeded", [False, True])
@pytest.mark.parametrize("keyed", [False, True])
def test_a_dry_run_changes_no_row_and_stores_no_synthetic_id(dry, faq_prompt, settings, monkeypatch, keyed, seeded, pinned):  # noqa: F811
    settings.VAPI_SQUAD_ID = pinned
    if keyed:  # a live key: reads go out (stubbed to an empty account), writes are only recorded
        monkeypatch.setattr(vapi, "configured", lambda: True)
        monkeypatch.setattr(vapi, "auth_ok", lambda: {"ok": True, "configured": True, "error": ""})
        monkeypatch.setattr(vapi, "_client", lambda: _StubClient())
    else:
        monkeypatch.setattr(vapi, "configured", lambda: False)
    _seed({"faq": "real-faq-id"} if seeded else {})
    if seeded:
        VapiObject.objects.create(kind="tool", name="faq_lookup", vapi_id="real-tool-id", last_provision_hash="old")
        VapiObject.objects.create(kind="assistant", name="entry_faq", vapi_id="real-faq-id", last_provision_hash="old")
        VapiObject.objects.create(kind="squad", name=C.SQUAD_NAME, vapi_id=pinned or "real-squad-id")
    before = _rows()

    _run("--per-store")

    after = _rows()
    assert after == before, "a dry run wrote to the database"
    stored = [str(v) for table in after for row in table for v in row]
    assert not [v for v in stored if v.startswith("dryrun-")], "a synthetic dryrun- id was stored"


@pytest.mark.django_db
def test_a_dry_run_assistant_never_writes_its_synthetic_id_onto_the_agent_prompt(dry, faq_prompt, monkeypatch):  # noqa: F811
    """With the faq_lookup tool already recorded the assistant is NOT skipped, so ``ensure_assistant``
    reaches the AgentPrompt write-back; in a dry run it must not (the live call path reads that id)."""
    from voice import provision

    monkeypatch.setattr(vapi, "configured", lambda: False)
    VapiObject.objects.create(kind="tool", name="faq_lookup", vapi_id="real-tool-id")
    vapi.set_dry_run(True)
    result = provision.ensure_assistant("faq", name="entry_faq")
    vapi.set_dry_run(False)
    assert result.action == "created" and result.vapi_id.startswith("dryrun-")  # the control: it did plan a create
    assert AgentPrompt.objects.get(role="faq").vapi_assistant_id == ""


@pytest.mark.django_db
def test_the_dry_run_still_reports_what_it_would_do(dry, faq_prompt, monkeypatch):  # noqa: F811
    """Control: the guard must not turn the dry run into a no-op that plans nothing."""
    monkeypatch.setattr(vapi, "configured", lambda: False)
    monkeypatch.setattr(vapi, "find_phone_number", lambda _id: {"id": _id})
    monkeypatch.setattr(vapi, "get_phone_number", lambda _id: {"id": _id})
    out = StringIO()
    call_command("provision_vapi", "--dry-run", stdout=out)
    vapi.set_dry_run(False)
    text = out.getvalue()
    assert "created" in text and "faq_lookup" in text
    assert "# POST/PATCH /tool  (faq_lookup)" in text
    assert "squad" in text and "patched" in text  # the chain tool -> assistant -> squad -> phone reads whole
    assert not VapiObject.objects.exists()
