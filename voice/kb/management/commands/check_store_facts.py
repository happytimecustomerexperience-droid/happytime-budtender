"""Reconcile store hours/address/phone across the three live sources of truth.

Store facts live in FOUR places and nobody reconciles them: (1) ``kb.StoreFact`` rows —
what the voice agent speaks; (2) root ``bundles/catalog.py`` ``STORES`` — what the
/custom-order storefront shows; (3) the public site's ``data/store-locations.json`` (a
separate repo, read via ``--site-json`` or ``HAPPYTIME_SITE_ROOT``); (4)
``kb/data/site_faqs.json`` prose, out of scope here (it's prose, not a fact table).

This command never edits anything — it only reports drift.

    python manage.py check_store_facts
    python manage.py check_store_facts --site-json /path/to/store-locations.json
    python manage.py check_store_facts --json

Exits 1 (raises CommandError) if any store/fact pair disagrees across the sources that
are available; exits 0 otherwise. The site source is optional: if neither
``--site-json`` nor ``HAPPYTIME_SITE_ROOT`` is set, or the file is missing, the site
column is skipped rather than failing the command.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from voice.evals.golden import atoms, norm

STORES = ("yakima", "mount-vernon", "pullman")
FACT_KINDS = ("hours", "address", "phone")

# atoms(..., "times") keeps ":00" minutes literally ("9:00am-10pm" vs "9am-10pm" for the
# same time), because the site JSON writes "8:00am" and the KB/catalog write "8 AM" —
# collapse the redundant ":00" so the two phrasings of the same hour compare equal.
_ZERO_MINUTES = re.compile(r":00(am|pm)")


def _canon_time_atoms(value: str) -> set[str]:
    return {_ZERO_MINUTES.sub(r"\1", a) for a in atoms(value, "times")}


def _load_catalog() -> dict:
    repo_root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(repo_root))
    try:
        from bundles import catalog  # bundles/catalog.py — pure Python, no Django
    finally:
        sys.path.remove(str(repo_root))
    return catalog.STORES


def _load_site(site_json: Path | None) -> dict | None:
    if site_json is None:
        root = os.environ.get("HAPPYTIME_SITE_ROOT")
        if not root:
            return None
        site_json = Path(root) / "data" / "store-locations.json"
    if not site_json.exists():
        return None
    data = json.loads(site_json.read_text(encoding="utf-8"))
    return {loc["id"]: loc for loc in data.get("locations", [])}


def _site_hours_blob(location: dict) -> str:
    return " ".join(str(v) for v in (location.get("hours") or {}).values())


def _street_part(address: str) -> str:
    """The street-number + street-name half of a full address — enough to check
    containment without failing on "Mt" vs "Mount"."""
    return address.split(",", 1)[0]


class Command(BaseCommand):
    help = "Compare StoreFact hours/address/phone against bundles/catalog.py and the public site."

    def add_arguments(self, parser):
        parser.add_argument(
            "--site-json",
            type=Path,
            default=None,
            help="Path to the public site's data/store-locations.json (overrides HAPPYTIME_SITE_ROOT).",
        )
        parser.add_argument("--json", action="store_true", help="Print the result as JSON instead of a table.")

    def handle(self, *args, **opts):
        from kb import models as m

        catalog_stores = _load_catalog()
        site_locations = _load_site(opts["site_json"])
        if site_locations is None:
            self.stdout.write("site: skipped (HAPPYTIME_SITE_ROOT unset)")

        rows = []
        any_mismatch = False

        for store in STORES:
            catalog_row = catalog_stores.get(store, {})
            site_row = site_locations.get(store) if site_locations else None

            for kind in FACT_KINDS:
                sf = (
                    m.StoreFact.objects.filter(store=store, kind=kind, is_active=True)
                    .order_by("-weight", "label")
                    .first()
                )
                storefact_value = sf.value if sf else ""

                if kind == "hours":
                    catalog_value = catalog_row.get("hours", "")
                    site_value = _site_hours_blob(site_row) if site_row else ""
                    sf_atoms = _canon_time_atoms(storefact_value)
                    cat_atoms = _canon_time_atoms(catalog_value)
                    site_atoms = _canon_time_atoms(site_value) if site_row else None
                    mismatch = bool(sf_atoms) and sf_atoms != cat_atoms
                    if site_row is not None:
                        mismatch = mismatch or (bool(sf_atoms) and sf_atoms != site_atoms)
                    display_catalog, display_site = catalog_value, site_value
                elif kind == "address":
                    catalog_value = ", ".join(
                        p for p in (catalog_row.get("street", ""), catalog_row.get("city", "")) if p
                    )
                    site_value = site_row.get("address", "") if site_row else ""
                    sf_street = norm(_street_part(storefact_value))
                    mismatch = bool(sf_street) and sf_street not in norm(catalog_value)
                    if site_row is not None:
                        mismatch = mismatch or (bool(sf_street) and sf_street not in norm(site_value))
                    display_catalog, display_site = catalog_value, site_value
                else:  # phone
                    catalog_value = catalog_row.get("phone", "")
                    site_value = site_row.get("phone", "") if site_row else ""
                    sf_atoms = atoms(storefact_value, "phone")
                    cat_atoms = atoms(catalog_value, "phone")
                    site_atoms = atoms(site_value, "phone") if site_row else None
                    mismatch = bool(sf_atoms) and sf_atoms != cat_atoms
                    if site_row is not None:
                        mismatch = mismatch or (bool(sf_atoms) and sf_atoms != site_atoms)
                    display_catalog, display_site = catalog_value, site_value

                status = "MISMATCH" if mismatch else "ok"
                any_mismatch = any_mismatch or mismatch
                rows.append(
                    {
                        "store": store,
                        "fact": kind,
                        "storefact": storefact_value,
                        "catalog": display_catalog,
                        "site": display_site if site_row else "(skipped)",
                        "status": status,
                    }
                )

        if opts["json"]:
            self.stdout.write(json.dumps(rows, indent=2))
        else:
            self._print_table(rows)

        if any_mismatch:
            raise CommandError(
                "store fact drift detected — "
                + ", ".join(f"{r['store']}/{r['fact']}" for r in rows if r["status"] == "MISMATCH")
            )

    def _print_table(self, rows: list[dict]) -> None:
        headers = ["store", "fact", "storefact", "catalog", "site", "status"]
        self.stdout.write("| " + " | ".join(headers) + " |")
        self.stdout.write("|" + "|".join(["---"] * len(headers)) + "|")
        for r in rows:
            self.stdout.write("| " + " | ".join(str(r[h]) for h in headers) + " |")
