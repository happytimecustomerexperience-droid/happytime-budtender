from voice.tools import dispatch
from voice.tools import phone_cart


class FakeBudtender:
    def __init__(self):
        self.upserts = []
        self.releases = []

    def phone_cart_upsert(self, payload):
        self.upserts.append(payload)
        return {
            "ok": True,
            "draft": {
                "draft_token": "pc-1",
                "status": "open",
                "quote": {"total": 60.0, "discounts": 10.0},
                "lines": [{"sku": "SKU-1", "quantity": 2}],
            },
        }

    def phone_cart_release(self, payload):
        self.releases.append(payload)
        return {
            "ok": True,
            "draft": {"draft_token": "pc-1", "status": "released", "quote": {}},
        }


def test_stage_phone_cart_adds_item_with_call_context(monkeypatch):
    fake = FakeBudtender()
    monkeypatch.setattr(phone_cart, "budtender", lambda: fake)

    out = dispatch(
        "stage_phone_cart",
        {"action": "add_item", "store": "yakima", "sku": "SKU-1", "quantity": 2},
        {"call_id": "call-1", "caller_number": "+15095551234", "tool_call_id": "tc-1"},
    )

    assert out["ok"] is True
    assert "updated the staged cart" in out["spoken_summary"]
    assert fake.upserts[0]["call_id"] == "call-1"
    assert fake.upserts[0]["phone"] == "+15095551234"
    assert fake.upserts[0]["audit"]["tool_call_id"] == "tc-1"


def test_stage_phone_cart_quotes_total_and_release(monkeypatch):
    fake = FakeBudtender()
    monkeypatch.setattr(phone_cart, "budtender", lambda: fake)

    quoted = dispatch(
        "stage_phone_cart",
        {"action": "quote", "store": "yakima", "draft_token": "pc-1"},
        {"call_id": "call-1"},
    )
    released = dispatch(
        "stage_phone_cart",
        {"action": "release", "store": "yakima", "draft_token": "pc-1"},
        {"call_id": "call-1"},
    )

    assert "current staged estimate is $60.00" in quoted["spoken_summary"]
    assert "released the staged phone cart" in released["spoken_summary"]
    assert fake.releases[0]["draft_token"] == "pc-1"
