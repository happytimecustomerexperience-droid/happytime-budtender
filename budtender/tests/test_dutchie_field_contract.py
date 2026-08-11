"""The Dutchie field-name contract, captured from a live response.

WHY THIS EXISTS: `budtender/dutchie.py` reads specific keys out of Dutchie's
`/reporting/inventory` payload, and until now NO test asserted those key names. If Dutchie
renamed one, every unit test would stay green (they all mock at or above this layer) and
production would silently return zero products. That is precisely the shape of the two worst
bugs found this session — `category_blocklist` and strain matching — where a test double stood
in for the real dependency and the suite passed while customers got nothing.

The fixture is field NAMES only, captured from a live 5,350-row Yakima response. No row bodies,
no cost values: the leak-guard discipline applies to fixtures too, and the contract does not need
them.

These tests are offline and make no network call. They pin the contract; they do not re-verify it
against Dutchie. To re-capture after a Dutchie API change, pull `/reporting/inventory` and refresh
`data/dutchie_reporting_inventory_contract.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

CONTRACT = json.loads(
    (Path(__file__).parent / "data" / "dutchie_reporting_inventory_contract.json").read_text(
        encoding="utf-8"
    )
)
ALL_KEYS = set(CONTRACT["all_keys"])
REQUIRED = CONTRACT["required_by_parser"]


@pytest.mark.parametrize("field", sorted(REQUIRED))
def test_every_field_the_parser_needs_exists_in_the_live_payload(field):
    """Each key `dutchie.py` reads was present in the real response."""
    assert field in ALL_KEYS, (
        f"{field!r} is read by dutchie.py but was NOT in the live /reporting/inventory payload. "
        f"Purpose: {REQUIRED[field]}"
    )


def test_there_is_no_bare_price_field():
    """A parser reaching for `price` finds nothing — the money fields are unitPrice /
    recUnitPrice / medUnitPrice. Pinned because 'price' is the obvious name to guess wrong."""
    assert "price" not in ALL_KEYS
    assert {"unitPrice", "recUnitPrice", "medUnitPrice"} <= ALL_KEYS


def test_stock_is_per_room_not_a_flat_count():
    """We sell from the Sales Floor room only. `quantityAvailable` is the all-rooms total and
    would over-promise stock (back-stock, quarantine, returns) if used as the sellable count."""
    assert "roomQuantities" in ALL_KEYS
    assert "quantityAvailable" in ALL_KEYS


def test_strain_type_is_the_field_behind_the_hyphenated_bug():
    """`strainType` carries Indica-Hybrid / Sativa-Hybrid, not just Indica / Sativa — an exact
    match against it silently skipped 66 in-stock products before the hyphen-aware fix."""
    assert "strainType" in ALL_KEYS


def test_the_parser_reads_no_field_the_payload_does_not_have():
    """Guard against the reverse drift: a key added to `required_by_parser` that Dutchie never
    sends. Keeps the contract honest in both directions."""
    unknown = sorted(set(REQUIRED) - ALL_KEYS)
    assert not unknown, f"declared as parser-required but absent from the live payload: {unknown}"


def test_cost_is_documented_as_never_leaving_the_service():
    """unitCost exists upstream and must stay inside budtender — the leak-guard contract."""
    assert "unitCost" in ALL_KEYS
    assert "NEVER" in REQUIRED["unitCost"]
    blob = json.dumps(CONTRACT)
    assert "__STRIPPED__" not in blob, "fixture should carry no row bodies at all"
