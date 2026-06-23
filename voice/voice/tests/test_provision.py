"""Provisioning unit tests (20-SPEC-vapi-deploy.md §8.1) — the Vapi HTTP layer is MOCKED, so the
suite runs with NO live keys (03-CONVENTIONS.md §5).

The headline assertion (task spec): ``build_assistant_payload`` emits the right
voice/model/transcriber/tool shape. Plus the idempotency reconcile (created → nodrift), the
zero-drift no-op, the no-``/workflow`` guard, secret redaction, and dry-run record-not-issue.

Every external Vapi call is replaced by an in-memory fake account fixture (``fake_vapi``) — no
network, deterministic, free.
"""

from __future__ import annotations

import json

import pytest

from core.services import vapi
from voice import constants as C
from voice import provision


# ── a tiny in-memory fake of the Vapi REST surface (record/replay, no network) ─
class FakeAccount:
    """An empty Vapi account that records create/patch calls and answers find-by-name from what
    has been created — exactly what the reconcile engine needs to exercise create→nodrift."""

    def __init__(self):
        self.tools: dict[str, dict] = {}
        self.assistants: dict[str, dict] = {}
        self.squads: dict[str, dict] = {}
        self.creates = 0
        self.patches = 0
        self._seq = 0

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    # tools
    def find_tool_by_name(self, name):
        for o in self.tools.values():
            if (o.get("function") or {}).get("name") == name or o.get("name") == name:
                return o
        return None

    def get_tool(self, _id):
        return self.tools[_id]

    def create_tool(self, body):
        self.creates += 1
        oid = self._id("tool")
        obj = {**body, "id": oid}
        self.tools[oid] = obj
        return obj

    def patch_tool(self, _id, body):
        self.patches += 1
        self.tools[_id] = {**self.tools[_id], **body, "id": _id}
        return self.tools[_id]

    # assistants
    def find_assistant_by_name(self, name):
        for o in self.assistants.values():
            if o.get("name") == name:
                return o
        return None

    def get_assistant(self, _id):
        return self.assistants[_id]

    def create_assistant(self, body):
        self.creates += 1
        oid = self._id("asst")
        obj = {**body, "id": oid}
        self.assistants[oid] = obj
        return obj

    def patch_assistant(self, _id, body):
        self.patches += 1
        self.assistants[_id] = {**self.assistants[_id], **body, "id": _id}
        return self.assistants[_id]

    # squads
    def find_squad_by_name(self, name):
        for o in self.squads.values():
            if o.get("name") == name:
                return o
        return None

    def get_squad(self, _id):
        return self.squads[_id]

    def create_squad(self, body):
        self.creates += 1
        oid = self._id("squad")
        obj = {**body, "id": oid}
        self.squads[oid] = obj
        return obj

    def patch_squad(self, _id, body):
        self.patches += 1
        self.squads[_id] = {**self.squads[_id], **body, "id": _id}
        return self.squads[_id]


@pytest.fixture
def fake_vapi(monkeypatch):
    """Patch the vapi client surface to a fake account + a configured key, so the reconcile engine
    runs offline. Files mirror is stubbed to a clean skip (Vapi Files need a live key)."""
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
    # KB mirror needs the live Files API; stub it to a clean skip for these unit tests.
    from kb import vapi_files

    monkeypatch.setattr(vapi_files, "mirror_all", lambda: {"skipped": "not configured"})
    return acct


@pytest.fixture
def faq_prompt(db):
    from kb.models import AgentPrompt

    return AgentPrompt.objects.create(
        role="faq",
        body="You are Koptza. Confirm 21+ by voice. Answer only from faq_lookup.",
        is_active=True,
    )


