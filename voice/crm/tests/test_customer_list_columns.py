"""The denormalised Customers-table columns: derived on save, filled by the importer, backfilled by
migration 0006. Offline, SQLite."""

from __future__ import annotations

import importlib
import json
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from crm.models import CustomerProfile, brands_text, parse_order_date


# ── pure helpers ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-04-01", date(2026, 4, 1)),
        ("2026-04-01T12:30:00", date(2026, 4, 1)),
        ("2026-04-01 12:30", date(2026, 4, 1)),
        (" 2026-04-01 ", date(2026, 4, 1)),
        ("", None),
        (None, None),
        ("garbage", None),
        ("2026-13-45", None),
        ("04/01/2026", None),
    ],
)
def test_parse_order_date(value, expected):
    assert parse_order_date(value) == expected


def test_brands_text_is_lowercase_wrapped_deduped_and_null_safe():
    assert brands_text(["Wyld", [], None, {"brand": "Kiva"}, {"Brand": "WYLD"}, "  "]) == "|wyld|kiva|"
    assert brands_text(["A|B"]) == "|a b|"  # a delimiter inside a name cannot break the wrapping
    assert brands_text([{"brand": None, "share": None}, "", None]) == ""


# ── sync on save ──────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_save_derives_columns():
    p = CustomerProfile.objects.create(
        customer_key="k", name="Jane", top_brand="Acme", favorite_brands=[{"brand": "Zed Co"}, "ACME"],
        top_categories=[{"category": "Edibles", "share": 60}, {"category": "Flower"}],
        first_order="2025-01-02", last_order="2026-03-04T10:00:00",
    )
    p.refresh_from_db()
    assert (p.top_category, p.brands_text) == ("Edibles", "|acme|zed co|")
    assert (p.first_order_date, p.last_order_date) == (date(2025, 1, 2), date(2026, 3, 4))
    assert p.items is None  # never derived: unknown until the importer reads TotalUnits


@pytest.mark.django_db
def test_save_handles_alternate_shapes_and_nulls():
    p = CustomerProfile.objects.create(
        customer_key="k", top_categories=[None, {"Category": "Vape"}], favorite_brands=[{"brand": None}],
        first_order="nope",
    )
    p.refresh_from_db()
    assert p.top_category == "Vape" and p.brands_text == "" and p.first_order_date is None
    p2 = CustomerProfile.objects.create(customer_key="k2", top_categories=["Pre-roll"])
    assert p2.top_category == "Pre-roll"


@pytest.mark.django_db
def test_partial_save_still_persists_rederived_columns():
    p = CustomerProfile.objects.create(customer_key="k", top_brand="Old")
    p.top_brand = "New"
    p.last_order = "2026-02-02"
    p.save(update_fields=["top_brand", "last_order"])
    fresh = CustomerProfile.objects.get(pk=p.pk)
    assert fresh.brands_text == "|new|" and fresh.last_order_date == date(2026, 2, 2)


# ── importer ──────────────────────────────────────────────────────────────────
def _write(tmp_path, profiles, rich=None):
    src = tmp_path / "customers.json"
    src.write_text(json.dumps({"customerProfiles": profiles, "customerRichDetail": rich or {}}),
                   encoding="utf-8")
    return str(src)


@pytest.mark.django_db
def test_import_fills_items_category_brands_and_dates(tmp_path):
    src = _write(tmp_path, {
        "Jane Doe": {"Orders": 10, "TotalSpend": 800, "AOV": 80, "TotalUnits": 57,
                     "FirstOrder": "2026-01-01", "LastOrder": "2026-04-01 09:00:00",
                     "TopCategories": [{"category": "Flower", "share": 70}], "TopBrand": "Acme"},
        "No Units": {"Orders": 1, "TotalSpend": 40, "AOV": 40, "FirstOrder": "2026-02-01",
                     "LastOrder": "2026-02-01"},
        "Null Units": {"Orders": 1, "TotalSpend": 40, "AOV": 40, "TotalUnits": None},
    }, {"Jane Doe": {"topBrands": [{"brand": "Kiva"}, "Wyld"]}})

    call_command("import_customer_profiles", "--customers", src)

    jane = CustomerProfile.objects.get(customer_key="Jane Doe")
    assert jane.items == 57
    assert jane.top_category == "Flower"
    assert jane.brands_text == "|acme|kiva|wyld|"
    assert (jane.first_order_date, jane.last_order_date) == (date(2026, 1, 1), date(2026, 4, 1))
    assert CustomerProfile.objects.get(customer_key="No Units").items is None  # unknown, not 0
    assert CustomerProfile.objects.get(customer_key="Null Units").items is None

    # Re-import is stable and refreshes the columns in place.
    call_command("import_customer_profiles", "--customers", src)
    assert CustomerProfile.objects.count() == 3
    assert CustomerProfile.objects.get(customer_key="Jane Doe").top_category == "Flower"


