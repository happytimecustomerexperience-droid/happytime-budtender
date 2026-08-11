import pytest

from kb.models import FAQEntry
from voice.tools import dispatch, phone_cart, suggest


class ConversationBudtender:
    def __init__(self):
        self.search_calls = []
        self.upserts = []
        self.releases = []

    def search(self, slots, *, limit=3, phone=None, session_token=None, exclude_skus=None, location=None):
        self.search_calls.append({"slots": slots, "location": location, "phone": phone})
        return {
            "results": [{
                "rank": 1,
                "sku": "CART-1",
                "name": "Blue Dream Cart 1g",
                "brand": "Happy",
                "strain": "Blue Dream",
                "price": 30.0,
                "price_was": 35.0,
                "thc_percent": 80.0,
                "dominant_terpene": "Limonene",
                "stock_on_hand": 8,
                "dutchie_link": "/catalog/product/blue-dream-cart",
                "image_url": None,
                "why_this": "Balanced cart under budget.",
            }]
        }

    def phone_cart_upsert(self, payload):
        self.upserts.append(payload)
        return {
            "ok": True,
            "draft": {
                "draft_token": "pc-real",
                "status": "open",
                "lines": [{"sku": payload.get("sku", "CART-1"), "quantity": payload.get("quantity", 1)}],
                "quote": {"subtotal": 70.0, "discounts": 10.0, "total": 60.0},
            },
        }

    def phone_cart_release(self, payload):
        self.releases.append(payload)
        return {"ok": True, "draft": {"draft_token": "pc-real", "status": "released", "quote": {}}}


@pytest.mark.django_db
def test_continuous_conversation_switches_product_specials_policy_cart_quote_release(monkeypatch):
    FAQEntry.objects.create(
        key="specials-yakima",
        question="What specials are available today?",
        answer="Monday flower special and cartridge deal are active today.",
        paraphrases=["discounts", "deals", "specials"],
        store="yakima",
        topic="specials",
        source_url="https://happytimeweed.com/specials",
    )
    FAQEntry.objects.create(
        key="returns-yakima",
        question="What is the return policy?",
        answer="Defective products are handled by staff under the documented return policy.",
        paraphrases=["return policy", "defective product"],
        store="yakima",
        topic="returns",
        source_url="https://happytimeweed.com/faq",
    )
    fake = ConversationBudtender()
    monkeypatch.setattr(suggest, "budtender", lambda: fake)
    monkeypatch.setattr(phone_cart, "budtender", lambda: fake)

    ctx = {
        "call_id": "call-real-script",
        "store": "yakima",
        "caller_number": "+15095551234",
        "tool_call_id": "tc-script",
    }
    product = dispatch(
        "suggest_products",
        {"store": "yakima", "category": "cartridge", "price_max": 35, "effect_desired": "middle"},
        ctx,
    )
    specials = dispatch("faq_lookup", {"query": "what discounts and specials today", "store": "yakima"}, ctx)
    policy = dispatch("faq_lookup", {"query": "what is the defective return policy", "store": "yakima"}, ctx)
    add = dispatch(
        "stage_phone_cart",
        {"action": "add_item", "store": "yakima", "sku": "CART-1", "quantity": 2},
        ctx,
    )
    quote = dispatch("stage_phone_cart", {"action": "quote", "store": "yakima", "draft_token": "pc-real"}, ctx)
    release = dispatch("stage_phone_cart", {"action": "release", "store": "yakima", "draft_token": "pc-real"}, ctx)

    assert product["picks"][0]["sku"] == "CART-1"
    assert specials["grounded"] is True and "special" in specials["answer"].lower()
    assert policy["grounded"] is True and "defective" in policy["answer"].lower()
    assert add["ok"] is True
    assert "current staged estimate is $60.00" in quote["spoken_summary"]
    assert release["draft"]["status"] == "released"
    assert fake.upserts[0]["call_id"] == "call-real-script"
    assert fake.upserts[0]["phone"] == "+15095551234"
    assert fake.releases[0]["draft_token"] == "pc-real"
