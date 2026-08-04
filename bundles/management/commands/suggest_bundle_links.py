"""Pick real in-stock products for each bundle slot and print signed links.

`alpine-automations`' `build_bundle_urls` signs a link once you already know which
product ids to put in it. This is the step before that: it looks at what is actually
on the shelf right now and chooses.

Run it here rather than in alpine because this is the repo with live register access
(`pos.catalog.get_inventory`) — alpine has the campaign, not the floor.

    python manage.py suggest_bundle_links
    python manage.py suggest_bundle_links --store yakima --bundle weekend
    python manage.py suggest_bundle_links --phone 5095551212 --ttl-days 30
    python manage.py suggest_bundle_links --format csv > links.csv

Candidates are matched with the bundle's OWN `Slot.accepts()` and `resolver.in_stock()`,
never a local reimplementation — if this command and the landing page disagreed about
what fits a slot, the page would quietly substitute and the email would be advertising
something the shopper does not get.

Deepest stock first, because that is what makes a link survive its own campaign.
"""
import csv
import json
import sys

from django.core.management.base import BaseCommand, CommandError

from bundles import resolver, signing
from bundles.catalog import BUNDLES, STORES
from dutchie import stores as dutchie_stores
from pos import catalog as pos_catalog

# The apex, not budtender-api: a Next.js rewrite proxies it, and it is the on-brand
# URL that belongs in a creative. Overridable for a staging host.
DEFAULT_BASE = "https://happytimeweed.com/custom-order/"


class Command(BaseCommand):
    help = "Choose live in-stock products for each bundle slot and emit signed /custom-order links."

    def add_arguments(self, parser):
        parser.add_argument("--store", action="append", choices=sorted(STORES),
                            help="repeatable; default is all three")
        parser.add_argument("--bundle", action="append", choices=sorted(BUNDLES),
                            help="repeatable; default is all")
        parser.add_argument("--phone", default="",
                            help="stamp the link with this shopper's token (the c= param)")
        parser.add_argument("--ttl-days", type=int, default=14)
        parser.add_argument("--base", default=DEFAULT_BASE)
        parser.add_argument("--format", choices=("text", "csv", "json"), default="text")
        parser.add_argument("--min-stock", type=int, default=0,
                            help="skip products with fewer than N on hand, on top of the "
                                 "app's own in_stock() floor; raise it for a long campaign")

    def handle(self, *args, **o):
        rows = []
        for store in (o["store"] or sorted(STORES, key=lambda s: s != "yakima")):
            # get_inventory keys on the POS STORE KEY ('mtvernon'), NOT the
            # location_slug ('mount-vernon'). Skipping this translation raises at
            # best and silently serves another store's shelf at worst.
            inv = [p for p in pos_catalog.get_inventory(dutchie_stores.store_key(store))
                   if resolver.in_stock(p) and float(p.get("qty") or 0) >= o["min_stock"]
                   and float(p.get("price") or 0) > 0]
            if not inv:
                raise CommandError(f"{store}: no in-stock products came back from the register")

            for slug in (o["bundle"] or sorted(BUNDLES)):
                bundle = BUNDLES[slug]
                picks, lines, used = [], [], set()
                for slot in bundle.slots:
                    cands = sorted(
                        (p for p in inv
                         if slot.accepts(p) and str(p.get("product_id")) not in used),
                        key=lambda p: -float(p.get("qty") or 0),
                    )
                    if not cands:
                        self.stderr.write(self.style.WARNING(
                            f"{store}/{slug}: nothing on the floor fits '{slot.label}' — skipped"))
                        picks = []
                        break
                    p = cands[0]
                    used.add(str(p["product_id"]))
                    picks.append((str(p["product_id"]), slot.qty))
                    lines.append({
                        "slot": slot.label, "qty": slot.qty,
                        "product_id": str(p["product_id"]), "name": p.get("name") or "",
                        "brand": p.get("brand") or "", "size": p.get("subcategory") or "",
                        "price": round(float(p.get("price") or 0), 2),
                        "stock": int(float(p.get("qty") or 0)),
                    })
                if not picks:
                    continue

                subtotal = sum(line["price"] * line["qty"] for line in lines)
                discount = subtotal * bundle.discount_pct / 100
                rows.append({
                    "store": store, "bundle": slug, "bundle_name": bundle.name,
                    "discount_pct": bundle.discount_pct,
                    "subtotal": round(subtotal, 2), "discount": round(discount, 2),
                    "total": round(subtotal - discount, 2),
                    "lines": lines,
                    "url": signing.build_url(
                        o["base"], bundle=slug, store=store, items=picks,
                        customer_token=signing.customer_token(o["phone"]) if o["phone"] else "",
                        ttl_days=o["ttl_days"]),
                })

        if not rows:
            raise CommandError("no bundle could be filled from current stock")
        getattr(self, f"_out_{o['format']}")(rows)
        self.stderr.write(self.style.WARNING(
            "\nPrices and stock are a snapshot. The landing page re-reads the register on "
            "every open, so an item that sells out is substituted from the same category "
            "and size — verify close to the send, and prefer --min-stock for a long campaign."))

    # ── formats ──────────────────────────────────────────────────────────────
    def _out_text(self, rows):
        for r in rows:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{r['store']} · {r['bundle_name']} ({r['discount_pct']}% off)"))
            for line in r["lines"]:
                self.stdout.write(
                    f"  {line['qty']}x [{line['product_id']}] {line['name'][:52]:52} "
                    f"{line['brand'][:16]:16} ${line['price']:>7.2f}  {line['stock']} on hand")
            self.stdout.write(
                f"  subtotal ${r['subtotal']:.2f}  −${r['discount']:.2f}  "
                f"= ${r['total']:.2f} before tax")
            self.stdout.write(f"  {r['url']}")

    def _out_csv(self, rows):
        w = csv.writer(self.stdout)
        w.writerow(["store", "bundle", "discount_pct", "subtotal", "discount", "total",
                    "product_ids", "url"])
        for r in rows:
            w.writerow([r["store"], r["bundle"], r["discount_pct"], r["subtotal"],
                        r["discount"], r["total"],
                        " ".join(f"{x['product_id']}:{x['qty']}" for x in r["lines"]),
                        r["url"]])

    def _out_json(self, rows):
        json.dump(rows, sys.stdout, indent=2)
        self.stdout.write("")
