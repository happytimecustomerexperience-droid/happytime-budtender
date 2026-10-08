"""HHT_SQUAD_MODE=single (the default): ONE concierge agent in a one-member squad, zero handoffs.

The owner heard "let me get a member that knows" and then dead silence. Single mode makes the first
agent do the whole call itself. These tests pin: the rendered concierge prompt (every intent section,
every code-owned rule, none of the multi-agent phrases), its exact tool list, the one-member squad
(static, per store, per call), that multi mode is byte-identical to before, the dry run and live run
in both modes (VAPI_SQUAD_ID adopted, old assistants left alone), the doctor, and the create-only seed.
Offline: Vapi is faked or dry-run; nothing here can judge how the voice SOUNDS (see the docs' test calls).
"""

from __future__ import annotations

import hashlib
import json
import re
from io import StringIO

import pytest
from django.core.management import call_command

from core.services import vapi
from kb import seed
from voice import caller, provision
from voice import constants as C
from voice import safety_copy as S
from voice.tests.test_vapi_doctor import (  # noqa: F401  (fixture)
    OURS,
    SQUAD,
    WEBHOOK_SECRET,
    report,
    world,
)

TOOL_IDS = {
    n: f"tool-{i}"
    for i, n in enumerate(
        ["faq_lookup", "suggest_products", "check_inventory", "pair_upsell", "stage_phone_cart",
         "notify_vendor_callback", "notify_staff_issue", "remember_caller", "kb_query"]
    )
}
MULTI = {"faq": "a-faq", "entry_router": "a-er", "budtender": "a-bt", "vendor": "a-v", "escalation": "a-e"}
ALL = {**MULTI, "concierge": "a-c"}

# The pre-change multi-mode payloads (every assistant x {no store, yakima}, the squad x {shared,
# pullman}, the per-call squad), captured from the code BEFORE single mode existed. Multi mode with
# HHT_TRANSFER_CONSULT=0 must reproduce them byte for byte.
MULTI_SNAPSHOT_SHA = {
    False: "9cac7a9e7e1ec29e9bea59e0a0d9f81507281914c1a3671c3b351d1eb610f7ce",
    True: "6e054afb854cf0b392f47ee4e29dc50a42d52e5dfccf4c3e0164b7871ee4f22e",
}

# Anything that tells a caller there is more than one agent. Scanned over the WHOLE rendered prompt
# minus the ONE VOICE block, the only place these appear (quoted, as prohibitions).
BANNED = [
    r"let me get (?:you )?(?:a |an |our |the )?(?:member|specialist|expert|colleague|teammate|agent|someone who knows)",
    r"\b(?:member|someone|somebody|specialist|agent|person) (?:that|who) knows\b",
    r"\bteammates?\b",
    r"\bcolleagues?\b",
    r"\bspecialists?\b",
    r"\banother agent\b",
    r"\bother agents?\b",
    r"\bsquad\b",
    r"\b(?:transfer|transferring|hand|handing|pass|passing|send|sending|route|routing) (?:you|them|the caller) "
    r"(?:over |off )?to (?:our |the |a )?(?:budtender|faq|vendor|escalation|agent|member|department)",
    r"\bhand (?:you|them|it) (?:off|over)\b",
    r"\bhand-off\b",
    r"\bhand to (?:the )?(?:budtender|faq|vendor|escalation)\b",
    r"\bhanding you over\b",
    r"\bentry[_ ](?:router|greeter)\b",
    r"\b(?:the|our) (?:faq|vendor|escalation|budtender) (?:agent|member|assistant)\b",
    r"\(escalation\)",
    r"\bour budtender will\b",
    r"\bconnecting you\b",
]


@pytest.fixture
def line(db, settings):
    """The real seeded rows + every custom tool provisioned (fake ids)."""
    from voice.models import VapiObject

    settings.HHT_SQUAD_MODE = "single"
    settings.HHT_TRANSFER_CONSULT = True
    settings.HHT_DYNAMIC_GREETING = True
    settings.PUBLIC_BASE_URL = "https://voice.example.test"
    settings.VAPI_WEBHOOK_SECRET = "s" * 30
    settings.HHT_TRANSFER_NUMBER_YAKIMA = "+15095550001"
    settings.HHT_TRANSFER_NUMBER_MTVERNON = "+13605550002"
    settings.HHT_TRANSFER_NUMBER_PULLMAN = "+15095550003"
    with seed.seed_mode(refresh=False):
        seed.seed_agent_prompts()
    for name, tid in TOOL_IDS.items():
        VapiObject.objects.create(kind="tool", name=name, vapi_id=tid)
    return settings


