"""kb/signals.py — the P6 instant-refresh chain: a StoreFact/AgentPrompt save nudges root, and
an AgentPrompt save publishes to Vapi exactly once (through the signal, not a direct view call).

The root-notify helper is off by default under pytest (settings.HHT_NOTIFY_BUDTENDER) — these
tests opt in explicitly and patch ``voice.tasks.dispatch_budtender_notify`` so no HTTP happens.
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_store_fact_save_notifies_root_exactly_once(monkeypatch, settings):
    from kb.models import StoreFact
    from voice import tasks

    settings.HHT_NOTIFY_BUDTENDER = True
    calls = []
    monkeypatch.setattr(tasks, "dispatch_budtender_notify", lambda kind: calls.append(kind))

    sf = StoreFact.objects.create(store="yakima", kind="hours", label="Yakima hours", value="8 AM–11 PM")

    assert calls == ["store-facts"]

    sf.value = "9 AM–11 PM"
    sf.save()
    assert calls == ["store-facts", "store-facts"]


@pytest.mark.django_db
def test_store_fact_delete_notifies_root(monkeypatch, settings):
    from kb.models import StoreFact
    from voice import tasks

    settings.HHT_NOTIFY_BUDTENDER = True
    sf = StoreFact.objects.create(store="yakima", kind="phone", label="Yakima phone", value="(509) 571-1106")

    calls = []
    monkeypatch.setattr(tasks, "dispatch_budtender_notify", lambda kind: calls.append(kind))
    sf.delete()

    assert calls == ["store-facts"]


@pytest.mark.django_db
def test_agent_prompt_save_publishes_once_and_notifies_root(monkeypatch, settings):
    from dashboard import publish as publish_mod
    from kb.models import AgentPrompt
    from voice import tasks

    settings.HHT_NOTIFY_BUDTENDER = True
    publish_calls = []
    notify_calls = []
    monkeypatch.setattr(publish_mod, "auto_publish_on_save", lambda p: publish_calls.append(p.pk) or "")
    monkeypatch.setattr(tasks, "dispatch_budtender_notify", lambda kind: notify_calls.append(kind))

    p = AgentPrompt.objects.create(role="faq", body="x", is_active=True)

    assert publish_calls == [p.pk]
    assert notify_calls == ["persona"]


@pytest.mark.django_db
def test_no_root_notify_when_disabled(monkeypatch, settings):
    """Default under pytest: HHT_NOTIFY_BUDTENDER off → the real notify helper no-ops (no HTTP)."""
    from kb.models import StoreFact
    from voice import budtender_client

    called = []
    monkeypatch.setattr("requests.post", lambda *a, **k: called.append(1))
    StoreFact.objects.create(store="yakima", kind="hours", label="Yakima hours", value="8 AM–11 PM")

    assert called == []
    assert budtender_client.notify_store_facts_refresh() is False
