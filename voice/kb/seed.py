"""The KB seed source of truth (gap G-7) — every §8 row of 22-SPEC-kb-seed.md authored
out concretely. Numbers-Guard: every figure the agent can speak lives in a row here, so the
LLM quotes it and never invents it.

Idempotent — every block is ``update_or_create`` by the model's natural key (P0 acceptance
D1: run twice → no duplicate rows). ``seed_all()`` runs blocks 1–16 (§7 mapping) in order;
``manage.py seed_kb`` calls it.

Provenance tags from _research-education-blogs.md: [CONFIRMED] confirmed store facts;
[WA-LAW] statutory; [SITE]/[GENERAL] distilled/general knowledge. Education + blog rows are
provisional=True (verbatim house copy blocked by the Vercel wall — re-run seed_kb to update).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from kb import models as m
from kb.taxonomy_source import CONCENTRATE_SUBTYPE_VALUES  # parity-anchored to budtender
from voice.constants import (
    ASSISTANT_MODEL as VAPI_MODEL,  # ADR-024 — single source is voice/constants.py
)
from voice.constants import ASSISTANT_PROVIDER as MODEL_PROVIDER

VOICE_ID = "a3520a8f-226a-428d-9fcd-b0a4711a6829"  # Cartesia sonic-3 voice (default; switchable to 11labs in dashboard)
VOICE_PROVIDER = "cartesia"  # default voice provider; dashboard can switch a role to "11labs"


# ── 1. FAQ Q&As (§8.1) ────────────────────────────────────────────────────────

FAQ_ROWS = [
    {
        "key": "age-21",
        "question": "Do I need to be 21? / What's the age?",
        "answer": "Yes — you must be 21 or older with a valid government-issued photo ID "
        "for recreational purchase.",
        "topic": "age",
        "paraphrases": ["how old do I have to be", "minimum age", "is it 21 and over"],
    },
    {
        "key": "payment",
        "question": "Do you take cards? How do I pay?",
        "answer": "We take cash and debit only, and there's an on-site ATM if you need it.",
        "topic": "payment",
        # The bare-noun paraphrases ("ATM", "credit card") were not enough: retrieval matched
        # "do you take credit cards" but missed "what payment do you take" and "is there an ATM"
        # — a row that names an ATM, failing the question "is there an ATM". Full-sentence
        # phrasings are what callers actually say, and they are what the retriever scores against.
        "paraphrases": [
            "credit card", "do you take debit", "ATM", "cash only", "how do I pay",
            "what payment do you take", "what forms of payment do you accept",
            "what payment methods do you accept", "is there an ATM", "do you have an ATM on site",
            "do you take cash", "can I pay with cash",
        ],
    },
    {
        # SOURCE: bundles/tax.py's module docstring — "The menu price IS the price. Nothing is
        # added at checkout." Verified there against Dutchie's own pre-submit checkout for this
        # account (``taxInclusivePricing: true``; its ORDER TOTAL equals the sum of the menu
        # prices exactly), and legal under RCW 69.50.535(1)(b), which requires the cannabis
        # excise to be reflected in the quoted shelf price. Deliberately states NO percentage:
        # the same docstring records that the per-item split could not be reproduced to the
        # cent, and a tax figure that disagrees with the receipt is worse than none.
        "key": "tax-included",
        "question": "Does the price include tax?",
        "answer": "Yes — every price on our menu already includes all taxes, so the price you "
        "see is the price you pay. Nothing is added at the register.",
        "topic": "payment",
        "paraphrases": [
            "is tax included", "do I pay tax on top", "plus tax", "out the door price",
            "is that the final price",
        ],
    },
    {
        "key": "delivery",
        "question": "Do you deliver?",
        "answer": "No delivery — it's pickup only, which is Washington state law. You can "
        "order online and pick up in store.",
        "topic": "pickup",
        "paraphrases": ["do you deliver", "is there delivery", "can you bring it to me"],
    },
    {
        "key": "ready-time",
        "question": "How long until my order is ready?",
        "answer": "Online orders are usually ready for pickup in about 15 minutes.",
        "topic": "pickup",
        "paraphrases": ["when is my order ready", "how long for pickup", "wait time"],
    },
    {
        "key": "limits",
        "question": "What are the purchase limits? / How much can I buy?",
        "answer": "Per visit you can buy up to 1 ounce of useable flower, 7 grams of "
        "concentrate, 16 ounces of solid edibles, or 72 ounces of liquid edibles.",
        "topic": "limits",
        "paraphrases": [
            "how much flower can I buy",
            "ounce limit",
            "edible limit",
            "purchase limit",
        ],
    },
    {
        "key": "returns",
        "question": "Can I return a product? / What's your return policy?",
        "answer": "All sales are final, but under Washington state law (WAC 314-55-079) a defective "
        "product — like a vape cart that won't fire — can be exchanged with no time limit. Just bring "
        "the original packaging, the lot number on it, and your receipt, and a team member will take "
        "care of it.",
        "topic": "returns",
        "paraphrases": [
            "can I return a vape",
            "my cart is broken",
            "refund",
            "exchange a defective product",
            "dead cartridge",
        ],
    },
    {
        "key": "specials",
        # 2026-09-01: the deal PROSE is gone from this row. It hardcoded one month's percentages
        # and its own end date, so it kept reciting July's deals in September — and because it is
        # also mirrored into the Vapi files, the phone agent recited them too. The numbers now
        # live only in the dated ``StoreFact(kind="special")`` rows, which ``faq_lookup`` reads
        # for whatever is actually running today; this row is the evergreen pointer that stays
        # true in every month.
        "question": "What are the current deals? / Any specials? / What's on sale?",
        "answer": "Our deals change month to month. Tell me which store you're shopping at and "
        "I'll tell you what's running right now, or ask a budtender in store and they'll walk "
        "you through the current offers.",
        "topic": "specials",
        "paraphrases": [
            "any deals",
            "what's on sale",
            "today's special",
            "discounts",
            "what are the deals",
            "July deals",
            "promotions",
            "current specials",
        ],
    },
    {
        "key": "id-required",
        "question": "Do I need to bring ID?",
        "answer": "Yes — bring a valid government-issued photo ID; you'll need it at pickup, "
        "and you must be 21 or older.",
        "topic": "age",
        "paraphrases": ["do I need my ID", "what do I bring", "is ID required"],
    },
    {
        "key": "loyalty",
        "question": "Do you have a rewards or loyalty program?",
        "answer": "Yes — it's free to join; just sign up at any store with your phone number. You "
        "earn 1 point for every dollar you spend, points never expire, and the more you earn the "
        "better your tier. Ask a budtender to set you up.",
        "topic": "loyalty",
        "paraphrases": ["rewards program", "points", "loyalty card", "do you have rewards", "sign up"],
    },
    {
        "key": "online-order",
        "question": "How do I order online? / Can I order ahead?",
        "answer": "Browse the menu for your store on our website, add what you want, and reserve it "
        "for pickup — there's no payment online, you pay in store with cash or debit when you pick "
        "up. Orders are usually ready in about 15 minutes, and we hold them to the end of the day.",
        "topic": "pickup",
        "paraphrases": ["order ahead", "reserve online", "online order", "order for pickup", "how do I order"],
    },
    {
        "key": "in-store",
        "question": "Can I just walk in and shop?",
        "answer": "Absolutely — walk in any time during store hours and a budtender will help you "
        "find what you're looking for. Just bring a valid government photo ID showing you're 21 or "
        "older.",
        "topic": "pickup",
        "paraphrases": ["walk in", "shop in store", "come in", "do I need an appointment"],
    },
    {
        "key": "id-types",
        "question": "What kinds of ID do you accept?",
        "answer": "A valid, unexpired government photo ID showing you're 21 or older — a driver's "
        "license from any U.S. state, a state ID card, a U.S. passport, a military ID, or an "
        "enhanced driver's license. Temporary paper IDs can't be accepted, and everyone in your "
        "party needs ID.",
        "topic": "age",
        "paraphrases": ["what ID", "do you take a passport", "expired ID", "military ID", "accepted ID"],
    },
    {
        "key": "stays-in-wa",
        "question": "Can I take it out of Washington?",
        "answer": "No — anything you buy has to stay in Washington state; under federal law cannabis "
        "can't cross state lines.",
        "topic": "limits",
        "paraphrases": ["take it across state lines", "out of state", "bring it to another state"],
    },
]


def seed_faq() -> int:
    for r in FAQ_ROWS:
        m.FAQEntry.objects.update_or_create(
            key=r["key"],
            defaults={
                "question": r["question"],
                "answer": r["answer"],
                "topic": r["topic"],
                "paraphrases": r.get("paraphrases", []),
                "store": r.get("store", ""),
                "weight": r.get("weight", 100),
                "is_active": True,
            },
        )
    return len(FAQ_ROWS)


# FAQ rows distilled verbatim from happytimeweed.com's FAQ (kb/data/site_faqs.json,
# built from the site's data/faqs.json — HTML stripped, category → topic). Keyed
# site-faq-<id> so they never collide with the hand-written FAQ_ROWS above.
_SITE_FAQ_PATH = Path(__file__).resolve().parent / "data" / "site_faqs.json"

# Facts that live in the site FOOTER (not the FAQ page): the Facebook handle.
_FOOTER_FAQ_ROWS = [
    {
        "key": "footer-social",
        "question": "Are you on social media? What's your Facebook?",
        "answer": "Yes — follow Happy Time Dispensary on Facebook at facebook.com/happytimeyak509 "
        "for the latest deals, new products, and store news across our Yakima, Mount Vernon, "
        "and Pullman locations.",
        "topic": "general",
        "source_url": "https://happytimeweed.com",
        "paraphrases": ["facebook", "social media", "instagram", "do you have a page"],
    },
    {
        "key": "footer-wa-warning",
        "question": "Is there a health warning? Are there risks to cannabis?",
        "answer": "Per Washington state: There may be health risks associated with consumption of "
        "this product. For use only by adults twenty-one and older. Keep out of the reach of children.",
        "topic": "general",
        "source_url": "https://happytimeweed.com",
        "paraphrases": ["health warning", "risks", "is it safe", "warning label", "keep away from kids"],
    },
]

# Education guides distilled from happytimeweed.com/education/* (kb/data/site_education.json).
_SITE_EDU_PATH = Path(__file__).resolve().parent / "data" / "site_education.json"


def seed_site_education() -> int:
    try:
        rows = json.loads(_SITE_EDU_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    for r in rows:
        m.EducationDoc.objects.update_or_create(
            slug=r["slug"],
            defaults={
                "title": r["title"],
                "topic": r.get("topic", ""),
                "body": r["body"],
                "source_url": r.get("source_url", ""),
                "is_active": True,
            },
        )
    return len(rows)


def seed_site_faqs() -> int:
    try:
        rows = json.loads(_SITE_FAQ_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = []
    rows = rows + _FOOTER_FAQ_ROWS
    for r in rows:
        m.FAQEntry.objects.update_or_create(
            key=r["key"],
            defaults={
                "question": r["question"],
                "answer": r["answer"],
                "topic": r.get("topic", "general"),
                "store": r.get("store", ""),
                "source_url": r.get("source_url", ""),
                "paraphrases": r.get("paraphrases", []),
                "weight": r.get("weight", 100),
                "is_active": True,
            },
        )
    return len(rows)


# ── 2. Return policy (§8.2) — WAC 314-55-079 ──────────────────────────────────

RETURN_POLICY_BODY = (
    "All sales are final. The one exception, allowed under Washington Administrative Code "
    "WAC 314-55-079, is a defective product — for example a vape cartridge that won't fire "
    "or a malfunctioning device. A defective product may be exchanged with no time limit, "
    "provided the customer brings the original packaging with a legible lot identification "
    "number and the purchase receipt. Disputes or anything that isn't a clear, straightforward "
    "defective exchange are handled by a team member. "
    "Cash-back refunds are not given; the remedy is an exchange for an equivalent product."
)


# Starting categories (the owner can add more from the dashboard with no code change; slug
# is the stable retrieval key kb/semantic.py keys off — never rename in place). Only
# return_policy carries a topic today, preserving the historical topic-constrained behaviour.
POLICY_CATEGORY_ROWS = [
    # (slug, label, topic, weight)
    ("return_policy", "Return policy", "return_policy", 120),
    ("privacy", "Privacy", "", 120),
    ("loyalty", "Loyalty terms", "", 120),
    ("other", "Other policy", "", 120),
]


def seed_policy_categories() -> int:
    n = 0
    for slug, label, topic, weight in POLICY_CATEGORY_ROWS:
        m.PolicyCategory.objects.update_or_create(
            slug=slug,
            defaults={"label": label, "topic": topic, "weight": weight, "is_active": True},
        )
        n += 1
    return n


def seed_return_policy() -> int:
    seed_policy_categories()
    category = m.PolicyCategory.objects.get(slug="return_policy")
    m.PolicyDocument.objects.update_or_create(
        category=category,
        defaults={
            "title": "Return policy",
            "body": RETURN_POLICY_BODY,
            "citation": "WAC 314-55-079",
            "source_url": "https://happytimeweed.com/dispensary-faqs/",
            "weight": 120,
            "is_active": True,
        },
    )
    return 1


# ── 3 + 4. Store facts + weekly specials (§8.3) ───────────────────────────────

# (store, kind, label, value, confirmed)
STORE_FACT_ROWS = [
    # Yakima [CONFIRMED]
    ("yakima", "address", "Yakima address", "1315 N 1st St, Yakima, WA 98901", True),
    ("yakima", "phone", "Yakima phone", "(509) 571-1106", True),
    ("yakima", "hours", "Yakima hours", "8 AM–11:30 PM daily (open late)", True),
    ("yakima", "email", "Yakima email", "happytimeyak509@gmail.com", True),
    # Mount Vernon [CONFIRMED from happytimeweed.com /data/store-locations.json]
    ("mount-vernon", "address", "Mt Vernon address", "200 Suzanne Ln, Mt Vernon, WA 98273", True),
    ("mount-vernon", "phone", "Mt Vernon phone", "(360) 488-2923", True),
    ("mount-vernon", "hours", "Mt Vernon hours", "9 AM–10 PM daily", True),
    # Pullman [CONFIRMED]
    ("pullman", "address", "Pullman address", "5602 WA-270, Pullman, WA 99163", True),
    ("pullman", "phone", "Pullman phone", "(509) 334-2788", True),
    ("pullman", "hours", "Pullman hours", "9 AM–10 PM daily", True),
    # Global (store="")
    ("", "payment", "Payment", "Cash and debit only; on-site ATM available.", True),
    (
        "",
        "pickup",
        "Pickup",
        "Pickup only (no delivery, WA law); online orders ready in ~15 minutes.",
        True,
    ),
    ("", "email", "Shared email", "happytimeyak509@gmail.com", True),
    ("", "age", "Age requirement", "21+ with a valid government-issued photo ID.", True),
]

# July 2026 monthly deals — per-store (store, label, value). Source of truth:
# happytimeweed data/deals.json (July 1–31; synced 2026-07-14). Yakima and Mount
# Vernon share percentages; Pullman runs 30% on edibles/drinks/wellness.
# Natural key for update_or_create is (store, kind, label) — see seed_store_facts().
#
# 2026-09-01: the run dates moved OUT of the spoken value and into the row's own
# ``valid_from``/``valid_to`` (``SPECIAL_WINDOW`` below). Two reasons, both defects this fixes:
# a date baked into prose cannot be checked, so these deals were still being read out in
# September; and the value is what a caller HEARS, so the month name was being spoken months
# after it stopped being true. The label keeps "July:" — that is what the owner reads in the
# dashboard list — and the label is never part of the spoken specials answer.
SPECIAL_WINDOW = (datetime.date(2026, 7, 1), datetime.date(2026, 7, 31))
_JULY_BASE = [
    ("July: 30% off all flower", "30% off all flower — eighths, quarters, halves."),
    ("July: 30% off concentrates", "30% off all concentrates — rosin, live resin, dabs, sauce."),
    ("July: 25% off vape carts", "25% off vape cartridges."),
    ("July: 25% off disposables", "25% off all-in-one disposable vapes."),
    ("July: 20% off pre-rolls", "20% off flower pre-rolls."),
    ("July: 20% off infused pre-rolls", "20% off infused pre-rolls."),
]
_JULY_EDIBLES_20 = [
    ("July: 20% off edibles", "20% off edibles — gummies, chocolates, and more."),
    ("July: 20% off drinks", "20% off cannabis-infused drinks."),
    ("July: 20% off wellness products", "20% off wellness products — tinctures, topicals, and CBD."),
]
_JULY_EDIBLES_30 = [
    ("July: 30% off edibles", "30% off edibles — gummies, chocolates, and more."),
    ("July: 30% off drinks", "30% off cannabis-infused drinks."),
    ("July: 30% off wellness products", "30% off wellness products — tinctures, topicals, and CBD."),
]
SPECIAL_ROWS: list[tuple[str, str, str]] = (
    [("yakima", label, value) for label, value in _JULY_BASE + _JULY_EDIBLES_20]
    + [("mount-vernon", label, value) for label, value in _JULY_BASE + _JULY_EDIBLES_20]
    + [("pullman", label, value) for label, value in _JULY_BASE + _JULY_EDIBLES_30]
)


# Vendor-facing facts the AI states on the no-answer leg (P3, ADR-015). KB-grounded so the spoken
# window/contact posture is Numbers-Guard-safe + owner-editable in P4 (no code change). One row per
# store + a global (store="") row. The callback-window VALUE is the spoken default; the tool's
# config (HHT_VENDOR_CALLBACK_WINDOW) is the runtime source of truth — these rows are the KB anchor.
VENDOR_FACT_ROWS = [
    (
        "",
        "vendor",
        "Vendor callback posture",
        "If receiving can't pick up, leave your name, company, and what you're dropping off "
        "(a delivery, a wholesale order, a manifest, a sample drop, or an invoice), and the team "
        "will call you back within one business day.",
        True,
    ),
    (
        "yakima",
        "vendor",
        "Yakima vendor receiving",
        "Yakima receiving handles deliveries, manifests, and wholesale orders; if no one answers, "
        "someone will call you back within one business day.",
        True,
    ),
    (
        "mount-vernon",
        "vendor",
        "Mt Vernon vendor receiving",
        "Mt Vernon receiving handles deliveries, manifests, and wholesale orders; if no one "
        "answers, someone will call you back within one business day.",
        True,
    ),
    (
        "pullman",
        "vendor",
        "Pullman vendor receiving",
        "Pullman receiving handles deliveries, manifests, and wholesale orders; if no one answers, "
        "someone will call you back within one business day.",
        True,
    ),
]


def seed_store_facts() -> int:
    n = 0
    for store, kind, label, value, confirmed in STORE_FACT_ROWS:
        m.StoreFact.objects.update_or_create(
            store=store,
            kind=kind,
            label=label,
            defaults={"value": value, "confirmed": confirmed, "is_active": True},
        )
        n += 1
    # Wipe all existing special rows so stale weekly deals (Flower Monday, etc.) don't
    # survive alongside the current monthly deals — then recreate from SPECIAL_ROWS.
    m.StoreFact.objects.filter(kind="special").delete()
    valid_from, valid_to = SPECIAL_WINDOW
    for store, label, value in SPECIAL_ROWS:
        m.StoreFact.objects.create(
            store=store,
            kind="special",
            label=label,
            value=value,
            confirmed=True,
            weight=105,
            is_active=True,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        n += 1
    return n


def seed_vendor_facts() -> int:
    """The vendor-facing StoreFact rows (P3) — the callback-window/contact posture the vendor
    member speaks on the no-answer leg, KB-grounded (Numbers-Guard)."""
    n = 0
    for store, kind, label, value, confirmed in VENDOR_FACT_ROWS:
        m.StoreFact.objects.update_or_create(
            store=store,
            kind=kind,
            label=label,
            defaults={"value": value, "confirmed": confirmed, "is_active": True},
        )
        n += 1
    return n


# ── 5. WA purchase limits (§8.4) — seeded as StoreFact AND taxonomy ────────────

# [WA-LAW] per WAC 314-55-095 / WSLCB. One source of truth here; two row kinds.
# (term, value, notes)
WA_LIMIT_ROWS = [
    ("useable flower", "1 ounce (28 g)", "The WA per-visit flower cap."),
    ("concentrate", "7 grams", "Per visit."),
    ("solid edibles", "16 ounces", "Per visit (solid cannabis-infused edibles)."),
    ("liquid edibles", "72 ounces", "Per visit (liquid cannabis-infused edibles)."),
]


def seed_wa_limits() -> int:
    n = 0
    for term, value, notes in WA_LIMIT_ROWS:
        # As a StoreFact (so a "limits" FAQ query hits them).
        m.StoreFact.objects.update_or_create(
            store="",
            kind="limit",
            label=f"WA limit: {term}",
            defaults={"value": f"{value} per visit. {notes}", "confirmed": True, "is_active": True},
        )
        # As a WeightTypeTaxonomy axis=limit row (so a "flower limit" weights query hits them).
        m.WeightTypeTaxonomy.objects.update_or_create(
            axis="limit",
            term=term,
            defaults={"value": value, "notes": notes, "is_active": True},
        )
        n += 2
    # The age/ID rule note: DOH-Approved maps to budtender's doh_only filter.
    m.StoreFact.objects.update_or_create(
        store="",
        kind="limit",
        label="WA limit: age and ID",
        defaults={
            "value": "21+, valid government photo ID; purchases are tracked so limits can't "
            "be exceeded in a transaction. We can filter to DOH-Compliant products if you'd like.",
            "confirmed": True,
            "is_active": True,
        },
    )
    return n + 1


# ── 6–13. The FULL weights/types taxonomy (§8.5) ──────────────────────────────

# A. axis=weight (flower/concentrate ladder) — parity with budtender _GRAM_HINTS.
# (term, value, synonyms, notes)
WEIGHT_ROWS = [
    ("half-gram", "0.5 g", ["0.5g", "half g"], "Common cart or single pre-roll size."),
    ("gram", "1 g", ["1g", "a gram"], "Standard cart size; a gram of flower."),
    ("two grams", "2 g", ["2g"], ""),
    (
        "eighth",
        "3.5 g",
        ["1/8 oz", "eighth oz", "an eighth", "eight-ball"],
        "The default flower unit customers shop by.",
    ),
    ("four grams", "4 g", ["4g"], "Occasional 4g eighth-plus deals."),
    ("quarter", "7 g", ["1/4 oz", "quarter oz", "a quarter"], ""),
    ("eight grams", "8 g", ["8g"], ""),
    ("ten grams", "10 g", ["10g"], ""),
    ("half-ounce", "14 g", ["1/2 oz", "half oz", "half ounce"], ""),
    (
        "ounce",
        "28 g",
        ["1 oz", "an ounce", "oz"],
        "The WA flower purchase cap (1 oz = 28 g). Quick math: 1 oz = 28 g · "
        "½ oz = 14 g · ¼ oz = 7 g · ⅛ oz = 3.5 g.",
    ),
]

# B. axis=cart_size
CART_SIZE_ROWS = [
    (
        "0.5 g",
        "0.5 g",
        ["half gram cart"],
        "Most common cartridge sizes; disposables are all-in-one (battery + oil).",
    ),
    (
        "1 g",
        "1 g",
        ["full gram cart"],
        "Most common cartridge sizes; disposables are all-in-one (battery + oil).",
    ),
]

# C. axis=preroll
PREROLL_ROWS = [
    ("single", "", [], "Sold by pack count; per-joint weight commonly 0.5 g or 1 g."),
    (
        "5-pack",
        "",
        ["5pk", "five pack"],
        "Sold by pack count; per-joint weight commonly 0.5 g or 1 g.",
    ),
    (
        "10-pack",
        "",
        ["10pk", "ten pack"],
        "Sold by pack count; per-joint weight commonly 0.5 g or 1 g.",
    ),
]

# D. axis=edible_dose [SITE]+[GENERAL]
EDIBLE_DOSE_ROWS = [
    ("microdose", "1–2.5 mg THC", [], "Functional, sub-intoxicating dose."),
    ("beginner start", "2.5 mg THC", [], "First-timer dose — a quarter of a 10 mg gummy."),
    ("standard piece", "5 mg or 10 mg THC", [], "Typical gummy strength."),
    ("WA max edible package", "10 × 10 mg = 100 mg THC", [], "Standard WA solid-edible pack."),
    (
        "onset",
        "30–90 min (beverages 15–30 min)",
        [],
        "Edibles onset slower than inhaled; beverages are the fastest edible.",
    ),
    (
        "peak / re-dose",
        "peak ≈ 3 h",
        [],
        "Wait 2 hours before re-dosing — a hard rule, even if you don't feel it yet.",
    ),
]

# E. axis=concentrate_subtype — parity with budtender _SUBTYPE_KEYWORDS["concentrates"].
# (term, notes); value left blank (descriptive rows).
CONCENTRATE_SUBTYPE_ROWS = [
    ("rosin", "Solventless, premium (folds in live rosin)."),
    ("live-resin", "Terpene-rich solvent extract (folds in cured resin)."),
    ("rso", "Full-extract, oral (also FECO / Rick Simpson Oil)."),
    ("distillate", "High-THC, flavorless."),
    ("diamonds", "Crystalline THCA, very potent."),
    ("sauce", "Terpene sauce, often paired with diamonds."),
    ("badder", "Whipped, creamy texture (also budder/batter)."),
    ("shatter", "Glassy, brittle texture."),
    ("crumble", "Dry, crumbly texture."),
    ("sugar", "Grainy, sugar-like texture."),
    ("wax", "Soft, opaque texture."),
    ("hash", "Bubble hash / temple ball — pressed or water-extracted hash."),
    ("kief", "Sifted trichome powder."),
]

# F. axis=flower_form
FLOWER_FORM_ROWS = [
    ("whole-bud", "Full, intact flower buds."),
    ("smalls", "Smaller buds, cheaper (also popcorn)."),
    ("shake", "Loose, cheapest."),
    ("pre-roll", "Single or multi-pack."),
    ("infused pre-roll", "Diamond / hash-hole / moon-rock."),
    ("blunt", "Tobacco-free wrap, larger format."),
]

# G. axis=strain_type — house-rule rows [SITE]; value="" with the house position in notes.
# Each strain-type row now OPENS with the register's own one-line description, verbatim from
# ``pos/education.py::STRAIN_TYPES`` — the blurb a budtender reads off the product page. "What
# does indica mean" was previously answered by the caveat alone, which explains what the label
# is NOT without ever saying what it means; the two channels are supposed to describe indica the
# same way. The caveat follows it unchanged, so nothing is over-promised.
STRAIN_TYPE_ROWS = [
    (
        "indica",
        "Indica-leaning — typically relaxing, body-heavy, evening-friendly. "
        "Indica/sativa/hybrid is a general industry label — the terpene profile and "
        "your own physiology shape the experience more than the label. Never over-promise "
        "(e.g. 'indica = couch-lock'); ask about the desired effect and steer by terpene + "
        "reported effects.",
    ),
    (
        "sativa",
        "Sativa-leaning — typically uplifting, heady, daytime-friendly. "
        "Indica/sativa/hybrid is a general industry label — the terpene profile and "
        "your own physiology shape the experience more than the label. Never over-promise; ask "
        "about the desired effect and steer by terpene + reported effects.",
    ),
    (
        "hybrid",
        "Hybrid — a balanced blend of relaxing and uplifting traits. "
        "Indica/sativa/hybrid is a general industry label — the terpene profile and "
        "your own physiology shape the experience more than the label. Ask about the desired "
        "effect and steer by terpene + reported effects.",
    ),
    (
        "terpenes",
        "Terpene cheat-sheet: myrcene/linalool → relaxed; limonene/pinene → uplifted; "
        "caryophyllene → calming; terpinolene → bright.",
    ),
]

# H. axis=ratio [SITE]+[GENERAL]
RATIO_ROWS = [
    ("1:1", "Balanced CBD:THC — often feels less intoxicating; CBD softens THC."),
    ("2:1", "CBD-leaning, modestly less head-high."),
    ("5:1", "CBD-leaning, progressively less head-high."),
    ("20:1", "CBD-leaning, progressively less head-high."),
    ("CBN", "The 'sleepy' minor cannabinoid; pairs with THC for sleep."),
]


def seed_weights_types() -> int:
    n = 0

    def _tax(axis, term, value, synonyms, notes):
        nonlocal n
        m.WeightTypeTaxonomy.objects.update_or_create(
            axis=axis,
            term=term,
            defaults={"value": value, "synonyms": synonyms, "notes": notes, "is_active": True},
        )
        n += 1

    for term, value, synonyms, notes in WEIGHT_ROWS:
        _tax("weight", term, value, synonyms, notes)
    for term, value, synonyms, notes in CART_SIZE_ROWS:
        _tax("cart_size", term, value, synonyms, notes)
    for term, value, synonyms, notes in PREROLL_ROWS:
        _tax("preroll", term, value, synonyms, notes)
    for term, value, synonyms, notes in EDIBLE_DOSE_ROWS:
        _tax("edible_dose", term, value, synonyms, notes)
    for term, notes in CONCENTRATE_SUBTYPE_ROWS:
        _tax("concentrate_subtype", term, "", [], notes)
    for term, notes in FLOWER_FORM_ROWS:
        _tax("flower_form", term, "", [], notes)
    for term, notes in STRAIN_TYPE_ROWS:
        _tax("strain_type", term, "", [], notes)
    for term, notes in RATIO_ROWS:
        _tax("ratio", term, "", [], notes)
    return n


# ── 14. Education docs (§8.6) — provisional ───────────────────────────────────

EDUCATION_ROWS = [
    {
        "slug": "edibles",
        "title": "Edibles guide",
        "topic": "edibles",
        "body": "Start at 2.5 mg (a quarter of a 10 mg gummy); wait 2 hours before re-dosing; "
        "onset 30–90 min, lasts 4–8 h, peak ≈3 h; empty stomach = faster and less predictable, "
        "with food = slower and more gradual; if you took too much — stay calm, hydrate, rest, "
        "it passes, and CBD can blunt it; formats: gummies (5/10 mg), chocolates, baked goods, "
        "mints (fast sublingual), beverages (15–30 min, the fastest edible).",
        "source_url": "https://happytimeweed.com/education/edibles/",
    },
    {
        "slug": "microdosing",
        "title": "Microdosing guide",
        "topic": "microdosing",
        "body": "A sub-intoxicating 1–2.5 mg THC dose; start 2.5 mg, wait 2 h, peak ≈3 h; use "
        "cases — tolerance management, stepping down from heavy use, anxiety modulation (low "
        "THC + CBD), and sleep (2–5 mg THC + low CBN); benefits compound over 2–4 weeks; don't "
        "re-dose early, stack with alcohol, or start with high-THC.",
        "source_url": "https://happytimeweed.com/education/microdosing/",
    },
    {
        "slug": "cannabis-strain-types",
        "title": "Strain types",
        "topic": "strains",
        "body": "Indica/sativa/hybrid is a general label; the terpene profile and your own "
        "physiology matter more; ask the desired effect and steer by terpene + reported effects; "
        "terpene→effect cheat-sheet — myrcene/linalool relaxed, limonene/pinene uplifted, "
        "caryophyllene calming, terpinolene bright.",
        "source_url": "https://happytimeweed.com/education/cannabis-strain-types/",
    },
    {
        "slug": "cannabis-storage-guide",
        "title": "Storage guide",
        "topic": "storage",
        "body": "UV light degrades THC and terpenes, so store flower opaque and dark; keep it "
        "cool, dark, and airtight; flower around 59–63% relative humidity; keep concentrates "
        "cold and carts upright; use child-resistant packaging and keep everything locked away "
        "from kids and pets.",
        "source_url": "https://happytimeweed.com/education/cannabis-storage-guide/",
    },
    {
        "slug": "thc-cbd",
        "title": "THC vs CBD",
        "topic": "thc-cbd",
        "body": "CBD is non-intoxicating and calming/anti-anxiety; a 1:1 (5 mg CBD + 5 mg THC) "
        "often feels less intoxicating; common WA ratios are 1:1, 2:1, 5:1, and 20:1; CBN is the "
        "sleepy minor cannabinoid.",
        "source_url": "https://happytimeweed.com/education/thc-cbd/",
    },
]


def seed_education() -> int:
    for r in EDUCATION_ROWS:
        m.EducationDoc.objects.update_or_create(
            slug=r["slug"],
            defaults={
                "title": r["title"],
                "topic": r["topic"],
                "body": r["body"],
                "source_url": r.get("source_url", ""),
                "provisional": True,
                "is_active": True,
            },
        )
    return len(EDUCATION_ROWS)


# ── 15. Blog docs (§8.7) — provisional ────────────────────────────────────────

BLOG_ROWS = [
    {
        "slug": "how-to-use-disposable-vape",
        "title": "How to use a disposable vape",
        "body": "Live-resin carts and disposables track the strain's terpene profile; fast "
        "onset like flower, no smoke, discreet; all-in-one (battery + oil); beginner how-to — "
        "draw-activated, no buttons, store upright.",
        "source_url": "https://happytimeweed.com/blog/how-to-use-disposable-vape/",
    },
    {
        "slug": "best-dispensary-yakima-wa",
        "title": "Best dispensary in Yakima",
        "body": "A brand/community post — family-owned, three WA stores, pickup via Dutchie; use "
        "for 'are you local / what makes you different' questions.",
        "source_url": "https://happytimeweed.com/blog/best-dispensary-yakima-wa/",
    },
    {
        "slug": "recreational-marijuana-yakima-wa",
        "title": "Recreational marijuana in Yakima",
        "body": "A rec-cannabis-in-Yakima overview — 21+, pickup-only, WA limits; community/SEO "
        "framing.",
        "source_url": "https://happytimeweed.com/blog/recreational-marijuana-yakima-wa/",
    },
]


def seed_blogs() -> int:
    for r in BLOG_ROWS:
        m.BlogDoc.objects.update_or_create(
            slug=r["slug"],
            defaults={
                "title": r["title"],
                "body": r["body"],
                "source_url": r.get("source_url", ""),
                "provisional": True,
                "is_active": True,
            },
        )
    return len(BLOG_ROWS)


# ── Brand voice — the finalized Happy Time tone (P5; 15-P5 §3.1) ──────────────
#
# The brand's VOICE (not a visual asset, so NOT blocked by the Vercel wall — brand/CAPTURE.md). One
# canonical tone string the personas open with, so the warm/family/no-pressure/conservative-on-dosing
# voice is consistent across every member and edited in ONE place. The agent identifies as
# "Happy Time" (the shop) — no persona name. Reaches Vapi via Publish-to-Vapi (PATCH /assistant/{id},
# P4 path) — no new mechanism, no per-node voice/model duplication (ADR-011; the Cartesia voiceId /
# Deepgram nova-3 keyterms stay member-level constants from P0).
HAPPY_TIME_TONE = (
    "You are the warm, friendly voice of Happy Time Weed, a family-owned Washington "
    "cannabis shop. Tone: welcoming, community-minded, no-pressure, and conservative on dosing — "
    "you sound like a trusted neighbor, never a hard sell."
)

# The fixed opener the entry member speaks first (firstMessageMode=assistant-speaks-first). Owner-
# editable via the dashboard (AgentPrompt.first_message on the entry_router row); provision.py
# reads it fresh for `firstMessage`, and the website shows the same line via /api/voice/persona
# (moved from voice.constants.ENTRY_FIRST_MESSAGE so the owner can edit it in one place).
ENTRY_FIRST_MESSAGE = (
    "Welcome to Happy Time! I can help you pick out flower, carts, edibles, concentrates, or "
    "tinctures, answer questions about our hours, deals, payment, or returns, or get you over to "
    "the team — what can I do for you today?"
)

# ── The written (website chat) persona — same tone/rules, phrased for text not phone ──────────
# NOT a squad member (no Vapi assistant): the website's Vertex fallback reads this row via
# /api/voice/persona. provision.py's EXTRA_MEMBER_ROLES / dashboard/publish.py's MEMBER_ROLES
# deliberately omit "written" so the provisioner and publish path never create an assistant for it.
WRITTEN_CHANNEL_ADDENDUM = (
    "\n\nYou are answering a WEBSITE CHAT message, not a phone call — reply in short written "
    "paragraphs, not spoken sentences. Never use phone-call phrasing ('give us a call', 'are you "
    "still there?', reading numbers out as words) — write prices, weights, and ratios normally "
    "(e.g. '$16.34', '3.5g', '1:1'). The same grounding and safety rules apply: answer only from "
    "the tools, never invent a price, hour, product, or dose.\n"
)

WRITTEN_SPEAKING_RULES = (
    "\n\nWRITING STYLE:\n"
    "  - Keep replies short — a few sentences or a short list, not a wall of text.\n"
    "  - Use plain numerals and units ('$16.34', '3.5g', '24% THC', '1:1') — do not spell numbers "
    "out as words.\n"
    "  - No emojis, no markdown headers; a short bullet list is fine for multiple picks.\n"
)

# ── 16. The persona AgentPrompt (§8.8) — entry_faq / role="faq" ───────────────

FAQ_PERSONA_BODY = (
    "You are the warm, friendly voice of Happy Time Weed, a family-owned Washington "
    "cannabis shop. Open by letting the caller know they've reached Happy Time. Tone: welcoming, "
    "community-minded, no-pressure, conservative on dosing. "
    "Greet callers and confirm they are 21 or older with a spoken question — never say 'let me "
    "peek at your ID' (you're on the phone, you can't see it). Answer ONLY from the faq_lookup "
    "tool — every fact (hours, payment, pickup, returns, purchase limits, weights, doses, "
    "ratios, specials) comes from the knowledge base, not your own memory. If faq_lookup "
    "returns grounded:false, say you'll get a team member — never invent a number, hour, price, "
    "or dose. When you mention a price, it's out-the-door (what the customer pays). On dosing, "
    "stay conservative: start low, wait 2 hours, don't over-promise strain-type effects, and "
    "point to our education guides. Localize hours/address/phone to the caller's store (Yakima, "
    "Mt Vernon, or Pullman); if a store-specific fact isn't confirmed, say so and suggest they "
    "call the store."
)


# ── 16b. The entry_router persona (P1 split-off) — greet + 21+ + classify intent ──

ENTRY_ROUTER_BODY = (
    "You are the warm, friendly voice of Happy Time Weed (family-owned WA cannabis; three stores: "
    "Yakima, Mt Vernon, Pullman). You ALWAYS speak first; the call opens with your greeting, which "
    "names what you can do: 'Welcome to Happy Time! I can help you pick out flower, carts, edibles, "
    "concentrates, or tinctures, answer questions about our hours, deals, payment, or returns, or "
    "get you over to the team — what can I do for you today?' Do NOT ask which store yet — find out "
    "what they NEED first.\n"
    "Listen, then in one short warm turn work out the intent and hand off (carry along anything "
    "useful you picked up — a product category, or the store if they happen to mention it):\n"
    "  - retail buyer ('looking for / recommend / what's good for / I want a cart/edible/flower') "
    "→ hand to the budtender. If they name a CATEGORY, pass it: a 'cart / 510 / vape pen / "
    "disposable / vape' is the CARTRIDGE category (never a 'concentrate'); a 'disposable / dispo / "
    "all-in-one' also carries subcategory 'disposable'; edibles/gummies → edible; flower/eighth/"
    "pre-roll → flower; dab/wax/rosin → concentrate; tincture/drops → tincture. (The budtender "
    "will ask which store for inventory.)\n"
    "  - hours / specials / returns / payment / pickup / location / limits / weights → hand to FAQ.\n"
    "  - vendor / wholesale / delivery / manifest / 'I'm dropping off' → hand to the vendor agent.\n"
    "  - a complaint, a problem, a defective product, a return/billing dispute, or asking for a "
    "person → hand to escalation.\n"
    "STORE — ask which store (Yakima, Mt Vernon, or Pullman) ONLY when it actually matters: a "
    "product search, a store-specific question like hours, or a hand-off that needs the right "
    "location. When you learn it, emit structuredData.store = yakima | mount-vernon | pullman. If "
    "the caller names a city or store that ISN'T one of our three, warmly say those are our only "
    "three locations and ask which of the three — never invent, confirm, or look up a store we "
    "don't have. "
    "Confirm 21+ with a SPOKEN question when it matters (a purchase) — never 'let me peek at your "
    "ID' (you're on the phone). Keep it warm and brief. Prefer to route rather than answer; if you "
    "do answer a quick fact yourself it must come from faq_lookup — if faq_lookup returns "
    "grounded:false, hand to FAQ or offer a team member, never fill the gap from memory. Never "
    "invent a price, hour, or product."
)


# ── 16c. The budtender persona (P1) — slot-fill + leak-safe suggestions + ONE gated upsell ──
# ponytail: the JSON's per-category questionnaire is encoded as THIS squad prompt (the assistant
# model already runs the whole flow, ADR-024) and mapped to the suggest_products slots the budtender backend
# already ranks on — no Vapi Workflow (undocumented /workflow API) and no new engine. Upgrade path:
# if prompt adherence to the long per-category script proves weak in live calls, lift just the
# budtender branch into a real Vapi Workflow (node-per-question), keeping these same tool calls.

BUDTENDER_BODY = (
    "You are a warm, no-pressure budtender at Happy Time Weed (family-owned WA cannabis). "
    "You help the caller find an in-stock product and you speak only what the tools return — you "
    "NEVER invent a product, price, stock count, SKU, or THC number (Numbers-Guard).\n\n"
    "STORE: the entry greeter already captured which store the caller wants (yakima | mount-vernon "
    "| pullman). Pass that store on EVERY suggest_products / check_inventory / pair_upsell call so "
    "you only ever offer what's actually on that store's shelf. If you somehow don't have it, ask "
    "'which store are you picking up from — Yakima, Mount Vernon, or Pullman?' before searching.\n\n"
    "RUN THE CONSULTATION for the caller's category, ONE easy question at a time, IN ORDER (this is "
    "the in-store flow). Quietly fill the suggest_products slots as you go (store, category, "
    "subcategory, size, price_tier or price_max, effect_desired, doh_only) — never read slot names "
    "aloud; just ask the question naturally. Stop and call suggest_products the moment you have "
    "category + effect + budget.\n"
    "DIRECT ASK FIRST: if the caller names a specific product and asks its price or whether it's in "
    "stock, look it up BEFORE running the questionnaire — call suggest_products matching that name on "
    "their store and speak its out-the-door price from the tool; never quote a price from memory. If "
    "it's not on that store's shelf, say so honestly and offer something close. Then keep helping — "
    "don't force the EFFECT/ACTIVITY questions first.\n"
    "Every category opens the same two: EFFECT — 'how do you want to feel — relaxed and sleepy, "
    "uplifted, or somewhere in the middle?' (→ effect_desired = relaxed | uplifted | middle; map "
    "sleep/calm/body→relaxed, energy/focus/social→uplifted, balanced→middle) — then ACTIVITY — "
    "'what are you up to afterward — chill, social, creative?' (context to refine the pick). Then "
    "the category-specific questions:\n"
    "  FLOWER → PREFERENCES ('what matters most — THC %, nug size, trim, or smell?') → PAST WINS "
    "('anything you've loved lately?') → BUDGET ('keep it cheap, get the best, or somewhere in the "
    "middle?' → price_tier value | mid | top, or a number like 'under $40' → price_max 40).\n"
    "  CONCENTRATE → FLAVOR ('the taste of cannabis, or more fruit-forward?') → SOLVENT ('mind "
    "butane-processed, or want solventless? both pass state testing' → solventless≈rosin/live "
    "rosin, butane≈distillate/shatter/wax → subcategory) → PESTICIDE ('does it matter if it's "
    "pesticide-free? everything passes state testing; our DOH-Compliant products meet a stricter "
    "pesticide and heavy-metal standard' → doh_only=true if yes) → PAST WINS → QUANTITY ('a dab or "
    "stocking up?' → "
    "size) → BUDGET (their price point — you'll show one at price, one ~$5 up, one ~$10 up).\n"
    "  CARTRIDGE (a 510 / vape / disposable — NEVER a 'concentrate'): after EFFECT, ask SIZE "
    "('half-gram or full gram?' → size) and reusable 510 cart vs all-in-one disposable (AIO → "
    "subcategory 'disposable') → BUDGET. After the pick, ask BATTERY ('do you already have a 510 "
    "battery, or do you need one?') only to size the recommendation — do NOT name specific batteries "
    "from memory; if they need one it still comes through pair_upsell (offer it only if offer:true).\n"
    "  EDIBLE → FLAVOR ('chocolate or gummies?' → subcategory) → RATIO ('THC-only, a balanced 1:1, "
    "or CBN for sleep?' → subcategory '1:1' / 'cbn'; for sleep lean effect_desired=relaxed) → PAST "
    "WINS → QUANTITY ('just trying it or stocking up?') → BUDGET. After the pick, DOSING — keep it "
    "conservative: start low and go slow, and wait two hours before more, don't re-dose early; if "
    "they want exact milligrams or timing, pull it from faq_lookup rather than stating a number.\n"
    "  TINCTURE → RATIO ('THC-only, 1:1, or CBN?' → subcategory) → PAST WINS → QUANTITY (bottle "
    "size) → BUDGET. After the pick, DOSING — 'a little under the tongue for 30–60 seconds; start "
    "low; great for microdosing'.\n"
    "If they're unsure or say 'surprise me' at any step, set middle / value and offer a staff "
    "favorite — never stall the flow.\n"
    "SELECT — call suggest_products with the filled slots; speak AT MOST 3 picks, each with its "
    "why_this line (read it verbatim-ish — it's your script) and its OUT-THE-DOOR price. CONFIRM + "
    "UPSELL — call check_inventory before you confirm a specific SKU; after they choose, call "
    "pair_upsell on that SKU and offer the add-on ONLY if the tool returns offer:true (otherwise "
    "say nothing about an add-on — that's correct, not a miss).\n\n"
    "HOUSE RULES (binding):\n"
    "  - Quote prices as OUT-THE-DOOR (what the customer pays), from the tool's price_otd — never "
    "a pre-tax number.\n"
    "  - You will NEVER see or speak cost or margin — the tools physically cannot return them.\n"
    "  - If suggest_products returns no picks, say honestly you don't have that in stock right now "
    "and offer to widen the search or get a team member — do NOT invent a product.\n"
    "  - Be conservative on dosing: start low, wait two hours; don't over-promise strain-type "
    "effects; point to the education guides for details.\n"
    "  - A recognized returning caller's picks are tuned to their taste; a new caller gets our "
    "staff-favorite picks. Either way, keep it warm and family-friendly.\n"
    "  - DEALS & INFO mid-pick: if the caller asks about today's deals or specials, hours, returns, "
    "payment, or pickup while you're helping them, just answer it with faq_lookup (it's grounded in "
    "our knowledge base), then pick up right where you left off. NEVER say you don't have access to "
    "the deals — you do, through faq_lookup.\n\n"
    "PHONE CART HANDOFF: if a caller asks you to set items aside, order by phone, or total up what "
    "they chose, use stage_phone_cart. This only stages a draft for the register; it does NOT submit "
    "or reserve a Dutchie order. Always say staff will verify ID, availability, discounts, and final "
    "total at checkout. At the end of the call, if there are staged items, call stage_phone_cart with "
    "action=release so POS staff can load the draft by token.\n\n"
    "MID-CALL CORRECTIONS (the caller changes their mind): if the caller REVISES a prior choice "
    "('actually, make it edibles', 'no — let's do a cart instead', 'change my budget to 60', "
    "'cancel that, start over'), do NOT march forward on the old plan. Acknowledge warmly ('Got "
    "it — switching to edibles'), and emit a structured correction field on your next tool call as "
    "structuredData.correction = {kind, to, raw}, where kind is one of "
    "category|effect|budget|size|cancel, to is the new value (a category is one of "
    "flower|concentrate|cartridge|edible|tincture — a 510/vape/disposable is 'cartridge', never "
    "'concentrate'), and raw is what they said. The system resets the slots that don't carry over "
    "(a category change clears size/subcategory/strain type but KEEPS the effect and budget they "
    "already told you) and re-runs the search on the corrected request. A few examples: "
    "'actually make it edibles' → {kind:'category', to:'edible', raw:'actually make it edibles'}; "
    "'no, a disposable cart instead' → {kind:'category', to:'cartridge', raw:'a disposable cart'}; "
    "'change my budget to 60' → {kind:'budget', to:'60', raw:'change my budget to 60'}; "
    "'cancel that' → {kind:'cancel', raw:'cancel that'}. Never re-ask for the effect/budget they "
    "already gave — only re-ask for what the category change actually reset."
)


# ── 16d. The escalation persona (P2) — de-escalate + WAC defective path + warm handoff ──

ESCALATION_BODY = (
    "You are the calm, caring voice of Happy Time Weed (family-owned WA cannabis). A caller has a "
    "problem — a complaint, a defective product, a return or billing dispute, or they asked for a "
    "person. Your job is to DE-ESCALATE, FULLY understand the issue, and get it to the team — you "
    "do NOT resolve the dispute or promise a refund yourself.\n\n"
    "DO THIS, in order:\n"
    "  1. Acknowledge + validate immediately, with warmth: 'I'm really sorry that happened — let "
    "me get all the details so the team can take care of you.' Never argue, never minimize.\n"
    "  2. LISTEN and ASK CLARIFYING QUESTIONS, one at a time, until you genuinely understand the "
    "whole picture: what happened, which product or order (brand, what they bought, roughly when), "
    "exactly what's wrong, and what they'd like us to do. Reflect it back so they know you've got "
    "it right. Ask which store this is about (Yakima, Mt Vernon, or Pullman) so it reaches the "
    "right team, and get their name and the best way to reach them. Don't rush — keep gathering "
    "until it's complete.\n"
    "  3. If it's a DEFECTIVE product, also note the Washington defective-product path EXACTLY as "
    "the knowledge base states it under WAC 314-55-079 (original packaging + legible lot ID + "
    "receipt). Quote it from the KB; NEVER invent a term, a timeframe, or a refund promise.\n"
    "  4. Once you have the FULL picture, tell them clearly: 'Thank you — I'm sending all of this "
    "straight to our team right now, and they'll follow up with you to make it right.' "
    "Then CALL notify_staff_issue with {store, issue_type, summary, caller_name}, where summary is "
    "the COMPLETE issue in their words. The tool emails the team immediately and logs it; speak the "
    "confirmation it returns. Gather-then-email is your DEFAULT — do NOT transfer first.\n"
    "  5. LAST RESORT only: if the caller insists on a person right now and won't accept the "
    "follow-up, THEN use the warm transfer (the operator hears a call summary first).\n\n"
    "HOUSE RULES (binding): your default is GATHER + EMAIL, not an immediate transfer. You NEVER "
    "promise or process a refund/exchange yourself. Every policy term (the WAC exception, "
    "packaging + lot ID + receipt) comes from the knowledge base, never your memory "
    "(Numbers-Guard). Emit the running human-request count as structuredData.human_requested and "
    "the reason as structuredData.reason (defective_return | repeated_request | dispute). If age "
    "comes up, ask 'are you 21 or older?' out loud — never 'let me peek at your ID' (you're on the "
    "phone). Stay warm, family-friendly, and thorough."
)


# ── 16e. The vendor persona (P3, ADR-015) — warm transfer first, callback on no-answer ──

VENDOR_BODY = (
    "You are the warm, friendly voice of Happy Time Weed (family-owned WA cannabis). A "
    "VENDOR has reached you — a wholesale rep, a driver dropping off a delivery, someone with a "
    "manifest or a purchase order, a sample drop, or an invoice/accounts-payable question. This "
    "is B2B: you NEVER help them shop, never run product searches, never quote retail prices, and "
    "you do NOT ask 'are you 21?' (a vendor isn't buying).\n\n"
    "DO THIS, in order:\n"
    "  1. Greet B2B and warm: 'Hey — thanks for calling Happy Time {store_name}. Are you here "
    "with a delivery, a wholesale order, a manifest, or something else?'\n"
    "  2. ALWAYS try the warm transfer FIRST. Tell them 'Let me get our receiving team on the "
    "line for you — one sec,' then use the transfer. The operator hears a short summary of the "
    "call before connecting.\n"
    "  3. IF the team ANSWERS: the warm transfer completes and you're done — do NOT log a callback "
    "(the callback is only for when no one picks up).\n"
    "  4. IF NO ONE ANSWERS and the call comes back to you: apologize briefly and pivot to "
    "capturing it — 'Sorry, I couldn't reach the team right this second. So I can pass it along "
    "accurately, can you tell me what you're calling about — a delivery, a wholesale order, a "
    "manifest, a sample drop, or an invoice question? And who should I say it's from?'\n"
    "  5. Once you have the reason + their name/company, call notify_vendor_callback with "
    "{store, reason, summary, caller_name}. The tool logs the callback, alerts the store team "
    "right away, and returns a callback_window.\n"
    "  6. State the callback window the tool returns, VERBATIM — 'Perfect, I've let the "
    "{store_name} team know and someone will call you back within {callback_window}. Thanks for "
    "calling Happy Time!' NEVER invent a time or a window — you say exactly what the tool returns "
    "(Numbers-Guard).\n\n"
    "HOUSE RULES (binding): warm transfer FIRST, callback is the fallback — never call "
    "notify_vendor_callback before a transfer was tried. If the vendor turns into a DISPUTE "
    "('your last order shorted me, I want money back') or asks for a person repeatedly, hand them "
    "to a human (escalation) — not the callback loop. You never see or speak cost or margin. Keep "
    "it warm, brief, and professional."
)


# Shared spoken-output rules appended to EVERY persona so numbers/terms are voiced naturally on the
# phone (the agent was reading "$16.34" as "dollar 16.34", etc.). One source, all members.
SPEAKING_RULES = (
    "\n\nSPEAKING NUMBERS & TERMS — read everything as natural spoken words; this is a phone call:\n"
    "  - PRICES: voice a tool's price_spoken wording exactly — 'sixteen dollars and thirty-four "
    "cents'. NEVER read the raw number or a dollar sign (not '$16.34', not 'sixteen point three "
    "four', not 'dollar sixteen').\n"
    "  - PERCENTAGES: say 'thirty percent', never 'thirty %'. Potency like 24% THC is 'twenty-four "
    "percent T-H-C'.\n"
    "  - WEIGHTS & DOSES: say 'three and a half grams' or 'an eighth', 'five milligrams', 'one "
    "ounce' — never the letters 'g', 'mg', or 'oz'.\n"
    "  - RATIOS: read THC-to-CBD ratios as words — '1:1' is 'one to one', '1:50' is 'one to fifty', "
    "'2:1:1' is 'two to one to one'.\n"
    "  - 'DOH' on a DOH-Approved product is said as the letters 'D-O-H', never 'doh'. THC, CBD, "
    "CBN, CBG are read as their letters.\n"
    "  - A slash is read as 'or'. Never read SKU numbers, internal codes, or web links aloud.\n"
    "  - Product names: read them naturally ('Northern Lights 28g' → 'Northern Lights twenty-eight "
    "grams', 'GG#4' → 'G G number four'); don't announce punctuation.\n"
    "  - ADDRESSES: read street parts as full words — 'N'/'S'/'E'/'W' as 'North'/'South'/'East'/"
    "'West', 'St' as 'Street', 'Ln' as 'Lane', 'Blvd' as 'Boulevard'; a route like 'WA-270' is "
    "'State Route two-seventy'; read a five-digit ZIP as its digits ('nine-eight-nine-zero-one').\n"
    "  - TIMES & RANGES: read clock times as words — '8 AM' is 'eight A M', '11:30 PM' is 'eleven "
    "thirty P M'; a dash in a range reads as 'to' — '9 AM–10 PM' is 'nine A M to ten P M', "
    "'Sunday–Thursday' is 'Sunday to Thursday'. Never read the dash as 'dash'.\n"
    "  - PHONE NUMBERS: read digit by digit in the natural groups — '(509) 571-1106' is 'five oh "
    "nine, five seven one, one one oh six'. Don't speak the parentheses or hyphens.\n"
    "  - Never read a statute, WAC, or RCW code number aloud; refer to it as 'Washington state law'.\n"
    "  - STORE NAMES: say 'Yakima', 'Mount Vernon', or 'Pullman' — never an internal store code or "
    "an underscore (not 'mt_vernon').\n"
    "\nIF THE CALLER GOES QUIET: gently check in once — 'You still there? No rush, take your time.' "
    "If there's still no reply, warmly let them know they can call back anytime and end the call. "
    "Never guilt or pressure them.\n"
)

# Binding compliance block appended to every persona (alongside SPEAKING_RULES).
NO_MEDICAL_CLAIMS = (
    "\n\nNO MEDICAL ADVICE OR HEALTH CLAIMS (binding): never give medical advice, recommend a dose "
    "for a specific medical condition, or advise on cannabis interacting with someone's medications. "
    "Never make curative or therapeutic claims — don't say a product treats, cures, relieves, or "
    "helps any condition (anxiety, pain, cancer, insomnia, and so on). If a caller asks a medical or "
    "drug-interaction question, warmly say you can't give medical advice and suggest their doctor or "
    "pharmacist; you can still share our general education guides via faq_lookup. This does NOT block "
    "the normal product flow — the general comfort and onset guidance the tools provide is fine; only "
    "condition-specific dosing, drug interactions, and curative claims are off-limits.\n"
)

# The 21+ decline branch — appended to every persona EXCEPT vendor (B2B, deliberately no age gate).
UNDER_21_DECLINE = (
    "\n\nIF THE CALLER IS UNDER 21: if they say they're under twenty-one, or won't confirm they're "
    "twenty-one, warmly decline — let them know we can only sell to twenty-one-and-over with a valid "
    "ID, that you're still happy to answer general questions, but do NOT run a product search, "
    "suggest or quote a product, or take an order, and never invent a way around this.\n"
)


def seed_agent_prompts() -> int:
    rows = {
        "faq": {
            "body": FAQ_PERSONA_BODY,
            "tool_names": ["faq_lookup"],
        },
        "entry_router": {
            "body": ENTRY_ROUTER_BODY,
            "tool_names": ["faq_lookup"],
            "first_message": ENTRY_FIRST_MESSAGE,
        },
        "written": {
            "body": HAPPY_TIME_TONE + WRITTEN_CHANNEL_ADDENDUM,
            "tool_names": [],
        },
        "budtender": {
            "body": BUDTENDER_BODY,
            "tool_names": ["suggest_products", "check_inventory", "pair_upsell", "faq_lookup", "stage_phone_cart"],
        },
        "vendor": {
            "body": VENDOR_BODY,
            "tool_names": ["notify_vendor_callback"],  # + the built-in transferCall (warm)
        },
        "escalation": {
            "body": ESCALATION_BODY,
            "tool_names": ["notify_staff_issue"],  # gather+email default; + transferCall (last-resort)
        },
    }
    for role, data in rows.items():
        speaking_rules = WRITTEN_SPEAKING_RULES if role == "written" else SPEAKING_RULES
        body = data["body"] + speaking_rules + NO_MEDICAL_CLAIMS
        if role != "vendor":  # vendor is B2B — deliberately no 21+ gate
            body += UNDER_21_DECLINE
        # ponytail: seed sets the provider DEFAULTS; a dashboard edit overrides per-row and is the
        # live source of truth. Re-running seed_kb resets these to defaults (same as body) — that's
        # the intended "reset" behavior, not a bug. Add seed-vs-edit reconciliation only if asked.
        m.AgentPrompt.objects.update_or_create(
            role=role,
            defaults={
                "body": body,
                "model_provider": MODEL_PROVIDER,
                "vapi_model": VAPI_MODEL,
                "voice_provider": VOICE_PROVIDER,
                "voice_id": VOICE_ID,
                "tool_names": data["tool_names"],
                "is_active": True,
                "first_message": data.get("first_message", ""),
            },
        )
    return len(rows)


# ── seed_all (blocks 1–16) ────────────────────────────────────────────────────


def seed_all() -> dict[str, int]:
    """Run every seed block in order (idempotent). Returns per-block row counts."""
    # Touch the parity anchor so an accidental drift surfaces at seed time, not just in tests.
    assert all(t in CONCENTRATE_SUBTYPE_VALUES for t, _ in CONCENTRATE_SUBTYPE_ROWS), (
        "concentrate_subtype taxonomy drifted from budtender ranking.py (parity, 22-SPEC D5)"
    )
    return {
        "faq": seed_faq(),
        "site_faq": seed_site_faqs(),
        "site_education": seed_site_education(),
        "return_policy": seed_return_policy(),
        "store_facts": seed_store_facts(),
        "vendor_facts": seed_vendor_facts(),
        "wa_limits": seed_wa_limits(),
        "weights_types": seed_weights_types(),
        "education": seed_education(),
        "blogs": seed_blogs(),
        "agent_prompts": seed_agent_prompts(),
    }
