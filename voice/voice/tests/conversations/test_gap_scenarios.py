"""Data-driven gap scenarios (``gap_scenarios.yaml``) on the ``convo`` harness.

Each entry is a short conversation; ``expect`` is asserted on the LAST turn (or on ``expect.turn``,
a 0-based index, when an entry says otherwise). ``must``/``must_not`` are regexes over the answer;
``tools`` must all appear in that turn's tool_results; ``intent``/``grounded``/``escalated`` are the
``Turn`` properties of the same name. Header of the YAML explains the tags and fixture additions.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

SCENARIOS = yaml.safe_load(
    (pathlib.Path(__file__).with_name("gap_scenarios.yaml")).read_text(encoding="utf-8")
)


def _failures(turn, expect: dict) -> list[str]:
    out = []
    if "intent" in expect and turn.intent != expect["intent"]:
        out.append(f"intent {turn.intent!r} != {expect['intent']!r}")
    for key in ("grounded", "escalated"):
        if key in expect and getattr(turn, key) != bool(expect[key]):
            out.append(f"{key} {getattr(turn, key)} != {expect[key]}")
    missing = [t for t in expect.get("tools") or [] if t not in turn.tools]
    if missing:
        out.append(f"tools missing {missing} (got {turn.tools})")
    out += [f"must {p!r}" for p in expect.get("must") or [] if not re.search(p, turn.answer)]
    out += [f"must_not {p!r}" for p in expect.get("must_not") or [] if re.search(p, turn.answer)]
    return out


@pytest.mark.django_db
@pytest.mark.parametrize("entry", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_gap_scenario(entry, convo):
    c = convo(store=entry.get("store", "yakima"), phone=entry.get("phone", ""))
    turns = [c.say(t) for t in entry["turns"]]
    expect = entry.get("expect") or {}
    turn = turns[expect.get("turn", -1)]
    failures = _failures(turn, expect)
    assert not failures, f"{entry['id']}: {failures}\n--- transcript ---\n{c.transcript}"
