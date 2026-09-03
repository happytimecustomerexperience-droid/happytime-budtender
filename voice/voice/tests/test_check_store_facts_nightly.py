"""voice.tasks.check_store_facts_nightly — the nightly public-site-vs-KB drift check
(P6). Patches ``requests.get`` (the live ``/api/refresh-constants`` fetch inside
``kb.management.commands.check_store_facts._load_site_url``) so the test stays offline.
"""

from __future__ import annotations

import pytest


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


_AGREEING_LOCATIONS = {
    "yakima": {
        "address": "1315 N 1st St, Yakima, WA 98901",
        "phone": "(509) 571-1106",
        "hours": {"monday": "8:00am–11:30pm"},
    },
    "mount-vernon": {
        "address": "200 Suzanne Ln, Mt Vernon, WA 98273",
        "phone": "(360) 488-2923",
        "hours": {"monday": "9:00am–10:00pm"},
    },
    "pullman": {
        "address": "5602 WA-270, Pullman, WA 99163",
        "phone": "(509) 334-2788",
        "hours": {"monday": "9:00am–10:00pm"},
    },
}

_AGREEING_CATALOG = {
    "yakima": {"street": "1315 N 1st St", "city": "Yakima, WA 98901", "phone": "(509) 571-1106", "hours": "8 AM–11:30 PM daily"},
    "mount-vernon": {"street": "200 Suzanne Ln", "city": "Mt Vernon, WA 98273", "phone": "(360) 488-2923", "hours": "9 AM–10 PM daily"},
    "pullman": {"street": "5602 WA-270", "city": "Pullman, WA 99163", "phone": "(509) 334-2788", "hours": "9 AM–10 PM daily"},
}


@pytest.fixture(autouse=True)
def _patch_catalog(monkeypatch):
    monkeypatch.setattr(
        "kb.management.commands.check_store_facts._load_catalog", lambda: _AGREEING_CATALOG
    )


def _seed_agreeing_store_facts():
    from kb.models import StoreFact

    for store, loc in _AGREEING_LOCATIONS.items():
        StoreFact.objects.create(store=store, kind="address", label=f"{store} address", value=loc["address"])
        StoreFact.objects.create(store=store, kind="phone", label=f"{store} phone", value=loc["phone"])
        StoreFact.objects.create(store=store, kind="hours", label=f"{store} hours", value="8 AM–11:30 PM daily" if store == "yakima" else "9 AM–10 PM daily")


@pytest.mark.django_db
def test_nightly_check_sends_no_alert_when_no_drift(monkeypatch, settings):
    from crm import sinks
    from voice import tasks

    monkeypatch.setenv("HAPPYTIME_SITE_URL", "https://happytimeweed.com")
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse({"data": {"locations": _AGREEING_LOCATIONS}}),
    )
    _seed_agreeing_store_facts()

    alerts = []
    monkeypatch.setattr(sinks, "send_staff_alert", lambda **kw: alerts.append(kw))

    result = tasks.check_store_facts_nightly()

    assert result == {"drift": False, "mismatches": 0}
    assert alerts == []


@pytest.mark.django_db
def test_nightly_check_sends_one_alert_on_drift(monkeypatch, settings):
    from crm import sinks
    from voice import tasks

    monkeypatch.setenv("HAPPYTIME_SITE_URL", "https://happytimeweed.com")
    drifted = dict(_AGREEING_LOCATIONS)
    drifted["mount-vernon"] = {**drifted["mount-vernon"], "phone": "(360) 999-9999"}
    monkeypatch.setattr(
        "requests.get", lambda *a, **k: _FakeResponse({"data": {"locations": drifted}})
    )
    _seed_agreeing_store_facts()

    alerts = []
    monkeypatch.setattr(sinks, "send_staff_alert", lambda **kw: alerts.append(kw))

    result = tasks.check_store_facts_nightly()

    assert result == {"drift": True, "mismatches": 1}
    assert len(alerts) == 1
    assert "mount-vernon" in alerts[0]["markdown_table"]
    assert "|" in alerts[0]["markdown_table"]


@pytest.mark.django_db
def test_nightly_check_skips_silently_when_site_unreachable(monkeypatch, settings):
    from crm import sinks
    from voice import tasks

    monkeypatch.delenv("HAPPYTIME_SITE_URL", raising=False)
    alerts = []
    monkeypatch.setattr(sinks, "send_staff_alert", lambda **kw: alerts.append(kw))

    result = tasks.check_store_facts_nightly()

    assert result == {}
    assert alerts == []
