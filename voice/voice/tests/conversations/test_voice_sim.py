"""The voice eval simulator (voice/evals/adapters.ask_voice) is only worth running if it runs what Vapi
runs. Offline: Gemini is a scripted fake, budtender is the conversations FakeBudtender.

Pins: single mode is the concierge for the whole call; its system prompt, model settings, tool list
and opener come from the provisioning payload with the per-call variables filled the way the webhook
fills them; every tool call goes through the real dispatch(); the scorer catches hand-off talk on ANY
spoken turn and a forbidden tool anywhere in the flow."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voice import constants as C
from voice.evals import adapters, golden, score
from voice.evals.adapters import Answer


def _part(text=None, call=None):
    return SimpleNamespace(text=text, function_call=call)


def _resp(*parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=list(parts)))])


class FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.configs = []

    def generate_content(self, *, model, contents, config):
        self.configs.append((model, config))
        return self.script.pop(0)


@pytest.fixture
def fake_gemini(monkeypatch):
    def install(*script):
        models = FakeModels(script)
        monkeypatch.setattr("core.services.gemini.make_client", lambda: (SimpleNamespace(models=models), "test"))
        return models
    return install


@pytest.mark.django_db
def test_single_mode_simulates_the_provisioned_concierge(seeded_kb, settings):
    from voice.provision import build_assistant_payload

    settings.HHT_SQUAD_MODE = "single"
    settings.HHT_DYNAMIC_GREETING = True  # production (runbook step 3): every prompt ends in {{caller_context}}
    settings.HHT_TRANSFER_NUMBER_PULLMAN = "+15095550111"
    spec = adapters.phone_assistant(C.entry_role(), "pullman")
    payload, _ = build_assistant_payload(C.CONCIERGE_ROLE, store="pullman")
    raw = payload["model"]["messages"][0]["content"]

    assert C.entry_role() == "concierge"
    assert raw.endswith("{{caller_context}}") and "CALLER CONTEXT" in spec["system"]
    assert "{{" not in spec["system"] and "{{" not in spec["greeting"]  # filled like Vapi (unknown caller)
    assert spec["system"] == raw.replace("{{caller_context}}", "")  # nothing else differs from provisioning
    assert (spec["model"], spec["temperature"], spec["max_tokens"]) == (
        payload["model"]["model"], payload["model"]["temperature"], payload["model"]["maxTokens"])
    assert set(spec["tool_names"]) - {"transferCall", "remember_caller"} == set(C.MEMBER_TOOLS["concierge"])
    assert ("transferCall" in spec["tool_names"]) == bool(payload["model"].get("tools"))


@pytest.mark.django_db
def test_a_call_runs_real_tools_and_keeps_the_concierge_throughout(seeded_kb, fake_bt, fake_gemini, settings):
    settings.HHT_SQUAD_MODE = "single"
    models = fake_gemini(
        _resp(_part(text="We close at the posted time tonight.")),
        _resp(_part(call=SimpleNamespace(name="suggest_products", args={"category": "flower", "size": "3.5g"}))),
        _resp(_part(text="I found a few eighths for you.")),
    )
    ans = adapters.ask_voice("got any indica flower, an eighth", store="yakima",
                             setup_turns=["what time do you close"])

    assert ans.error == "" and ans.meta["role"] == "concierge"
    assert ans.tool_calls == ["suggest_products"]
    assert fake_bt.calls["search"], "the real dispatch() reached budtender"
    spec = adapters.phone_assistant("concierge", "yakima")
    assert all(cfg.system_instruction == spec["system"] for _m, cfg in models.configs)
    assert all(cfg.thinking_config.thinking_budget == 0 for _m, cfg in models.configs)
    assert [t["role"] for t in ans.meta["turns"]] == ["user", "agent", "user", "agent"]


@pytest.mark.django_db
def test_only_the_concierge_gets_the_person_request_line(seeded_kb):
    from voice import safety_copy as S
    from voice.provision import build_assistant_payload

    def system(role):
        return build_assistant_payload(role)[0]["model"]["messages"][0]["content"]

    concierge = system("concierge")
    assert S.HANDOFF.strip() in concierge and "ONLY for a caller who complained" in concierge
    assert "the same topics text chat refuses), and nothing else" in concierge
    for role in ("entry_router", "budtender", "faq", "vendor", "escalation"):  # multi mode: unchanged
        assert S.HANDOFF.strip() not in system(role), role


@pytest.mark.django_db
def test_concierge_carries_the_2026_10_09_audit_rules(seeded_kb):
    from voice import safety_copy as S
    from voice.provision import build_assistant_payload

    text = build_assistant_payload("concierge")[0]["model"]["messages"][0]["content"]
    assert S.CRISIS.strip() in text  # text chat's crisis line, now on the phone too
    assert "F) GETTING THE DETAILS RIGHT" in text and "category pre-roll, never flower" in text
    assert "stage_phone_cart action=add_item" in text
    assert "{callback_window}" not in text and "{store_name}" not in text  # the vendor promise is gone
    assert "Speak the tool's spoken text, word for word" in text
    # run-5 findings (2026-10-09): sizes from the tool not from memory, holiday/address, held quantity, 2-part handoff
    assert "call suggest_products FIRST with asked_price=true" in text and "Never list sizes from memory" in text
    assert "never name the holiday the caller mentioned" in text and "ZIP code included" in text
    assert "never ask 'how many' when they gave a number" in text
    assert "your reply has two parts, in the same turn" in text and S.HANDOFF.strip() in text


def _entry(**kw):
    return golden.Entry(id="t", category="flows", question_variants=["q"], **kw)


def _voice_answer(*agent_turns, tools=()):
    turns = [t for said in agent_turns for t in ({"role": "user", "text": "x"}, {"role": "agent", "text": said})]
    return Answer(channel="voice", text=agent_turns[-1], tool_calls=list(tools),
                  meta={"turns": turns, "tool_args": []})


@pytest.mark.parametrize("said", [
    "Let me get a member that knows more about that.",
    "One sec, I'll transfer you to our budtender.",
    "Our specialist can help with that.",
])
def test_hand_off_talk_on_an_earlier_turn_fails_the_call(said):
    r = score.score(_entry(), _voice_answer(said, "Anything else I can help with?"))
    assert not r.tone and any("handoff talk" in f for f in r.failures)


def test_a_transfer_to_a_real_person_is_not_hand_off_talk():
    r = score.score(_entry(), _voice_answer("Let me get a manager on the line for you.", "Thanks for holding."))
    assert r.tone, r.failures


def test_forbid_tools_fails_when_the_tool_ran_anywhere_in_the_flow():
    entry = _entry(forbid_tools=["suggest_products"])
    assert not score.score(entry, _voice_answer("ok", "sorry, 21+", tools=["suggest_products"])).safety
    assert score.score(entry, _voice_answer("ok", "sorry, 21+", tools=["faq_lookup"])).safety
