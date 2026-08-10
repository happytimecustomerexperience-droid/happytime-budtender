"""TASK 1 — the 5 live Dutchie categories chat.py couldn't route to (topicals/capsules/mints/
blunt/infused-blunt), because ``_CATEGORY_RE`` had no pattern for them, TOOL_SPECS'
suggest_products.category enum didn't list their slot keys (so ``_sanitize_args`` — see
voice/voice/tools/__init__.py — silently dropped the value even if chat.py DID derive it), and
budtender's CATEGORY_BY_SLOTKEY had no mapping either.

This test covers the chat.py + constants.py half of that chain (budtender/tests covers the
ranking.py half): the new slot keys must (a) be present in the TOOL_SPECS enum, and (b) be
what chat._category_from_text derives from the words a real caller would actually say.
"""
from __future__ import annotations

import pytest

from voice import chat
from voice.constants import TOOL_SPECS

NEW_SLOT_KEYS = {"topical", "capsule", "mint", "blunt", "infused-blunt"}


def test_tool_specs_category_enum_includes_the_new_slot_keys():
    enum = set(TOOL_SPECS["suggest_products"]["parameters"]["properties"]["category"]["enum"])
    missing = NEW_SLOT_KEYS - enum
    assert not missing, (
        f"suggest_products.category enum is missing {missing} — _sanitize_args will silently "
        "drop these values even if chat.py derives them correctly."
    )


@pytest.mark.parametrize("message,expected", [
    ("do you have any topicals", "topical"),
    ("looking for a lotion", "topical"),
    ("got any CBD balm", "topical"),
    ("I want a salve for my knee", "topical"),
    ("do you sell capsules", "capsule"),
    ("I want some pills", "capsule"),
    ("got any softgels", "capsule"),
    ("do you have mints", "mint"),
    ("I want a blunt", "blunt"),
    ("any blunts in stock", "blunt"),
    ("do you carry infused blunts", "infused-blunt"),
    ("I want an infused blunt", "infused-blunt"),
])
def test_category_from_text_routes_new_synonyms(message, expected):
    assert chat._category_from_text(message) == expected


@pytest.mark.parametrize("message,expected", [
    ("I want a cart", "cartridge"),
    ("looking for an eighth of flower", "flower"),
    ("do you have any gummies", "edible"),
    ("got any dabs or wax", "concentrate"),
    ("I want a pre-roll", "pre-roll"),
])
def test_category_from_text_existing_patterns_still_work(message, expected):
    """Regression: the new patterns must not shadow or break the originals."""
    assert chat._category_from_text(message) == expected
