"""
The voice suite's FakeBudtender fakes ``subcategory`` as a stored strain-type field
(indica/sativa/hybrid) on its canned catalog and filters on it directly — see
voice/voice/tests/conversations/conftest.py CATALOG + FakeBudtender.search(), and
test_thread_01_everyday_shopper.py which literally asserts:

    t = c.say("cool. I want some relaxing indica flower")
    args["subcategory"] == "indica"
    assert t.picks, "the fake catalog has indica flower — a miss means slots never
                      reached search"

But voice/chat.py's ``_SUBCATEGORY_RE`` (indica|sativa|hybrid) sets the SAME
``subcategory`` slot key for ANY category, and the real ``rank_products`` treats
``subcategory`` as a HARD filter matched against ``product_subtype(name, category)``
(budtender/ranking.py) — which is keyed on TEXTURE/FORM (rosin, gummies, distillate,
infused, …), never strain type, and has NO entries at all for "flower" (flower is
documented as having no subtype split). So a real caller asking for "relaxing indica
flower" reaches rank_products with slots={"category": "flower", "subcategory": "indica"},
product_subtype() returns "" for every flower product, "" != "indica", and the HARD
filter drops the entire candidate set — zero picks — even though indica flower is
in stock. The voice test suite's fake hides this completely because it fakes
subcategory as if it were strain_type.

This is the same class of bug as the category_blocklist gap: a slot the fake
implements a filter for, that the real engine implements differently (or not at
all) for that slot value.
"""
from django.test import TestCase

from budtender.models import Product
from budtender.ranking import rank_products


def _seed_flower(location="yakima"):
    Product.objects.create(
        sku="FL-INDICA-1", location_slug=location, name="Blueberry OG 3.5g",
        category="flower", strain_type="indica",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="FL-SATIVA-1", location_slug=location, name="Sour Diesel 3.5g",
        category="flower", strain_type="sativa",
        price=32, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )


class SubcategoryStrainTypeGapTests(TestCase):
    def test_indica_flower_request_returns_the_indica_flower_in_stock(self):
        """This is the bug: voice sends subcategory='indica' for a flower ask, and
        real in-stock indica flower must come back — not an empty result."""
        _seed_flower()
        ranked = rank_products(
            "yakima",
            {"category": "flower", "subcategory": "indica"},
            None,
            limit=10,
        )
        skus = {p.sku for p, _ in ranked}
        self.assertIn(
            "FL-INDICA-1", skus,
            "real rank_products returned no indica flower for subcategory='indica' — "
            "product_subtype() has no keywords for 'flower' and never returns 'indica', "
            "so the hard subcategory filter drops all flower. The voice FakeBudtender "
            "hides this because it stores subcategory as a literal strain-type field.",
        )
        self.assertNotIn("FL-SATIVA-1", skus)
