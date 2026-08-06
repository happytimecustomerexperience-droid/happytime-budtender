"""Lab-result normalisation, driven by the two committed live payloads.

Nothing here touches the network: `dutchie.lab._client` is replaced with a stub
whose two methods return the fixtures, and the Django cache is cleared between
tests so the mapping cache never leaks a result across cases.

Run: pytest dutchie/tests/test_lab.py
"""

import json
from pathlib import Path

import pytest
from django.core.cache import cache

from dutchie import lab

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


MAPPING_PAYLOAD = _fixture("lab_field_mapping")
RESULT_PAYLOAD = _fixture("lab_result")
SEARCH_ROW = _fixture("product_search_row")


class FakeClient:
    """Stands in for LabClient. Raises on demand; never opens a socket."""

    def __init__(self, *, mapping=None, result=None, boom=""):
        self.mapping = MAPPING_PAYLOAD["Data"] if mapping is None else mapping
        self.result = RESULT_PAYLOAD["Data"] if result is None else result
        self.boom = boom
        self.batches: list[int] = []

    def get_field_mapping(self):
        if self.boom == "mapping":
            raise RuntimeError("Dutchie down")
        return self.mapping

    def load_lab_result(self, batch_id):
        self.batches.append(batch_id)
        if self.boom == "result":
            raise RuntimeError("Dutchie down")
        return self.result


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def patched(monkeypatch):
    """Install a FakeClient and hand it back so tests can assert on calls."""
    holder = {}

    def install(**kw):
        client = FakeClient(**kw)
        holder["client"] = client
        monkeypatch.setattr(lab, "_client", lambda store_key: client)
        return client

    return install


# ── field_mapping ────────────────────────────────────────────────────────────
def test_field_mapping_normalises_all_59_rows(patched):
    patched()
    m = lab.field_mapping("yakima")
    assert len(m) == 59
    assert m["CBDValue"] == {"friendly": "CBD", "unit_field": "CBDUnit",
                             "is_cannabinoid": True}
    # terpene side of the IsCannabinoid split
    assert m["LimoneneValue"]["is_cannabinoid"] is False
    # the two rows that legitimately carry no unit field
    assert m["CBDML"]["unit_field"] == "" and m["THCML"]["unit_field"] == ""


def test_field_mapping_is_cached(patched):
    client = patched()
    calls = []
    orig = client.get_field_mapping

    def counted():
        calls.append(1)
        return orig()

    client.get_field_mapping = counted
    lab.field_mapping("yakima")
    lab.field_mapping("yakima")
    lab.field_mapping("yakima")
    assert len(calls) == 1


def test_field_mapping_returns_empty_on_failure(patched):
    patched(boom="mapping")
    assert lab.field_mapping("yakima") == {}


# ── lab_result ───────────────────────────────────────────────────────────────
def test_lab_result_splits_cannabinoids_and_terpenes(patched):
    client = patched()
    out = lab.lab_result("yakima", 7548778)
    assert client.batches == [7548778]

    names = [c["name"] for c in out["cannabinoids"]]
    # Only the three populated cannabinoids survive; totals are hoisted out.
    assert names == ["THCA", "THC"]
    assert {"name": "THCA", "value": 48, "unit": "%"} in out["cannabinoids"]
    assert out["total_cannabinoids"] == {"name": "Total Cannabinoids",
                                         "value": 44.996, "unit": "%"}
    # The whole terpene panel is null in this sample -> the key is absent, not [].
    assert "terpenes" not in out
    assert "total_terpenes" not in out


def test_lab_result_drops_every_null(patched):
    patched()
    out = lab.lab_result("yakima", 7548778)
    # CBD, CBDA and every terpene are null in the payload.
    flat = out.get("cannabinoids", []) + out.get("terpenes", [])
    assert not [e for e in flat if e["name"] in ("CBD", "CBDA", "Limonene", "Linalool")]
    # Null scalars must not appear as empty keys either.
    for absent in ("harvest_date", "package_date", "expires",
                   "lab_name", "lab_license", "coa_url"):
        assert absent not in out
    assert not [k for k, v in out.items() if v in (None, "", [], {})]


def test_lab_result_keeps_the_dates_that_exist(patched):
    patched()
    out = lab.lab_result("yakima", 7548778)
    # TestedDate is null in the capture, so THCDate is the fallback.
    assert out["tested_date"] == "2026-07-24T17:39:43.283"
    assert out["sample_id"] == 5361233
    assert out["batch_id"] == 7548778


def test_cannabinoids_sorted_strongest_first(patched):
    patched()
    out = lab.lab_result("yakima", 7548778)
    values = [c["value"] for c in out["cannabinoids"]]
    assert values == sorted(values, reverse=True)


def test_unknown_unit_code_is_blank_not_guessed(patched):
    row = dict(RESULT_PAYLOAD["Data"][0])
    row["THCAUnit"] = 7  # a code we have never seen resolved
    patched(result=[row])
    out = lab.lab_result("yakima", 7548778)
    thca = [c for c in out["cannabinoids"] if c["name"] == "THCA"][0]
    assert thca["unit"] == ""  # never a fabricated unit on a potency figure
    assert thca["value"] == 48


def test_zero_is_a_measurement_not_a_null(patched):
    row = dict(RESULT_PAYLOAD["Data"][0])
    row["CBDValue"] = 0
    row["CBDUnit"] = 2
    patched(result=[row])
    out = lab.lab_result("yakima", 7548778)
    assert {"name": "CBD", "value": 0, "unit": "%"} in out["cannabinoids"]


# ── fail-soft ────────────────────────────────────────────────────────────────
def test_failed_call_returns_none(patched):
    patched(boom="result")
    assert lab.lab_result("yakima", 7548778) is None


def test_missing_mapping_returns_none(patched):
    patched(boom="mapping")
    assert lab.lab_result("yakima", 7548778) is None


def test_empty_data_returns_none(patched):
    patched(result=[])
    assert lab.lab_result("yakima", 7548778) is None


def test_row_with_no_numbers_returns_none(patched):
    """Dates without a single analyte is not a lab result worth rendering."""
    row = {k: None for k in RESULT_PAYLOAD["Data"][0]}
    row["THCDate"] = "2026-07-24T17:39:43.283"
    patched(result=[row])
    assert lab.lab_result("yakima", 7548778) is None


def test_no_batch_id_short_circuits(patched):
    client = patched()
    assert lab.lab_result("yakima", None) is None
    assert client.batches == []  # not even a mapping fetch is wasted


# ── the join key ─────────────────────────────────────────────────────────────
def test_search_row_carries_the_join_key_and_basic_potency():
    """The extra lab call is unnecessary for a plain potency badge."""
    assert SEARCH_ROW["BatchId"] == 7548778
    assert SEARCH_ROW["THCContent"] == 0.472
    assert SEARCH_ROW["THCContentUnitId"] == 2
    assert "CBDContent" in SEARCH_ROW and "TotalTerpenesContent" in SEARCH_ROW
