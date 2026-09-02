"""kb.management.commands.check_store_facts — cross-source store-fact reconciliation.

Real drift exists between the seeded StoreFact rows and bundles/catalog.py (Mount Vernon
hours) — that's the whole point of the command, so these tests patch in a synthetic
catalog that matches the seed instead of asserting against the real (drifted) one.
"""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from kb.seed import seed_all

# A synthetic catalog that agrees with kb.seed.STORE_FACT_ROWS exactly, so the
# "everything agrees" test isn't tripped up by the real, known Mount Vernon drift.
_AGREEING_CATALOG = {
    "yakima": {
        "street": "1315 N 1st St",
        "city": "Yakima, WA 98901",
        "phone": "(509) 571-1106",
        "hours": "8 AM–11:30 PM daily",
    },
    "mount-vernon": {
        "street": "200 Suzanne Ln",
        "city": "Mt Vernon, WA 98273",
        "phone": "(360) 488-2923",
        "hours": "9 AM–10 PM daily",
    },
    "pullman": {
        "street": "5602 WA-270",
        "city": "Pullman, WA 99163",
        "phone": "(509) 334-2788",
        "hours": "9 AM–10 PM daily",
    },
}


def _agreeing_site_locations() -> list[dict]:
    def _week(range_str: str) -> dict:
        return {d: range_str for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}

    return [
        {
            "id": "yakima",
            "address": "1315 N 1st St, Yakima, WA 98901",
            "phone": "(509) 571-1106",
            "hours": _week("8:00am–11:30pm"),
        },
        {
            "id": "mount-vernon",
            "address": "200 Suzanne Ln, Mt Vernon, WA 98273",
            "phone": "(360) 488-2923",
            "hours": _week("9:00am–10:00pm"),
        },
        {
            "id": "pullman",
            "address": "5602 WA-270, Pullman, WA 99163",
            "phone": "(509) 334-2788",
            "hours": _week("9:00am–10:00pm"),
        },
    ]


def _write_site_json(tmp_path, locations: list[dict]):
    path = tmp_path / "store-locations.json"
    path.write_text(json.dumps({"locations": locations}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _patch_catalog(monkeypatch):
    monkeypatch.setattr(
        "kb.management.commands.check_store_facts._load_catalog",
        lambda: _AGREEING_CATALOG,
    )


@pytest.mark.django_db
def test_all_sources_agree_passes(tmp_path, capsys):
    seed_all()
    site_json = _write_site_json(tmp_path, _agreeing_site_locations())

    call_command("check_store_facts", site_json=site_json)

    out = capsys.readouterr().out
    assert "MISMATCH" not in out
    assert "mount-vernon" in out


@pytest.mark.django_db
def test_site_hours_drift_fails(tmp_path, capsys):
    seed_all()
    locations = _agreeing_site_locations()
    for loc in locations:
        if loc["id"] == "mount-vernon":
            loc["hours"]["friday"] = "9:00am–11:00pm"
            loc["hours"]["saturday"] = "9:00am–11:00pm"
    site_json = _write_site_json(tmp_path, locations)

    with pytest.raises(CommandError) as exc_info:
        call_command("check_store_facts", site_json=site_json)

    message = str(exc_info.value)
    assert "mount-vernon" in message
    assert "hours" in message


@pytest.mark.django_db
def test_site_json_missing_is_skipped_not_fatal(tmp_path, monkeypatch, capsys):
    seed_all()
    monkeypatch.delenv("HAPPYTIME_SITE_ROOT", raising=False)

    # No --site-json, no env var → site column skipped, command still runs (and still
    # finds the storefact/catalog mismatch it's patched to agree on here, so it passes).
    call_command("check_store_facts")

    out = capsys.readouterr().out
    assert "site: skipped" in out
