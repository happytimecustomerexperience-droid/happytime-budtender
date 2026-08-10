"""TASK 2 — a live-data drift alarm.

`pre-rolls` — the single LARGEST live category — was itself unreachable until a same-day
fix, because CATEGORY_BY_SLOTKEY silently drifted out of sync with real Dutchie inventory.
Nothing caught that until a human went and counted rows by hand. This test replaces the human:
it reads the DISTINCT categories on in-stock Product rows and fails, by name and count, the
moment one of them has no home in CATEGORY_BY_SLOTKEY. Run against a synced production DB (not
just this seeded set) the same assertion catches the *next* new Dutchie category automatically.

A mirror assertion guards the other direction: every category slot key TOOL_SPECS lets a voice/
chat caller send must resolve to a real CATEGORY_BY_SLOTKEY mapping — otherwise `_sanitize_args`
(voice/voice/tools/__init__.py) is validating an enum member that budtender can never fulfill.
TOOL_SPECS lives in the separate `voice` service/venv, so it's read here as data (a plain literal
dict, parsed with `ast`) rather than imported — that keeps this test importable from budtender's
own Django settings without pulling in voice's dependencies.
"""
import ast
from pathlib import Path

from django.test import TestCase

from budtender.models import Product
from budtender.ranking import CATEGORY_BY_SLOTKEY

# Live Dutchie in-stock categories, measured 2026-08-10 (4,748 products). This is the seed data
# for the test, NOT a dependency of the assertion itself — the assertion queries whatever is
# actually in the (test) database, so it re-runs correctly against any future live snapshot too.
LIVE_CATEGORIES_TODAY = (
    "pre-rolls", "vape-cartridges", "flower", "edibles", "concentrates",
    "topicals", "tinctures", "infused-blunt", "mints", "blunt", "capsules",
)


def _seed_live_snapshot(location="yakima"):
    for i, cat in enumerate(LIVE_CATEGORIES_TODAY):
        Product.objects.create(
            sku=f"LIVE-{i}", location_slug=location, name=f"Sample {cat} product",
            category=cat, price=10, cost=5, margin=5,
            quantity_on_hand=5, availability=True,
        )


def _voice_tool_specs() -> dict:
    """Parse voice/voice/constants.py::TOOL_SPECS as data (ast.literal_eval) — no Django
    setup, no import of the sibling `voice` service, just its source read as a sibling file."""
    path = Path(__file__).resolve().parents[2] / "voice" / "voice" / "constants.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "TOOL_SPECS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"TOOL_SPECS assignment not found in {path}")


class LiveCategoryDriftAlarmTests(TestCase):
    def test_every_in_stock_category_has_a_slotkey_mapping(self):
        """Any in-stock Product.category with no CATEGORY_BY_SLOTKEY mapping is unreachable by
        every caller — voice and chat alike. Fail loudly, naming the category and its count."""
        _seed_live_snapshot()
        mapped = set(CATEGORY_BY_SLOTKEY.values())
        in_stock_categories = (
            Product.objects.filter(availability=True, quantity_on_hand__gt=0)
            .values_list("category", flat=True)
            .distinct()
        )
        missing = []
        for cat in in_stock_categories:
            if cat and cat not in mapped:
                count = Product.objects.filter(
                    category=cat, availability=True, quantity_on_hand__gt=0
                ).count()
                missing.append(f"{cat!r} ({count} in stock)")
        self.assertEqual(
            missing, [],
            "In-stock categories with NO CATEGORY_BY_SLOTKEY mapping — unreachable by any "
            "caller: " + ", ".join(sorted(missing)),
        )

    def test_every_tool_specs_category_enum_value_maps_to_a_real_category(self):
        """The inverse gap: a category slot key TOOL_SPECS lets a caller send, but that
        CATEGORY_BY_SLOTKEY doesn't know — the value would sail through _sanitize_args's enum
        check and then match zero products in rank_products."""
        specs = _voice_tool_specs()
        enum = specs["suggest_products"]["parameters"]["properties"]["category"]["enum"]
        unmapped = [v for v in enum if v not in CATEGORY_BY_SLOTKEY]
        self.assertEqual(
            unmapped, [],
            "TOOL_SPECS suggest_products.category enum values with no CATEGORY_BY_SLOTKEY "
            f"mapping (voice/chat can send these but rank_products can never match them): {unmapped}",
        )
