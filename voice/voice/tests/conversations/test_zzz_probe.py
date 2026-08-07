from __future__ import annotations

import pytest


def dump(t, label):
    print(f"--- {label} ---")
    print("intent:", t.intent)
    print("escalated:", t.escalated)
    print("next_action:", t.next_action)
    print("grounded:", t.grounded)
    print("tools:", t.tools)
    print("answer:", repr(t.answer))
    print("sources:", t.sources)
    print("picks:", t.pick_names)
    print("suggest_args:", t.args("suggest_products"))
    print()


@pytest.mark.django_db
def test_probe_thread13(convo, fake_bt):
    c = convo(store="pullman", phone="+15095551234")
    fake_bt.fail_search = True

    t = c.say("do you have any live resin dabs today"); dump(t, "t1")
    t = c.say("nothing at all? what about wax or hash"); dump(t, "t2")
    t = c.say("come on. is there a budtender who can actually go look at the shelf"); dump(t, "t3")
    fake_bt.fail_search = False
    searches_before = len(fake_bt.calls.get("search", []))
    t = c.say("okay, while I have you — what concentrates are actually on the shelf"); dump(t, "t4")
    print("searches_before", searches_before, "after", len(fake_bt.calls.get("search", [])))
    t = c.say("I mean rosin or wax, whatever concentrate you've got"); dump(t, "t5")
    t = c.say("okay whatever, just tell me what concentrate you have"); dump(t, "t6")
    t = c.say("seriously, what concentrate do you have in stock"); dump(t, "t7")
    print("history len:", len(c.history))
    for i, m in enumerate(c.history):
        print(i, m["role"], repr(m["content"][:60]))
