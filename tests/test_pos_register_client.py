"""Ponytail check — register calls post the EXACT shapes confirmed by the live HAR.

Bypasses login by pinning a fake session; captures (path, body) instead of posting.
Run: pytest tests/test_pos_register_client.py
"""

from dutchie.pos_register_client import PosRegisterClient, map_product_row
from dutchie.session import EmployeeSession, Store

STORE = Store(
    name="yakima", base_url="https://bo", pos_base_url="https://pos",
    org_id=700002, lsp_id=700045, loc_id=700498, register_id=700318,
    username="u", password="p", api_key="",
)


def _client(capture, resp=None):
    c = PosRegisterClient(STORE)
    c._pinned = EmployeeSession(cookie_header="ck", session_gid="SID-123", user_id=95602)

    def fake_post(path, body, **kw):
        capture.append((path, body))
        return resp.get(path) if resp and path in resp else {"Result": True, "Data": []}

    c.post = fake_post  # type: ignore[assignment]
    return c


def test_checkin_guest_shape():
    cap = []
    _client(cap).checkin_guest(710000001)
    path, body = cap[0]
    assert path == "/api/v2/guest/checkin_guest"
    assert body["AcctId"] == 710000001 and body["MJStateIDNo"] == "" and body["Register"] == 0
    assert body["RoomId"] == "" and body["SessionId"] == "SID-123"
    assert body["LspId"] == "700045" and body["LocId"] == "700498"
    assert "Register" in body and body["Register"] == 0  # checkin uses Register 0


def test_guest_details_shape():
    cap = []
    _client(cap).guest_details(710000001)
    path, body = cap[0]
    assert path == "/api/v2/guest/details"
    assert body["Guest_id"] == 710000001 and body["SessionId"] == "SID-123"


def test_select_guest_shape():
    cap = []
    _client(cap).select_guest_to_register(710000001, 730000001, 720000001)
    path, body = cap[0]
    assert path == "/api/v2/guest/Select_Guest_To_Register"
    assert body["Guest_id"] == 710000001 and body["RegisterId"] == 700318
    assert body["ScheduleId"] == 730000001 and body["ShipmentId"] == 720000001
    assert "Register" not in body  # select uses RegisterId


def test_product_search_returns_list():
    cap = []
    c = _client(cap, {"/api/v2/product/product_SearchV2": {"Result": True, "Data": [{"ProductId": 1}]}})
    rows = c.product_search()
    path, body = cap[0]
    assert path == "/api/v2/product/product_SearchV2"
    assert body["Register"] == 700318 and rows == [{"ProductId": 1}]


def test_price_check_shape():
    cap = []
    _client(cap).price_check("790000000000001")
    path, body = cap[0]
    assert path == "/api/v2/inventory/price-check"
    assert body["PackageSerialNumber"] == "790000000000001"


def test_add_item_shape():
    cap = []
    item = map_product_row({"ProductId": 750000001, "BatchId": 760000001, "SerialNo": "790000000000001",
                            "ProductDesc": "1UP Cartridge", "UnitPrice": 25, "RecUnitPrice": 25,
                            "CannabisInventory": "Yes"})
    _client(cap).add_item_to_cart(710000001, 720000001, item, avail_oz=2530.1)
    path, body = cap[0]
    assert path == "/api/v2/cart/add_item_to_shopping_cart"
    assert body["AcctId"] == 710000001 and body["ShipmentId"] == 720000001
    assert body["ProductId"] == 750000001 and body["BatchId"] == 760000001
    assert body["SerialNo"] == "790000000000001" and body["AvailOz"] == 2530.1
    assert body["Cnt"] == 1 and body["QuantityItem"] is True and body["Register"] == 700318
    assert body["LoyaltyAsDiscount"] is True and body["Weight"] == 0
    # Discounts ON: Dutchie applies every auto-discount/promo + auto-price server-side.
    assert body["RunAutoDiscount"] is True and body["RunAutoPrice"] is True


def test_parse_price_check_extracts_discount():
    from dutchie.pos_register_client import PosRegisterClient as PRC
    out = PRC.parse_price_check(
        {"Result": True, "Data": {"UnitPrice": 18.0, "RecUnitPrice": 25.0,
                                  "DiscountAmount": 7.0, "TotalAvailable": 9}})
    assert out["price"] == 18.0 and out["rec_price"] == 25.0
    assert out["discount"] == 7.0 and out["available"] == 9 and out["ok"] is True
    # discount inferred from rec-vs-price when not explicit
    inferred = PRC.parse_price_check({"Data": [{"UnitPrice": 20, "RecUnitPrice": 30}]})
    assert inferred["discount"] == 10.0
    # junk degrades to all-None (caller falls back to cached price), never raises
    empty = PRC.parse_price_check({})
    assert empty["price"] is None and empty["available"] is None


def test_update_status_shape():
    cap = []
    _client(cap).update_transaction_status(720000001, "Ready for pickup")
    path, body = cap[0]
    assert path == "/api/posv3/maintenance/UpdateTransactionStatus"
    assert body["TransId"] == 720000001 and body["TransactionStatus"] == "Ready for pickup"
    assert "Register" not in body


def test_submit_cart_full_flow():
    cap = []
    resp = {
        "/api/v2/guest/checkin_guest": {"Result": True, "Data": [
            {"ShipmentId": 720000001, "ScanResult": "730000001"}]},
        "/api/v2/guest/details": {"Result": True, "Data": {"Allotment": 2530.1}},
    }
    c = _client(cap, resp)
    item = map_product_row({"ProductId": 1, "BatchId": 2, "SerialNo": "3", "UnitPrice": 5})
    res = c.submit_cart(710000001, [item])
    paths = [p for p, _ in cap]
    assert paths == [
        "/api/v2/guest/checkin_guest",
        "/api/v2/guest/details",
        "/api/v2/guest/Select_Guest_To_Register",
        "/api/v2/cart/add_item_to_shopping_cart",
        "/api/posv3/maintenance/UpdateTransactionStatus",
    ]
    assert res["shipment_id"] == 720000001 and res["schedule_id"] == 730000001
    assert res["allotment"] == 2530.1
    # add_item got the allotment as AvailOz
    add_body = dict(cap[3][1])
    assert add_body["AvailOz"] == 2530.1


def test_guest_search_shape():
    cap = []
    _client(cap).guest_search("5095550100")
    path, body = cap[0]
    assert path == "/api/v2/guest/checkin_search_by_string"
    assert body["SearchString"] == "5095550100"
    assert body["SessionId"] == "SID-123" and "Register" not in body


def test_map_product_row():
    row = {"ProductId": 3567668, "BatchId": 7548777, "SerialNo": "214", "UnitPrice": 19,
           "RecUnitPrice": 19, "ProductDescription": "Temple Ball 1g", "CannabisInventory": "Yes",
           "ProductCategory": "DOH Approved Concentrate", "BrandName": "111 RANCH"}
    it = map_product_row(row)
    assert it["ProductId"] == 3567668 and it["BatchId"] == 7548777
    assert it["ProductDesc"] == "Temple Ball 1g" and it["CannbisProduct"] == "Yes"
    assert it["Brand"] == "111 RANCH"