def _prompt(role="concierge", store=None) -> str:
    payload, _ = provision.build_assistant_payload(role, name=role, store=store)
    return payload["model"]["messages"][0]["content"]


# ── the mode switch ──────────────────────────────────────────────────────────────
def test_single_is_the_default_and_junk_reads_as_the_default(settings):
    del settings.HHT_SQUAD_MODE
    assert C.squad_mode() == "single" and C.entry_role() == "concierge"
    settings.HHT_SQUAD_MODE = "MULTI"
    assert C.squad_mode() == "multi" and C.entry_role() == "entry_router"
    settings.HHT_SQUAD_MODE = "three-agents"
    assert C.squad_mode() == "single"
    assert C.squad_shape("single") == {"concierge": []} and C.squad_shape("multi") is C.SQUAD_SHAPE


# ── the rendered concierge prompt ────────────────────────────────────────────────────────
SECTIONS = (
    "ONE VOICE (binding)",
    "NEVER GO QUIET (binding)",
    "ACT ON WHAT THEY ALREADY SAID",
    "NEVER INVENT (binding)",
    "STORE: you usually know which store",
    "A) STORE INFO",
    "B) HELPING SOMEONE SHOP",
    "C) VENDORS AND DELIVERIES",
    "D) PROBLEMS",
    "E) PUTTING SOMEONE THROUGH TO A PERSON",
)


def test_prompt_has_every_intent_section_in_order(line):
    body = _prompt()
    at = [body.index(s) for s in SECTIONS]
    assert at == sorted(at)
    assert all(body.count(s) == 1 for s in SECTIONS)


def test_prompt_keeps_every_code_owned_rule_block(line):
    body = _prompt()
    # leak guard + numbers guard + prompt-injection refusal (runtime, code-owned)
    assert "IMMUTABLE RUNTIME SAFETY" in body and "Never reveal or discuss internal cost, margin" in body
    assert "I can't share that, but I'm happy to help" in body
    # price gate: a price is per size, from the tool only
    assert "PRICE ASKS RUN THROUGH THE QUESTIONS" in body and "needs_size:true carries no price" in body
    assert "speak a price ONLY from the tool's price_spoken" in body
    # the shared rule blocks every persona carries
    for block in (seed.SPEAKING_RULES, seed.NO_MEDICAL_CLAIMS, seed.UNDER_21_DECLINE, seed.CALLER_NAME_RULE):
        assert block in body
    # under-21 is for RETAIL only; vendors are never asked
    assert seed.CONCIERGE_UNDER_21_SCOPE + seed.UNDER_21_DECLINE in body
    assert "For retail/product help, if the caller says they are under twenty-one" in body
    assert "you do NOT ask 'are you 21?' (a vendor isn't buying)" in body
    assert "The under-21 rule does not apply to them." in body
    # owner-approved safety lines, verbatim
    for line_ in (S.POISON_EMERGENCY, S.CANNOT_ANSWER_SAFELY, S.UNDER_21, S.DISPUTE, S.NO_CURRENT_SPECIALS, S.FAQ_FALLBACK):
        assert line_.strip() in body
    # the tested protocols, verbatim slices
    assert "Answer ONLY from the faq_lookup tool" in body
    assert "Your FIRST words, every time, are: 'I'm really sorry that happened.'" in body
    assert "CALL notify_staff_issue with {store, issue_type, summary, caller_name}" in body
    assert "never call notify_vendor_callback before a transfer was tried" in body
    assert "NEVER invent a time or a window" in body
    assert "LAB DATA & POTENCY (binding)" in body and "PHONE CART HANDOFF" in body
    # caller notes: the per-call variable is the last thing in the prompt
    assert body.rstrip().endswith("{{caller_context}}") and "CALLER CONTEXT (code-owned" in body


def test_prompt_has_the_one_voice_no_silence_and_first_sentence_rules(line):
    body = _prompt()
    assert "one short natural filler ('One sec, let me check.') and then ALWAYS speak the result" in body
    assert "If a tool fails, errors, or returns nothing usable, say so plainly" in body
    assert "never answer a clear request with 'how can I help?'" in body and "'a 1:1 gummy for tonight'" in body
    assert "Ask at most ONE question per turn" in body and "keep every turn short" in body
    assert "never say you are connecting them before a person has said yes" in body