@pytest.mark.django_db
def test_import_sums_units_across_same_phone_rows(tmp_path, settings):
    settings.PHONE_HASH_PEPPER = "test-pepper"
    src = _write(tmp_path, {
        "A": {"Phone": "509-555-1212", "Orders": 1, "TotalSpend": 10, "TotalUnits": 3},
        "B": {"Phone": "509-555-1212", "Orders": 1, "TotalSpend": 10, "TotalUnits": 4},
        "C": {"Phone": "509-555-3434", "Orders": 1, "TotalSpend": 10, "TotalUnits": 5},
        "D": {"Phone": "509-555-3434", "Orders": 1, "TotalSpend": 10},
        "E": {"Phone": "509-555-5656", "Orders": 1, "TotalSpend": 10},
        "F": {"Phone": "509-555-5656", "Orders": 1, "TotalSpend": 10},
    })
    call_command("import_customer_profiles", "--customers", src)
    units = sorted(
        (p.items is None, p.items or 0) for p in CustomerProfile.objects.all()
    )
    assert units == [(False, 5), (False, 7), (True, 0)]  # 3+4, the known 5, all-unknown stays None


@pytest.mark.django_db
def test_import_warns_about_unparseable_last_order(tmp_path):
    src = _write(tmp_path, {
        "Good": {"Orders": 1, "LastOrder": "2026-04-01"},
        "Bad": {"Orders": 1, "LastOrder": "April 1st"},
    })
    out = StringIO()
    call_command("import_customer_profiles", "--customers", src, stdout=out)
    assert "WARNING: 1 have a last-order date that could not be parsed" in out.getvalue()
    CustomerProfile.objects.all().delete()
    out = StringIO()
    call_command("import_customer_profiles", "--customers", _write(tmp_path, {"Good": {"Orders": 1}}),
                 stdout=out)
    assert "WARNING" not in out.getvalue()


# ── migration 0006 backfill ───────────────────────────────────────────────────
MIGRATION = "0006_customerprofile_list_columns"


def _historical_model():
    """The CustomerProfile model exactly as migration 0006 leaves it (no save() override)."""
    state = MigrationExecutor(connection).loader.project_state([("crm", MIGRATION)])
    return state.apps.get_model("crm", "CustomerProfile")


@pytest.mark.django_db
def test_backfill_fills_rows_written_before_the_columns_existed():
    migration = importlib.import_module(f"crm.migrations.{MIGRATION}")
    Hist = _historical_model()
    old_shape = [
        dict(customer_key="a", top_brand="Acme", first_order="2025-01-02", last_order="2026-03-04",
             favorite_brands=[{"brand": "Kiva"}], top_categories=[{"category": "Flower"}]),
        dict(customer_key="b", top_brand="", favorite_brands=[{"brand": None}],
             top_categories=[], first_order="", last_order="not a date"),
        dict(customer_key="c", top_brand="Wyld", top_categories=[None, {"Category": "Vape"}],
             last_order="2026-05-06T07:08:09", favorite_brands=["wyld", "Zed"]),
    ]
    for kw in old_shape:  # historical model: no save() override, so the new columns stay blank
        Hist.objects.create(**kw)
    assert not Hist.objects.exclude(top_category="").exists()

    migration.backfill_list_columns(Hist._meta.apps, None)

    rows = {p.customer_key: p for p in Hist.objects.all()}
    a, b, c = rows["a"], rows["b"], rows["c"]
    assert (a.top_category, a.brands_text) == ("Flower", "|acme|kiva|")
    assert (a.first_order_date, a.last_order_date) == (date(2025, 1, 2), date(2026, 3, 4))
    assert (b.top_category, b.brands_text, b.first_order_date, b.last_order_date) == ("", "", None, None)
    assert (c.top_category, c.brands_text) == ("Vape", "|wyld|zed|")
    assert c.last_order_date == date(2026, 5, 6) and c.first_order_date is None
    assert all(p.items is None for p in rows.values())  # unknown, not 0


@pytest.mark.django_db
def test_backfill_matches_what_save_derives():
    """The migration keeps its own copy of the derivation; it must not drift from the model."""
    migration = importlib.import_module(f"crm.migrations.{MIGRATION}")
    shapes = [
        dict(top_brand="Acme", favorite_brands=[{"brand": "Kiva"}, "ACME", None, {"brand": None}],
             top_categories=[{"category": "Flower"}], first_order="2025-01-02", last_order="x"),
        dict(top_brand="A|B", favorite_brands=[{"Brand": "C"}], top_categories=["Edibles"],
             last_order="2026-03-04T10:00"),
        dict(top_brand="", favorite_brands=[], top_categories=[{"category": None}, {"Category": "Z"}]),
    ]
    Hist = _historical_model()
    for i, kw in enumerate(shapes):
        Hist.objects.create(customer_key=f"h{i}", **kw)
        CustomerProfile.objects.create(customer_key=f"m{i}", **kw)
    migration.backfill_list_columns(Hist._meta.apps, None)
    cols = ("top_category", "brands_text", "first_order_date", "last_order_date")
    for i in range(len(shapes)):
        assert (
            Hist.objects.filter(customer_key=f"h{i}").values_list(*cols).get()
            == CustomerProfile.objects.filter(customer_key=f"m{i}").values_list(*cols).get()
        )


def test_backfill_reverse_is_a_noop():
    migration = importlib.import_module(f"crm.migrations.{MIGRATION}")
    from django.db.migrations import RunPython

    ops = [o for o in migration.Migration.operations if isinstance(o, RunPython)]
    assert len(ops) == 1 and ops[0].reverse_code is RunPython.noop
