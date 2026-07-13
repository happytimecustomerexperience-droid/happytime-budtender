from types import SimpleNamespace

from django.test import SimpleTestCase

from budtender import views


class ProductSearchContractTests(SimpleTestCase):
    def test_forwards_ranking_context_without_db(self):
        calls = []
        saves = []
        profile = SimpleNamespace(phone="+15095551234")
        session = SimpleNamespace(
            customer=None,
            phone="",
            channel="questionnaire",
            save=lambda **kw: saves.append(kw),
        )

        def fake_rank(location, slots, prof, *, limit, exclude_skus, ranking_weights):
            calls.append(
                {
                    "location": location,
                    "slots": slots,
                    "profile": prof,
                    "limit": limit,
                    "exclude_skus": exclude_skus,
                    "ranking_weights": ranking_weights,
                }
            )
            return [(SimpleNamespace(sku="SKU2"), "fits your taste")]

        original_session = views.ChatSession
        original_suggested = views.SuggestedProduct
        original_profile = views._profile_for_phone
        original_stale = views.inventory_is_stale
        original_rank = views.rank_products
        original_public = views.public_product
        try:
            views.ChatSession = SimpleNamespace(
                objects=SimpleNamespace(get_or_create=lambda **kw: (session, True))
            )
            views.SuggestedProduct = SimpleNamespace(
                objects=SimpleNamespace(create=lambda **kw: None)
            )
            views._profile_for_phone = lambda phone: profile if phone == "+1 (509) 555-1234" else None
            views.inventory_is_stale = lambda location: False
            views.rank_products = fake_rank
            views.public_product = lambda product, rank, why_this: {
                "sku": product.sku,
                "rank": rank,
                "why_this": why_this,
            }

            response = views.ProductSearchView().post(
                SimpleNamespace(
                    data={
                        "slots": {"store": "pullman", "category": "flower", "price_tier": "mid"},
                        "limit": 2,
                        "phone": "+1 (509) 555-1234",
                        "session_token": "s1",
                        "exclude_skus": ["OLD1", 7],
                        "ranking_weights": {"w_known": {"affinity": 1}},
                    }
                )
            )
        finally:
            views.ChatSession = original_session
            views.SuggestedProduct = original_suggested
            views._profile_for_phone = original_profile
            views.inventory_is_stale = original_stale
            views.rank_products = original_rank
            views.public_product = original_public

        self.assertEqual(response.data["source"], "vps")
        self.assertEqual(response.data["results"][0]["sku"], "SKU2")
        self.assertEqual(calls[0]["location"], "pullman")
        self.assertEqual(calls[0]["slots"]["category"], "flower")
        self.assertIs(calls[0]["profile"], profile)
        self.assertEqual(calls[0]["limit"], 2)
        self.assertEqual(calls[0]["exclude_skus"], {"OLD1", "7"})
        self.assertEqual(calls[0]["ranking_weights"], {"w_known": {"affinity": 1}})
        self.assertIs(session.customer, profile)
        self.assertEqual(session.phone, "+15095551234")
        self.assertEqual(saves, [{"update_fields": ["customer", "phone"]}])