def test_prompt_has_the_transfer_rules(line):
    body = _prompt()
    assert "'Who should I say is calling, and what is it about?'" in body
    assert "the team will hear 'a caller'" in body
    assert "call transferCall once" in body and "Never promise that someone is available" in body
    assert "offer to take a message" in body
    assert "notify_vendor_callback for a vendor or notify_staff_issue for anyone else" in body
    assert "Never say a phone number" in body


def test_prompt_contains_no_multi_agent_language(line):
    for store in (None, "yakima"):
        body = _prompt(store=store)
        assert body.count(seed.CONCIERGE_ONE_VOICE) == 1
        rest = body.replace(seed.CONCIERGE_ONE_VOICE, "")
        hits = [(p, m.group(0)) for p in BANNED for m in re.finditer(p, rest, re.IGNORECASE)]
        assert hits == []
    # the guard itself works: each banned phrase the owner reported is caught
    for said in ("let me get a member that knows", "transferring you to our budtender", "let me get someone who knows",
                 "I'll hand you off", "our vendor agent"):
        assert any(re.search(p, said, re.IGNORECASE) for p in BANNED), said


def test_the_multi_bodies_are_untouched_by_the_concierge(line):
    from kb.models import AgentPrompt

    assert AgentPrompt.objects.get(role="budtender").body.startswith(seed.BUDTENDER_BODY)
    assert AgentPrompt.objects.get(role="entry_router").body.startswith(seed.ENTRY_ROUTER_BODY)
    assert seed.CONCIERGE_BODY not in AgentPrompt.objects.get(role="budtender").body


# ── tools ──────────────────────────────────────────────────────────────────────
EXPECTED_TOOLS = ["faq_lookup", "suggest_products", "check_inventory", "pair_upsell", "stage_phone_cart",
                  "notify_vendor_callback", "notify_staff_issue"]


def test_tool_names_are_exactly_the_specified_set(line, settings):
    from kb.models import AgentPrompt

    row = AgentPrompt.objects.get(role="concierge")
    assert row.tool_names == EXPECTED_TOOLS == C.MEMBER_TOOLS["concierge"]
    assert provision._tool_names_for_role("concierge", row) == [*EXPECTED_TOOLS, "remember_caller"]
    payload, warnings = provision.build_assistant_payload("concierge", name="concierge")
    assert payload["model"]["toolIds"] == [TOOL_IDS[n] for n in [*EXPECTED_TOOLS, "remember_caller"]]
    assert TOOL_IDS["kb_query"] not in payload["model"]["toolIds"]
    assert [t["type"] for t in payload["model"]["tools"]] == ["transferCall"]
    assert [d["number"] for d in payload["model"]["tools"][0]["destinations"]] == [
        "+15095550001", "+13605550002", "+15095550003"]
    assert warnings == []
    settings.HHT_DYNAMIC_GREETING = False  # no greeting by name → no remember_caller, no caller block
    payload, _ = provision.build_assistant_payload("concierge", name="concierge")
    assert payload["model"]["toolIds"] == [TOOL_IDS[n] for n in EXPECTED_TOOLS]
    assert "{{caller_context}}" not in payload["model"]["messages"][0]["content"]


def test_transfers_off_means_no_transfer_tool_and_no_promise(line):
    from voice import capabilities

    capabilities.set_enabled("call.transfer", False)
    payload, _ = provision.build_assistant_payload("concierge", name="concierge")
    assert "tools" not in payload["model"]
    assert "Call transfers are switched off" in payload["model"]["messages"][0]["content"]


def test_concierge_speaks_first_with_the_entry_greeting(line):
    payload, _ = provision.build_assistant_payload("concierge", name="concierge", store="yakima")
    assert payload["firstMessageMode"] == "assistant-speaks-first"
    assert payload["firstMessage"] == seed.ENTRY_FIRST_MESSAGE
    assert provision.entry_greeting() == seed.ENTRY_FIRST_MESSAGE


# ── the squad ─────────────────────────────────────────────────────────────────
def test_squad_payload_is_one_member_with_no_destinations(line):
    squad = provision.build_squad_payload(ALL)
    assert squad == {"name": "Happy Time Voice", "members": [{"assistantId": "a-c", "assistantDestinations": []}]}


