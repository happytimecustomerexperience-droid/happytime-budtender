"""P3 — the entry_router classifier matrix (13-P3 §7 A; §8.1). The CENTRAL test.

Pins the §4.1 precedence on the code-owned classifier (voice/routing.py) — NOT a live LLM. The
load-bearing invariant: a VENDOR opener routes to the ``vendor`` member and NEVER to retail
(the export-#6 fix). Escalation (dispute/defective/repeated-human) OUTRANKS vendor; retail/faq
openers are unaffected (no regression). Expected values hand-authored (03-CONVENTIONS.md §5).
"""

from __future__ import annotations

import pytest

from voice import routing

# (opener, expected_intent, expected_destination)
VENDOR_OPENERS = [
    "Hi, I'm dropping off a delivery for the back.",
    "I'm a vendor with a wholesale order.",
    "I'm a distributor, here to talk to the buyer.",
    "I'm a sales rep, do you have a minute?",
    "Here's a manifest for receiving.",
    "I've got a transfer manifest and a delivery.",
    "Wholesale order question for the buyer.",
    "I have a PO to drop off.",
    "Got a purchase order for you.",
    "I have some samples — a sample drop.",
    "Invoice question, accounts payable.",
    "I'm the driver, I'm here with an order.",
    "I've got a pallet to unload for receiving.",
]

RETAIL_OPENERS = [
    "I'm looking for an indica for sleep, under $40.",
    "Can you recommend something to relax?",
    "What's good for energy and focus?",
    "I want a cartridge.",
    "I need some gummies.",
    "Something for sleep please.",
]

FAQ_OPENERS = [
    "What time do you close?",
    "Do you take cards?",
    "What are this week's specials?",
    "Where are you located?",
    "What's the purchase limit?",
    "Can I return a product?",
]

ESCALATION_OPENERS = [
    "Your last order shorted me and I want a refund.",
    "My vape cart is defective and won't fire.",
    "You charged me twice, I want my money back.",
    "I have a complaint about my last order.",
]


@pytest.mark.parametrize("opener", VENDOR_OPENERS)
def test_vendor_openers_route_to_vendor_never_retail(opener):
    """A1 (export-#6 fix): every vendor opener → intent=vendor → the vendor member, NEVER retail."""
    assert routing.classify_intent(opener) == routing.INTENT_VENDOR
    dest = routing.classify_destination(opener)
    assert dest == "vendor"
    assert dest != "budtender"  # never the retail slot-fill


@pytest.mark.parametrize("opener", ESCALATION_OPENERS)
def test_dispute_outranks_vendor(opener):
    """A2: a dispute/defective opener → escalation (precedence 1 > vendor 2), not the callback loop."""
    assert routing.classify_intent(opener) == routing.INTENT_ESCALATION
    assert routing.classify_destination(opener) == "escalation"


@pytest.mark.parametrize("opener", RETAIL_OPENERS)
def test_retail_openers_unaffected(opener):
    """A3: retail-buyer openers still route to the budtender (no regression)."""
    assert routing.classify_intent(opener) == routing.INTENT_RETAIL
    assert routing.classify_destination(opener) == "budtender"


@pytest.mark.parametrize("opener", FAQ_OPENERS)
def test_faq_openers_unaffected(opener):
    """A3: info openers still route to faq (no regression)."""
    assert routing.classify_intent(opener) == routing.INTENT_FAQ
    assert routing.classify_destination(opener) == "faq"


def test_repeated_human_request_escalates():
    """A3: two explicit human requests → escalation regardless of the opener text."""
    assert routing.classify_intent("can I just talk to a person", human_requested=2) == (
        routing.INTENT_ESCALATION
    )


def test_ambiguous_defaults_to_faq():
    """Precedence 5: an opener matching nothing falls to the safe FAQ default."""
    assert routing.classify_intent("uh, hi there") == routing.INTENT_FAQ