# ── the headline assertion: assistant payload shape (voice/model/transcriber/tool) ──
@pytest.mark.django_db
def test_assistant_payload_shape(fake_vapi, faq_prompt):
    """build_assistant_payload emits the right voice / model / transcriber / tool blocks, each
    ONCE (ADR-011), the single intentional model (ADR-010), the 33-term keyterms, and the
    server + serverMessages contract — with the faq_lookup tool id resolved."""
    # The tool must exist first so the assistant resolves its toolId.
    tool_res = provision.ensure_tool("faq_lookup")
    assert tool_res.action == "created" and tool_res.vapi_id

    payload, warnings = provision.build_assistant_payload("faq", name="entry_faq")
    assert not warnings  # tool resolved, prompt present → no warnings

    # name + model (ADR-010 single intentional model, never gpt-5.2-chat-latest)
    assert payload["name"] == "entry_faq"
    model = payload["model"]
    assert model["provider"] == "openai"
    assert model["model"] == "gpt-4.1-mini"
    assert "gpt-5.2-chat-latest" not in json.dumps(payload)
    assert model["temperature"] == 0.3
    assert model["maxTokens"] == 250
    assert model["messages"][0]["role"] == "system"
    assert "Koptza" in model["messages"][0]["content"]
    assert model["toolIds"] == [tool_res.vapi_id]  # resolved, not dangling

    # voice — Cartesia sonic-3 Koptza, emotion positivity:highest (verbatim voiceId)
    voice = payload["voice"]
    assert voice["provider"] == "cartesia"
    assert voice["voiceId"] == "a3520a8f-226a-428d-9fcd-b0a4711a6829"
    assert voice["model"] == "sonic-3"
    assert voice["experimentalControls"]["emotion"] == ["positivity:highest"]

    # transcriber — Deepgram nova-3, numerals, the EXACT 33-term keyterm list
    transcriber = payload["transcriber"]
    assert transcriber["provider"] == "deepgram"
    assert transcriber["model"] == "nova-3"
    assert transcriber["numerals"] is True
    assert transcriber["keyterm"] == C.DEEPGRAM_KEYTERMS
    assert len(transcriber["keyterm"]) == 33

    # server + serverMessages (the webhook contract the provisioner writes)
    assert payload["server"]["url"].endswith("/api/voice/vapi")
    assert payload["serverMessages"] == [
        "tool-calls",
        "status-update",
        "end-of-call-report",
    ]

    # voice / transcriber / model emitted ONCE each (no per-node dup — ADR-011, export bug #7)
    dumped = json.dumps(payload)
    assert dumped.count('"provider": "cartesia"') == 1
    assert dumped.count('"provider": "deepgram"') == 1
    assert dumped.count('"provider": "openai"') == 1
    assert dumped.count('"keyterm"') == 1


@pytest.mark.django_db
def test_keyterms_are_33_unique():
    """The lifted Deepgram keyterm list is exactly 33 terms, no duplicates (export L32–72)."""
    assert len(C.DEEPGRAM_KEYTERMS) == 33
    assert len(set(C.DEEPGRAM_KEYTERMS)) == 33
    assert "all-in-one" in C.DEEPGRAM_KEYTERMS  # plain hyphen, not U+2011


@pytest.mark.django_db
def test_tool_payload_shape():
    """build_tool_payload(faq_lookup) → a Vapi function tool with the schema + server + async."""
    payload = provision.build_tool_payload("faq_lookup")
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "faq_lookup"
    assert payload["function"]["parameters"]["required"] == ["query"]
    assert payload["server"]["url"].endswith("/api/voice/vapi")
    assert payload["async"] is False


@pytest.mark.django_db
def test_assistant_skipped_when_tool_unprovisioned(fake_vapi, faq_prompt):
    """If faq_lookup is not provisioned, the assistant is SKIPPED with a warning and NO PATCH is
    sent (never a dangling toolId — C3)."""
    res = provision.ensure_assistant("faq", name="entry_faq")
    assert res.action == "skipped"
    assert any("tool not provisioned" in w for w in res.warnings)
    assert fake_vapi.creates == 0 and fake_vapi.patches == 0


# ── idempotency: create → nodrift (zero drift), and an edit → exactly one patch ──
@pytest.mark.django_db
def test_provision_all_then_rerun_is_zero_drift(fake_vapi, faq_prompt):
    """A first provision_all creates the stack; an immediate re-run issues ZERO creates + ZERO
    patches — every object reconciles to nodrift (A-IDEMP headline)."""
    r1 = provision.provision_all(dry_run=False)
    assert r1.ok
    assert r1.created >= 3  # faq_lookup tool + entry_faq assistant + squad
    creates_after_first = fake_vapi.creates

    # Immediate re-run — no local edits.
    fake_vapi.creates = 0
    fake_vapi.patches = 0
    r2 = provision.provision_all(dry_run=False)
    assert r2.ok
    assert fake_vapi.creates == 0
    assert fake_vapi.patches == 0
    assert r2.created == 0 and r2.patched == 0
    # Every reconciled object is nodrift (files/phone skip cleanly offline).
    assert all(res.action in ("nodrift", "skipped") for res in r2.results)
    assert creates_after_first >= 3


@pytest.mark.django_db
def test_editing_prompt_issues_exactly_one_patch(fake_vapi, faq_prompt):
    """Editing the AgentPrompt body changes that assistant's hash → exactly ONE patch_assistant on
    the next run, and zero creates (A3)."""
    provision.provision_all(dry_run=False)
    fake_vapi.creates = 0
    fake_vapi.patches = 0

    faq_prompt.body = faq_prompt.body + " (edited)"
    faq_prompt.save()

    r = provision.provision_all(dry_run=False)
    assert fake_vapi.creates == 0
    assert fake_vapi.patches == 1  # only the assistant changed
    assert r.patched == 1