def test_store_squad_is_one_member_with_that_stores_overrides(line):
    squad = provision.build_squad_payload(ALL, "pullman")
    (member,) = squad["members"]
    assert member["assistantId"] == "a-c" and member["assistantDestinations"] == []
    body = member["assistantOverrides"]["model"]["messages"][0]["content"]
    assert "Pullman store's own phone line" in body
    assert [d["number"] for d in member["assistantOverrides"]["model"]["tools"][0]["destinations"]] == ["+15095550003"]


def test_until_the_concierge_is_provisioned_the_multi_squad_keeps_answering(line):
    assert provision.effective_mode(MULTI) == "multi"
    assert len(provision.build_squad_payload(MULTI)["members"]) == 5
    assert provision.build_call_squad(MULTI, None, first_message=lambda b: b, variables={})["members"][0][
        "assistantId"] == "a-er"


def test_call_squad_is_one_member_with_the_variables_and_the_opener(line):
    ctx = {"first_name": "Sam", "has_history": True, "known": True}
    squad = provision.build_call_squad(
        ALL, "yakima", first_message=lambda base: caller.greeting(ctx, base), variables={"caller_context": "CALLER: Sam."}
    )
    (member,) = squad["members"]
    assert member["assistantId"] == "a-c" and member["assistantDestinations"] == []
    assert member["assistantOverrides"]["firstMessage"].startswith("Welcome back to Happy Time, Sam!")
    assert squad["membersOverrides"] == {"variableValues": {"caller_context": "CALLER: Sam."}}


def test_assistant_request_answers_with_the_one_member_squad(line, monkeypatch):
    from kb.models import AgentPrompt
    from voice import webhooks as W

    for role, aid in ALL.items():
        AgentPrompt.objects.filter(role=role).update(vapi_assistant_id=aid)
    monkeypatch.setattr(caller, "for_call", lambda *a, **k: {"first_name": "Ana", "known": True})
    resp = W.handle_assistant_request({"type": "assistant-request", "call": {"id": "c-1", "customer": {"number": "+15095551212"}}})
    squad = json.loads(resp.content)["squad"]
    assert [m["assistantId"] for m in squad["members"]] == ["a-c"]
    assert squad["members"][0]["assistantOverrides"]["firstMessage"].startswith("Welcome back to Happy Time, Ana!")
    assert squad["membersOverrides"]["variableValues"]["caller_first_name"] == "Ana"
    # dynamic off: the static answer is the concierge assistant (it can do everything)
    line.HHT_DYNAMIC_GREETING = False
    body = json.loads(W.handle_assistant_request({"type": "assistant-request", "call": {"id": "c-2"}}).content)
    assert body["assistantId"] == "a-c"


