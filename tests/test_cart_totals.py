"""Cart totals — the five numbers the register prints, parsed from the live capture.

Fixture dutchie/fixtures/cart_load_v2.json is the contract. Nothing here touches
the network: the session is pinned and .post is replaced with a capture stub.
Run: pytest tests/test_cart_totals.py
"""

import json
from pathlib import Path

from dutchie.pos_register_client import PosRegisterClient, parse_cart_totals
from dutchie.session import EmployeeSession, Store

FIXTURE = Path(__file__).resolve().parents[1] / "dutchie" / "fixtures" / "cart_load_v2.json"

STORE = Store(
    name="yakima", base_url="https://bo", pos_base_url="https://pos",
    org_id=700002, lsp_id=700045, loc_id=700498, register_id=700318,
    username="u", password="p", api_key="",
)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _client(capture, resp=None):
    c = PosRegisterClient(STORE)
    c._pinned = EmployeeSession(cookie_header="ck", session_gid="SID-123", user_id=95602)

    def fake_post(path, body, **kw):
        capture.append((path, body))
        return resp.get(path) if resp and path in resp else {"Result": True, "Data": []}

    c.post = fake_post  # type: ignore[assignment]
    return c


def test_load_cart_shape():
    cap = []
    _client(cap).load_cart(747577578, 729377942)
    path, body = cap[0]
    assert path == "/api/v2/cart/load_v2"
    assert body["AcctId"] == 747577578 and body["ShipmentId"] == 729377942
    assert body["Register"] == 700318
    assert body["SessionId"] == "SID-123" and body["UserId"] == "95602"
    assert body["LspId"] == "700045" and body["LocId"] == "700498"
    # Timestamp is epoch MILLISECONDS in the capture (13 digits this century).
    assert isinstance(body["Timestamp"], int) and body["Timestamp"] > 1_000_000_000_000


def test_load_cart_register_override():
    cap = []
    _client(cap).load_cart(747577578, 729377942, register=708318)
    assert cap[0][1]["Register"] == 708318


def test_totals_match_the_register_printout():
    out = parse_cart_totals(_fixture())
    assert out["subtotal"] == 8.25
    assert out["discounts_and_loyalty"] == 0.00
    assert out["tax"] == 3.75
    assert out["rounding"] == 0.00
    assert out["total"] == 12.00
    assert out["item_count"] == 2
    # the owner's arithmetic: 8.25 + 3.75 = 12.00
    assert round(out["subtotal"] + out["tax"], 2) == out["total"]


def test_lines_carry_per_item_numbers():
    lines = parse_cart_totals(_fixture())["lines"]
    assert len(lines) == 2
    second = lines[1]
    assert second["total"] == 12.00 and second["tax"] == 3.75  # TaxAmt 3.7525 -> cents
    assert second["discount"] == 0.00 and second["loyalty"] == 125.00
    assert second["quantity"] == 1 and second["unit_price_formatted"] == "1.00"
    assert second["total_formatted"] == "12.00"
    assert second["serial_no"] == "WA413287.IN9XMT"


def test_cart_totals_end_to_end_through_the_client():
    cap = []
    c = _client(cap, {"/api/v2/cart/load_v2": _fixture()})
    out = c.cart_totals(47577578, 229377942)
    assert cap[0][0] == "/api/v2/cart/load_v2"
    assert (out["subtotal"], out["tax"], out["total"]) == (8.25, 3.75, 12.00)


def test_missing_field_is_none_not_zero():
    payload = _fixture()
    del payload["Data"]["GrandTotalRounded"]
    del payload["Data"]["GrandTotal"]
    del payload["Data"]["Tax"]
    out = parse_cart_totals(payload)
    assert out["total"] is None  # NOT 0.0 — a wrong total is worse than no total
    assert out["tax"] is None
    assert out["subtotal"] == 8.25  # the fields that are present still parse


def test_result_false_yields_none():
    assert parse_cart_totals({"Result": False, "Message": "nope", "Data": None}) is None


def test_missing_data_yields_none():
    assert parse_cart_totals({"Result": True}) is None
    assert parse_cart_totals({}) is None
    assert parse_cart_totals(None) is None


def test_cart_totals_swallows_transport_failure():
    c = PosRegisterClient(STORE)
    c._pinned = EmployeeSession(cookie_header="ck", session_gid="SID-123", user_id=95602)

    def boom(path, body, **kw):
        raise RuntimeError("Dutchie down")

    c.post = boom  # type: ignore[assignment]
    assert c.cart_totals(47577578, 229377942) is None
