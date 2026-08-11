"""`manage.py sync_inventory` must survive the stores being closed.

The 10-minute sync is gated to opening hours and returns {"skipped": "stores_closed"} outside
them. The command summed `counts.values()` with int(), so that string raised ValueError and
crashed — every time it ran while the stores were shut, including on every deploy, because
deploy-vps.sh calls it as its last step. A closed-store skip is a normal outcome.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command


def test_closed_stores_is_reported_not_a_crash(monkeypatch):
    monkeypatch.setattr(
        "budtender.management.commands.sync_inventory.sync_inventory_all",
        lambda: {"skipped": "stores_closed"},
    )
    out = StringIO()
    call_command("sync_inventory", stdout=out, stderr=StringIO())
    text = out.getvalue()
    assert "skipped" in text.lower()
    assert "not an error" in text.lower()


def test_a_normal_sync_still_reports_totals(monkeypatch):
    monkeypatch.setattr(
        "budtender.management.commands.sync_inventory.sync_inventory_all",
        lambda: {"yakima": 2500, "mount-vernon": 1100, "pullman": 1000},
    )
    out = StringIO()
    call_command("sync_inventory", stdout=out, stderr=StringIO())
    text = out.getvalue()
    assert "total: 4600" in text
    assert "yakima: 2500 products" in text


def test_a_genuinely_empty_sync_still_warns(monkeypatch):
    """The 0-product alarm must survive the skip handling — it is how an expired Dutchie key is
    noticed, and quietly losing it would be worse than the crash this fixes."""
    monkeypatch.setattr(
        "budtender.management.commands.sync_inventory.sync_inventory_all",
        lambda: {"yakima": 0, "mount-vernon": 0, "pullman": 0},
    )
    err = StringIO()
    with pytest.raises(SystemExit):
        call_command("sync_inventory", stdout=StringIO(), stderr=err)
    assert "0 products synced" in err.getvalue()