# ── multi mode is byte-identical to before ────────────────────────────────────────────
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("members", [MULTI, ALL], ids=["multi-members", "with-concierge-too"])
def test_multi_mode_reproduces_the_pre_change_payloads(line, dynamic, members):
    line.HHT_SQUAD_MODE = "multi"
    line.HHT_TRANSFER_CONSULT = False
    line.HHT_DYNAMIC_GREETING = dynamic
    line.HHT_TRANSFER_NUMBER_MTVERNON = ""
    line.HHT_TRANSFER_NUMBER_PULLMAN = ""
    out = {}
    for role in ["entry_router", "budtender", "faq", "vendor", "escalation"]:
        for store in [None, "yakima"]:
            out[f"asst:{role}:{store}"] = list(provision.build_assistant_payload(role, name=role, store=store))
    for store in [None, "pullman"]:
        out[f"squad:{store}"] = provision.build_squad_payload(members, store)
    out["call"] = provision.build_call_squad(members, "yakima", first_message=lambda b: "HELLO " + b, variables={"x": 1})
    digest = hashlib.sha256(json.dumps(out, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == MULTI_SNAPSHOT_SHA[dynamic]


# ── provision_vapi ─────────────────────────────────────────────────────────────
def _dry_run(monkeypatch) -> str:
    import httpx

    monkeypatch.setattr(vapi, "configured", lambda: False)  # no key: a dry run whatever the flag
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("no real HTTP in a dry run"))
    buf = StringIO()
    try:
        call_command("provision_vapi", "--dry-run", stdout=buf)
    finally:
        vapi.set_dry_run(False)
    return buf.getvalue()


def _squad_block(out: str) -> dict:
    text = out.split("POST/PATCH /squad  (Happy Time Voice)", 1)[1].split("\n", 1)[1]
    end = text.find("\n\n#")
    return json.loads(text if end < 0 else text[:end])


def test_dry_run_single_shows_the_concierge_and_the_one_member_squad(line, monkeypatch):
    line.HHT_DYNAMIC_GREETING = False
    out = _dry_run(monkeypatch)
    assert "HHT_SQUAD_MODE=single" in out
    assert "POST/PATCH /assistant  (concierge)" in out and "POST/PATCH /assistant  (entry_faq)" not in out
    assert re.search(r"assistant\s+concierge\s+created", out)
    for name in ("entry_faq", "entry_router", "budtender", "vendor", "escalation"):
        assert re.search(rf"assistant\s+{name}\s+skipped .*left as is in Vapi for rollback", out), name
    squad = _squad_block(out)
    assert len(squad["members"]) == 1 and squad["members"][0]["assistantDestinations"] == []
    assert "DELETE" not in out
    # a dry run never saves its synthetic id as the live concierge id (calls are built from it)
    from kb.models import AgentPrompt

    assert AgentPrompt.objects.get(role="concierge").vapi_assistant_id == ""
    assert provision.saved_member_ids() == {}


def test_dry_run_multi_is_the_old_plan(line, monkeypatch):
    line.HHT_SQUAD_MODE = "multi"
    line.HHT_DYNAMIC_GREETING = False
    out = _dry_run(monkeypatch)
    assert "HHT_SQUAD_MODE" not in out and "concierge" not in out
    assert "POST/PATCH /assistant  (entry_faq)" in out
    assert re.search(r"assistant\s+entry_router\s+created", out)


def test_live_single_run_patches_the_owners_squad_and_leaves_old_agents_alone(line, monkeypatch):
    from voice.models import VapiObject
    from voice.tests.test_provision import FakeAccount

    live = "2b132e78-6b37-4b12-b99a-17d23f8906e7"
    acct = FakeAccount()
    acct.squads[live] = {"id": live, "name": "Owner's live squad", "members": [{"assistantId": "old"}]}
    old_ids = {}
    for role in ("entry_router", "budtender", "vendor", "escalation"):  # the multi agents already live
        old_ids[role] = acct.create_assistant({"name": role})["id"]
        VapiObject.objects.create(kind="assistant", name=role, vapi_id=old_ids[role])
    VapiObject.objects.filter(kind="tool").delete()  # tools get provisioned by the run
    monkeypatch.setattr(vapi, "configured", lambda: True)
    monkeypatch.setattr(vapi, "auth_ok", lambda: {"ok": True, "configured": True, "error": ""})
    for name in ("find_tool_by_name", "get_tool", "create_tool", "patch_tool", "find_assistant_by_name",
                 "get_assistant", "create_assistant", "patch_assistant", "find_squad_by_name", "create_squad"):
        monkeypatch.setattr(vapi, name, getattr(acct, name))
    patched_squads = []
    monkeypatch.setattr(vapi, "get_squad", lambda _id: acct.squads[_id])
    monkeypatch.setattr(vapi, "patch_squad", lambda _id, body: patched_squads.append((_id, body)) or {"id": _id})
    from kb import vapi_files

    monkeypatch.setattr(vapi_files, "mirror_all", lambda: {"skipped": "not configured"})
    line.VAPI_SQUAD_ID = live
    line.VAPI_PHONE_NUMBER_ID = ""
    before = {k: dict(v) for k, v in acct.assistants.items()}

    report = provision.provision_all(dry_run=False)
    assert report.ok, [r.line() for r in report.results]
    concierge = next(r for r in report.results if r.name == "concierge")
    assert concierge.action == "created"
    assert list(acct.squads) == [live]  # never a second squad
    ((sid, body),) = patched_squads
    assert sid == live and body["members"] == [{"assistantId": concierge.vapi_id, "assistantDestinations": []}]
    for role, aid in old_ids.items():  # not PATCHed, not deleted
        assert acct.assistants[aid] == before[aid], role
    # re-run: zero creates
    creates = acct.creates
    provision.provision_all(dry_run=False)
    assert acct.creates == creates


# ── the doctor ─────────────────────────────────────────────────────────────────


def _concierge_live(world, settings, *, destinations=None, mode="warm-transfer-experimental", tool_ids=None):  # noqa: F811
    from kb.models import AgentPrompt

    fv, _ = world
    settings.HHT_SQUAD_MODE = "single"
    with seed.seed_mode(refresh=False):
        seed.seed_agent_prompts()
    AgentPrompt.objects.filter(role="concierge").update(vapi_assistant_id="asst-concierge")
    expected, _ = provision._resolve_tool_ids("concierge", [], AgentPrompt.objects.get(role="concierge"))
    dests = [{"type": "number", "number": "+15095711106", "transferPlan": {"mode": mode}}]
    fv.routes[f"/squad/{SQUAD}"] = (200, {"id": SQUAD, "name": "Happy Time Voice", "members": [
        {"assistantId": "asst-concierge", "assistantDestinations": destinations or []}]})
    fv.routes["/assistant/asst-concierge"] = (200, {
        "id": "asst-concierge", "name": "concierge",
        "model": {"provider": "google", "model": "gemini-2.5-flash-lite",
                  "toolIds": expected if tool_ids is None else tool_ids,
                  "tools": [{"type": "transferCall", "destinations": dests}]},
        "server": {"url": OURS, "secret": WEBHOOK_SECRET},
    })
    return fv


def test_doctor_single_mode_one_concierge_member_passes(world, settings):  # noqa: F811
    from voice.models import VapiObject

    for name, tid in TOOL_IDS.items():
        VapiObject.objects.update_or_create(kind="tool", name=name, defaults={"vapi_id": tid})
    _concierge_live(world, settings)
    checks, _ = report()
    assert checks["vapi.squad_shape"]["status"] == "PASS"
    assert checks["vapi.member.concierge"]["status"] == "PASS", checks["vapi.member.concierge"]
    assert checks["vapi.transfer_consult"]["status"] == "PASS"
    assert checks["vapi.call_squad"]["status"] == "PASS"


def test_doctor_warns_when_the_live_squad_is_still_multi_member(world, settings):  # noqa: F811
    settings.HHT_SQUAD_MODE = "single"
    with seed.seed_mode(refresh=False):
        seed.seed_agent_prompts()
    checks, _ = report()
    c = checks["vapi.squad_shape"]
    assert c["status"] == "WARN" and "5 members" in c["detail"] and "provision_vapi" in c["fix"]


def test_doctor_warns_on_handoff_destinations_wrong_tools_or_no_consult(world, settings):  # noqa: F811
    from voice.models import VapiObject

    for name, tid in TOOL_IDS.items():
        VapiObject.objects.update_or_create(kind="tool", name=name, defaults={"vapi_id": tid})
    _concierge_live(world, settings, destinations=[{"type": "assistant", "assistantName": "budtender"}])
    assert report()[0]["vapi.squad_shape"]["status"] == "WARN"
    _concierge_live(world, settings, tool_ids=["tool-0"])
    member = report()[0]["vapi.member.concierge"]
    assert member["status"] == "WARN" and "tools differ" in member["detail"]
    _concierge_live(world, settings, mode="warm-transfer-say-summary")
    assert report()[0]["vapi.transfer_consult"]["status"] == "WARN"


def test_doctor_multi_mode_has_no_single_mode_check(world):  # noqa: F811
    checks, _ = report()
    assert "vapi.squad_shape" not in checks


# ── the seed ──────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_create_only_seed_adds_the_concierge_and_leaves_every_other_row_alone():
    from kb.models import AgentPrompt

    seed.seed_all()
    AgentPrompt.objects.filter(role="concierge").delete()  # a DB from before this change
    AgentPrompt.objects.filter(role="entry_router").update(first_message="Hi from the owner!", body="owner edit")
    before = {p.role: (p.body, p.first_message, p.tool_names) for p in AgentPrompt.objects.all()}
    seed.seed_all()
    after = {p.role: (p.body, p.first_message, p.tool_names) for p in AgentPrompt.objects.all()}
    assert {k: v for k, v in after.items() if k != "concierge"} == before
    row = AgentPrompt.objects.get(role="concierge")
    assert row.body == seed.concierge_body() and row.tool_names == EXPECTED_TOOLS and row.is_active
    assert row.first_message == "Hi from the owner!"  # keeps the greeting the owner already chose
    assert row.vapi_model == C.ASSISTANT_MODEL  # the same model default as every other agent


@pytest.mark.django_db
def test_refresh_resets_the_concierge_too():
    from kb.models import AgentPrompt

    seed.seed_all()
    AgentPrompt.objects.filter(role="concierge").update(body="edited", tool_names=["faq_lookup"])
    seed.seed_all(refresh=True)
    row = AgentPrompt.objects.get(role="concierge")
    assert row.body == seed.concierge_body() and row.tool_names == EXPECTED_TOOLS
    assert row.first_message == seed.ENTRY_FIRST_MESSAGE
