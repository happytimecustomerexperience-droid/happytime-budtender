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

from kb import models as m
from kb.taxonomy_source import CONCENTRATE_SUBTYPE_VALUES  # parity-anchored to budtender

VOICE_ID = "a3520a8f-226a-428d-9fcd-b0a4711a6829"  # Cartesia sonic-3 voice
VAPI_MODEL = "gpt-4.1-mini"  # ADR-010


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
        "paraphrases": ["credit card", "do you take debit", "ATM", "cash only", "how do I pay"],
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
        "answer": "All sales are final, but under Washington law (WAC 314-55-079) a defective "
        "product — like a vape cart that won't fire — can be exchanged with no time limit. "
        "Bring the original packaging with a legible lot ID and your receipt, and a team "
        "member will take care of it.",
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
        "question": "What are this week's specials? / Any deals?",
        "answer": "We run a daily deal: Flower Monday 30% off, Cyber Tuesday 30% off online, "
        "Wax Wednesday 25% off, Self-Care Thursday 25% off, and Happy Friday 30% off online.",
        "topic": "specials",
        "paraphrases": ["any deals", "what's on sale", "today's special", "discounts"],
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


# ── 2. Return policy (§8.2) — WAC 314-55-079 ──────────────────────────────────

RETURN_POLICY_BODY = (
    "All sales are final. The one exception, allowed under Washington Administrative Code "
    "WAC 314-55-079, is a defective product — for example a vape cartridge that won't fire "
    "or a malfunctioning device. A defective product may be exchanged with no time limit, "
    "provided the customer brings the original packaging with a legible lot identification "
    "number and the purchase receipt. Defective-return disputes, refunds, or any case that "
    "isn't a clear straightforward defective exchange are handed to a team member "
    "(escalation) — the agent never promises a refund or adjudicates a dispute itself. "
    "Cash-back refunds are not given; the remedy is an exchange for an equivalent product."
)


def seed_return_policy() -> int:
    m.PolicyDocument.objects.update_or_create(
        kind="return_policy",
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
    (
        "mount-vernon",
        "hours",
        "Mt Vernon hours",
        "Sunday–Thursday 9 AM–10 PM, Friday–Saturday 9 AM–11 PM",
        True,
    ),
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

# Weekly specials (store="", kind="special", one row each so "what's the Wednesday deal"
# retrieves just that row).
SPECIAL_ROWS = [
    ("Flower Monday", "Flower Monday — 30% off flower."),
    ("Cyber Tuesday", "Cyber Tuesday — 30% off online orders."),
    ("Wax Wednesday", "Wax Wednesday — 25% off concentrates/wax."),
    ("Self-Care Thursday", "Self-Care Thursday — 25% off (self-care / wellness)."),
    ("Happy Friday", "Happy Friday — 30% off online orders."),
]


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
    for label, value in SPECIAL_ROWS:
        m.StoreFact.objects.update_or_create(
            store="",
            kind="special",
            label=label,
            defaults={"value": value, "confirmed": True, "weight": 105, "is_active": True},
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
STRAIN_TYPE_ROWS = [
    (
        "indica",
        "Indica/sativa/hybrid is a general industry label — the terpene profile and "
        "your own physiology shape the experience more than the label. Never over-promise "
        "(e.g. 'indica = couch-lock'); ask about the desired effect and steer by terpene + "
        "reported effects.",
    ),
    (
        "sativa",
        "Indica/sativa/hybrid is a general industry label — the terpene profile and "
        "your own physiology shape the experience more than the label. Never over-promise; ask "
        "about the desired effect and steer by terpene + reported effects.",
    ),
    (
        "hybrid",
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
    "location. When you learn it, emit structuredData.store = yakima | mount-vernon | pullman. "
    "Confirm 21+ with a SPOKEN question when it matters (a purchase) — never 'let me peek at your "
    "ID' (you're on the phone). Keep it warm and brief; never invent a price, hour, or product."
)


# ── 16c. The budtender persona (P1) — slot-fill + leak-safe suggestions + ONE gated upsell ──
# ponytail: the JSON's per-category questionnaire is encoded as THIS squad prompt (gpt-4.1-mini
# already runs the whole flow) and mapped to the suggest_products slots the budtender backend
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
    "rosin, butane≈distillate/shatter/wax → subcategory) → PESTICIDE ('want DOH pesticide- and "
    "heavy-metal-free?' → doh_only=true if yes) → PAST WINS → QUANTITY ('a dab or stocking up?' → "
    "size) → BUDGET (their price point — you'll show one at price, one ~$5 up, one ~$10 up).\n"
    "  CARTRIDGE (a 510 / vape / disposable — NEVER a 'concentrate'): after EFFECT, ask SIZE "
    "('half-gram or full gram?' → size) and reusable 510 cart vs all-in-one disposable (AIO → "
    "subcategory 'disposable') → BUDGET. After the pick, BATTERY ('what battery do you use? we've "
    "got a budget 510, a temp-control, or an all-in-one').\n"
    "  EDIBLE → FLAVOR ('chocolate or gummies?' → subcategory) → RATIO ('THC-only, a balanced 1:1, "
    "or CBN for sleep?' → subcategory '1:1' / 'cbn'; for sleep lean effect_desired=relaxed) → PAST "
    "WINS → QUANTITY ('just trying it or stocking up?') → BUDGET. After the pick, DOSING — 'start "
    "around 5 mg, wait 30–60 minutes before more, don't redose early'.\n"
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
    "straight to our [store] team right now, and they'll follow up with you to make it right.' "
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


def seed_agent_prompts() -> int:
    rows = {
        "faq": {
            "body": FAQ_PERSONA_BODY,
            "tool_names": ["faq_lookup"],
        },
        "entry_router": {
            "body": ENTRY_ROUTER_BODY,
            "tool_names": ["faq_lookup"],
        },
        "budtender": {
            "body": BUDTENDER_BODY,
            "tool_names": ["suggest_products", "check_inventory", "pair_upsell"],
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
        m.AgentPrompt.objects.update_or_create(
            role=role,
            defaults={
                "body": data["body"],
                "vapi_model": VAPI_MODEL,
                "voice_id": VOICE_ID,
                "tool_names": data["tool_names"],
                "is_active": True,
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
        "return_policy": seed_return_policy(),
        "store_facts": seed_store_facts(),
        "vendor_facts": seed_vendor_facts(),
        "wa_limits": seed_wa_limits(),
        "weights_types": seed_weights_types(),
        "education": seed_education(),
        "blogs": seed_blogs(),
        "agent_prompts": seed_agent_prompts(),
    }