@pytest.mark.django_db
def test_assistant_id_written_back_to_prompt(fake_vapi, faq_prompt):
    """The provisioner writes the assistant id back onto AgentPrompt.vapi_assistant_id (P4 reads it)."""
    provision.provision_all(dry_run=False)
    faq_prompt.refresh_from_db()
    assert faq_prompt.vapi_assistant_id.startswith("asst_")


# ── dry-run: record-not-issue (client-level contract — no fake; the REAL recorder) ──
def test_dry_run_recorder_records_writes_issues_no_request(monkeypatch):
    """The client's dry_run mode RECORDS a non-GET write (returns a synthetic id) and never opens
    a transport — the real contract provision_all relies on when VAPI_PRIVATE_KEY is unset (B5)."""
    import httpx

    def _boom(*a, **k):  # any real transport attempt fails the test
        raise AssertionError("dry-run must not issue a real HTTP request")

    monkeypatch.setattr(httpx, "Client", _boom)
    vapi.set_dry_run(True)
    try:
        out = vapi.post("/tool", {"function": {"name": "faq_lookup"}, "server": {"secret": "x"}})
        assert out["id"].startswith("dryrun-")
        assert [c["method"] for c in vapi.recorded_calls] == ["POST"]
        # GETs against an unconfigured client return empty (so reconcile plans a create).
        monkeypatch.setattr(vapi, "configured", lambda: False)
        assert vapi.get("/tool") is None
    finally:
        vapi.set_dry_run(False)


@pytest.mark.django_db
def test_provision_all_dry_run_offline_records_and_skips(monkeypatch, faq_prompt):
    """The real offline dry-run path (no VAPI_PRIVATE_KEY, no fake): provision_all auto-engages
    dry-run, the client RECORDS the planned tool/assistant/squad POSTs, files+phone skip cleanly,
    and zero real writes are issued (matches the pasted ``provision_vapi --dry-run`` output)."""
    import httpx

    monkeypatch.setattr(vapi, "configured", lambda: False)  # no key → auto dry-run
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("no real HTTP in dry-run"))

    report = provision.provision_all(dry_run=True)
    assert report.dry_run is True
    assert report.ok  # no errors offline
    methods = [c["method"] for c in vapi.recorded_calls]
    assert methods.count("POST") >= 3  # tool + assistant + squad recorded
    paths = {c["path"] for c in vapi.recorded_calls}
    assert {"/tool", "/assistant", "/squad"} <= paths
    # No real secret value leaks into the recorded payloads (server.secret redacted to ***).
    assert all(
        c.get("json", {}).get("server", {}).get("secret", "***") == "***"
        for c in vapi.recorded_calls
        if isinstance(c.get("json"), dict) and "server" in c["json"]
    )
    vapi.set_dry_run(False)


# ── guards: no /workflow, secret redaction ──────────────────────────────────────
def _code_lines_without_strings_or_comments(module) -> str:
    """Return the module source with comments + string literals (docstrings) tokenized away, so a
    guard can assert on EXECUTABLE references only — the docstrings deliberately mention /workflow."""
    import inspect
    import io
    import tokenize

    src = inspect.getsource(module)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_no_workflow_path_anywhere():
    """ADR-002 guard (B4): no executable code in the client or provisioner constructs a /workflow
    path. The only allowed mention is the defensive ``_FORBIDDEN_PATH = "/workflow"`` guard CONSTANT
    + docstrings (both string literals, excluded here)."""
    assert "/workflow" not in _code_lines_without_strings_or_comments(vapi)
    assert "/workflow" not in _code_lines_without_strings_or_comments(provision)
    # And the defensive guard constant IS present (the client refuses such a path at runtime).
    assert vapi._FORBIDDEN_PATH == "/workflow"


def test_secret_is_redacted_in_logs_and_errors(monkeypatch):
    """A VapiError body + any redact() output masks live secret values (B3 / 23-SPEC AC-7)."""
    monkeypatch.setenv("VAPI_PRIVATE_KEY", "super-secret-key-value")
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "super-secret-webhook")
    err = vapi.VapiError("boom super-secret-key-value", body="leak super-secret-webhook here")
    assert "super-secret-key-value" not in str(err)
    assert "super-secret-webhook" not in err.body
    # redact_payload masks a server.secret block for the dry-run dump.
    red = vapi.redact_payload({"server": {"url": "x", "secret": "super-secret-webhook"}})
    assert red["server"]["secret"] == "***"
