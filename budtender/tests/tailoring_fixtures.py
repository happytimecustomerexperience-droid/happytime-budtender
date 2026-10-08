"""Synthetic catalogue + customer personas for the tailoring tests (customer_model / tailored ranking /
audit_customer). No real customer data: every name, phone and purchase here is invented.

The catalogue is a realistic in-stock Yakima shelf (flower tiers, pre-rolls, carts, concentrates by
extraction, ratio and THC-only edibles, tinctures, a topical) plus a few sold-out rows a persona bought
before (they must never come back as a pick). Margins are set so the margin-first order differs from
what each persona actually buys: a tailored result that matches the persona is the tailoring at work.

Personas (purchase_history rows in the exact shape tasks._fold_history writes):
  ratio   CBD-ratio gummy buyer: 1:1 and 2:1 10mg gummies, a 1:1 chocolate and tincture
  conn    flower connoisseur: top-tier, high-THC eighths, an infused pre-roll
  rosin   concentrate explorer: live rosin badder/jam, live rosin cart, a live resin sauce
  budget  value shopper: cheapest eighths and singles, a 5mg gummy
  new     brand-new customer (no history)
  low     two purchases only (confidence "low": must rank exactly as before tailoring)
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from budtender.models import BatchLab, CustomerProfile, Product, ProductDetail

LOC = "yakima"

# (sku, name, category, brand, strain_type, thc, price, margin, qty, extra)
CATALOGUE = [
    # flower
    ("F1", "Value Haze 3.5g", "flower", "ValueCo", "sativa", 17, 15, 7, 40, {"unit_weight": 3.5, "price_z": -1.0, "bucket": "traffic"}),
    ("F2", "Budget Kush 3.5g", "flower", "ValueCo", "indica", 19, 18, 8, 40, {"unit_weight": 3.5, "price_z": -0.8, "bucket": "traffic"}),
    ("F3", "Mid Cookies 3.5g", "flower", "MidFarm", "hybrid", 22, 30, 13, 30, {"unit_weight": 3.5, "price_z": 0.0}),
    ("F4", "Mid Gelato 3.5g", "flower", "MidFarm", "hybrid", 24, 35, 16, 30, {"unit_weight": 3.5, "price_z": 0.2}),
    ("F5", "Top Runtz 3.5g", "flower", "Phat Panda", "hybrid", 29, 55, 20, 20, {"unit_weight": 3.5, "price_z": 1.0, "bucket": "profit"}),
    ("F6", "Top Zkittlez 3.5g", "flower", "Top Shelf Co", "indica", 31, 60, 21, 20, {"unit_weight": 3.5, "price_z": 1.2, "bucket": "profit"}),
    ("F7", "Margin Kings Wedding Cake 3.5g", "flower", "Margin Kings", "hybrid", 20, 40, 32, 25, {"unit_weight": 3.5, "price_z": 0.4, "bucket": "profit", "velocity": 3.0}),
    ("F8", "Top Ounce Blend 28g", "flower", "Bulk Bros", "hybrid", 26, 180, 60, 10, {"unit_weight": 28, "price_z": 0.1}),
    ("F9", "ACDC CBD Flower 3.5g", "flower", "Hemp Hollow", "cbd", 1, 25, 10, 15, {"unit_weight": 3.5, "price_z": 0.0}),
    ("F10", "Top Gas Mints 3.5g", "flower", "Phat Panda", "indica", 32, 58, 19, 2, {"unit_weight": 3.5, "price_z": 1.1}),  # sold out
    # pre-rolls
    ("PR1", "Classic Pre-Roll 1g", "pre-rolls", "ValueCo", "hybrid", 20, 8, 3, 50, {"unit_weight": 1.0}),
    ("PR2", "Diamond Infused Pre-Roll 1g", "pre-rolls", "Phat Panda", "hybrid", 38, 16, 7, 30, {"unit_weight": 1.0}),
    ("PR3", "Mini Joints 5pk", "pre-rolls", "MidFarm", "sativa", 22, 20, 9, 30, {"unit_weight": 2.5}),
    # vapes
    ("V1", "Distillate Cartridge 1g", "vape-cartridges", "Cloud Co", "hybrid", 88, 25, 15, 30, {"unit_weight": 1.0}),
    ("V2", "Live Resin Cartridge 1g", "vape-cartridges", "Dabstract", "indica", 80, 35, 14, 30, {"unit_weight": 1.0}),
    ("V3", "Live Rosin Cartridge 0.5g", "vape-cartridges", "Fresh Bros", "hybrid", 76, 45, 15, 20, {"unit_weight": 0.5}),
    # concentrates
    ("C1", "Live Rosin Badder 1g", "concentrates", "Fresh Bros", "hybrid", 72, 50, 16, 15, {"unit_weight": 1.0}),
    ("C2", "Live Hash Rosin Jam 1g", "concentrates", "Rosin Lab", "indica", 75, 60, 18, 15, {"unit_weight": 1.0}),
    ("C3", "Live Resin Sauce 1g", "concentrates", "Dabstract", "hybrid", 78, 30, 12, 20, {"unit_weight": 1.0}),
    ("C4", "Glass Shatter 1g", "concentrates", "Cloud Co", "sativa", 80, 15, 10, 40, {"unit_weight": 1.0}),
    ("C5", "Distillate Syringe 1g", "concentrates", "Cloud Co", "hybrid", 90, 22, 17, 40, {"unit_weight": 1.0, "velocity": 2.0}),
    ("C6", "Cured Resin Sugar 1g", "concentrates", "Dabstract", "sativa", 74, 25, 11, 20, {"unit_weight": 1.0}),
    # edibles
    ("E1", "Verdelux 1:1 Gummies 10mg 10pk", "edibles", "Verdelux", "hybrid", None, 20, 7, 30, {"potency_mg": 100}),
    ("E2", "Verdelux 2:1 THC:CBD Gummies 10mg 10pk", "edibles", "Verdelux", "hybrid", None, 22, 8, 30, {"potency_mg": 100}),
    ("E3", "Hemp Hollow 20:1 CBD Gummies 10pk", "edibles", "Hemp Hollow", "cbd", None, 24, 9, 25, {"potency_mg": 10}),
    ("E4", "Sour Blast THC Gummies 100mg 10pk", "edibles", "Blast", "hybrid", None, 18, 12, 40, {"potency_mg": 100, "velocity": 4.0}),
    ("E5", "Midnight Gummies 5mg 20pk", "edibles", "Blast", "indica", None, 15, 9, 40, {"potency_mg": 100}),
    ("E6", "Dark Chocolate Bar 100mg", "edibles", "Cocoa Co", "hybrid", None, 22, 11, 25, {"potency_mg": 100}),
    ("E7", "Cocoa Co 1:1 Chocolate Squares 10mg 10pk", "edibles", "Cocoa Co", "hybrid", None, 24, 8, 20, {"potency_mg": 100}),
    ("E8", "Live Rosin Gummies 100mg 10pk", "edibles", "Fresh Bros", "hybrid", None, 28, 10, 20, {"potency_mg": 100}),
    ("E9", "Lemon Seltzer 10mg", "edibles", "Fizz", "sativa", None, 8, 4, 40, {"potency_mg": 10}),
    ("E10", "Full Spectrum 1:1 Gummies 10mg 10pk", "edibles", "Verdelux", "hybrid", None, 26, 9, 20, {"potency_mg": 100}),
    ("E11", "Mega THC Gummies 100mg 10pk", "edibles", "Mega", "hybrid", None, 20, 14, 40, {"potency_mg": 100, "velocity": 1.0}),
    ("E12", "Verdelux 1:1 Gummies 5mg 20pk", "edibles", "Verdelux", "hybrid", None, 21, 7, 2, {"potency_mg": 100}),  # sold out
    ("B1", "Ginger Soda 10mg", "beverages", "Fizz", "hybrid", None, 7, 3, 40, {"potency_mg": 10}),
    # tinctures / topicals
    ("T1", "Balance 1:1 Tincture 300mg", "tinctures", "Verdelux", "hybrid", None, 40, 15, 15, {"potency_mg": 300}),
    ("T2", "Hemp Hollow 20:1 CBD Tincture 500mg", "tinctures", "Hemp Hollow", "cbd", None, 45, 16, 15, {"potency_mg": 500}),
    ("TP1", "CBD Balm 500mg", "topicals", "Hemp Hollow", "cbd", None, 30, 12, 15, {}),
]

# Batch labs (only the fields compute_derived / the ranker read): cbd_total/thc_total.
LABS = {
    "E1": {"thc_total": 100.0, "cbd_total": 100.0},
    "E2": {"thc_total": 100.0, "cbd_total": 50.0},
    "E3": {"thc_total": 5.0, "cbd_total": 100.0},
    "E7": {"thc_total": 100.0, "cbd_total": 100.0},
    "E10": {"thc_total": 100.0, "cbd_total": 100.0},
    "F9": {"thc_total": 1.0, "cbd_total": 15.0},
    "T2": {"thc_total": 25.0, "cbd_total": 500.0},
}
DETAILS = {
    "E10": {"tags": ["Full Spectrum", "Gummies"]},
    "V2": {"tags": ["Live Resin"]},
}

PHONES = {
    "ratio": "+15095550101",
    "conn": "+15095550102",
    "rosin": "+15095550103",
    "budget": "+15095550104",
    "new": "+15095550105",
    "low": "+15095550106",
}


def make_catalogue() -> dict[str, Product]:
    out = {}
    lab_base = {"total_terpenes": None, "terpenes": [], "cbd_total": None, "thc_total": None,
                "minor_cannabinoids": [], "tested_date": None, "lab_name": None, "coa_url": None, "contaminants": {}}
    now = timezone.now()
    for i, (sku, name, cat, brand, st, thc, price, margin, qty, extra) in enumerate(CATALOGUE, start=1):
        p = Product.objects.create(
            sku=sku, product_id=f"pid-{sku}", batch_id=f"b-{sku}", location_slug=LOC, slug=f"s-{sku.lower()}",
            name=name, category=cat, brand=brand, strain_type=st, thc_percent=thc, price=price,
            cost=price - margin, margin=margin, quantity_on_hand=qty, availability=True, **extra)
        out[sku] = p
        if sku in LABS:
            BatchLab.objects.create(batch_id=p.batch_id, status="ok", checked_at=now, data={**lab_base, **LABS[sku]})
        if sku in DETAILS:
            ProductDetail.objects.create(product_id=p.product_id, status="ok", checked_at=now, data=DETAILS[sku])
    return out


def _row(p: Product, times: int, first_days: int, last_days: int, now, price=None) -> dict:
    """One purchase_history row as tasks._fold_history stores it."""
    def iso(days):
        return (now - timedelta(days=days)).isoformat()
    return {
        "product_id": p.product_id, "sku": p.sku, "product_name": p.name, "brand": p.brand,
        "category": p.category, "subcategory": p.subcategory, "strain": p.strain,
        "strain_type": p.strain_type, "dominant_terpene": p.dominant_terpene, "effects": [], "flavors": [],
        "thc_percent": p.thc_percent, "unit_weight": p.unit_weight, "potency_mg": p.potency_mg,
        "bucket": p.bucket, "price_z": float(p.price_z), "times_bought": times, "qty": float(times),
        "first_bought_at": iso(first_days), "last_bought_at": iso(last_days),
        "last_price": float(price if price is not None else p.price),
    }


# persona -> [(sku, times_bought, first_bought_days_ago, last_bought_days_ago)]
HISTORY = {
    "ratio": [("E1", 5, 70, 6), ("E2", 3, 56, 20), ("E7", 1, 42, 42), ("T1", 1, 28, 28), ("E12", 2, 84, 34)],
    "conn": [("F5", 4, 60, 5), ("F6", 3, 45, 15), ("F10", 2, 75, 30), ("PR2", 2, 45, 15)],
    "rosin": [("C1", 2, 50, 8), ("C2", 2, 36, 22), ("V3", 2, 64, 8), ("C3", 1, 64, 64)],
    "budget": [("F1", 4, 48, 4), ("F2", 3, 40, 12), ("PR1", 4, 48, 4), ("E5", 2, 26, 12)],
    "new": [],
    "low": [("F3", 1, 40, 40), ("E4", 1, 10, 10)],
}


def make_persona(key: str, catalogue: dict[str, Product]) -> CustomerProfile:
    """The profile row the nightly sync would leave: purchase_history folded, affinities recomputed
    by the real tasks.recompute_affinity (the same code path as production)."""
    from budtender import tasks

    now = timezone.now()
    hist = [_row(catalogue[s], t, f, ld, now) for s, t, f, ld in HISTORY[key]]
    prof = CustomerProfile.objects.create(phone=PHONES[key], name=f"Persona {key.title()}",
                                          purchase_history=hist, source="dutchie" if hist else "web")
    if hist:
        tasks.recompute_affinity(prof.phone)
        prof.refresh_from_db()
    return prof


def make_all():
    cat = make_catalogue()
    return cat, {k: make_persona(k, cat) for k in HISTORY}
