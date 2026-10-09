"""manage.py audit_customer: raw history vs `derived`, flags, and PASS/FAIL checks of the website's own
rank_products for each likely category. Synthetic personas (tailoring_fixtures); read-only."""
import io
import json
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from budtender import customer_model
from budtender.models import CustomerProfile, SuggestedProduct, SyncState
from budtender.tests import tailoring_fixtures as fx


def run(*args):
    out = io.StringIO()
    try:
        call_command("audit_customer", *args, stdout=out)
        code = 0
    except SystemExit as e:
        code = e.code
    return code, out.getvalue()


class AuditCustomer(TestCase):
    @classmethod
    def setUpTestData(cls):
        SyncState.objects.update_or_create(location_slug=fx.LOC, defaults={"last_synced_at": timezone.now()})
        cls.cat, cls.p = fx.make_all()

    def test_every_persona_passes_and_json_is_stable(self):
        for key in fx.HISTORY:
            code, out = run("--phone", fx.PHONES[key], "--json")
            self.assertEqual(code, 0, (key, out[-2000:]))
            rep = json.loads(out)
            self.assertEqual(rep["result"], "PASS")
            self.assertEqual(set(rep["derived"]), set(customer_model.EMPTY))
            self.assertEqual(out, run("--phone", fx.PHONES[key], "--json")[1], key)   # stable
            self.assertNotIn("margin", out)
            self.assertNotIn(fx.PHONES[key], out)   # last 4 only

    def test_ratio_buyer_report(self):
        rep = json.loads(run("--phone", fx.PHONES["ratio"], "--json")[1])
        self.assertEqual(rep["raw"]["ratios"], {"1:1": 9, "2:1": 3})
        self.assertEqual(rep["raw"]["categories"], {"edibles": 11, "tinctures": 1})
        self.assertEqual(rep["derived"]["ratio_pref"], ["1:1", "2:1"])
        checks = {(c["category"], c["check"]): c["status"] for c in rep["checks"]}
        self.assertEqual(checks[("edibles", "ratio")], "PASS")
        self.assertEqual(checks[("edibles", "form")], "PASS")
        self.assertEqual(checks[("edibles", "price_band")], "PASS")
        self.assertEqual(checks[("edibles", "in_stock")], "PASS")

    def test_untailored_suggestions_fail_the_audit(self):
        # the same customers ranked the way they were before tailoring
        with mock.patch.object(customer_model, "tailor_for", return_value=None):
            code, out = run("--phone", fx.PHONES["conn"], "--json")
            fails = {(c["category"], c["check"]) for c in json.loads(out)["checks"] if c["status"] == "FAIL"}
            self.assertEqual(code, 1)
            self.assertIn(("flower", "not_last_purchase"), fails)   # the eighth they bought 5 days ago leads
            code, out = run("--phone", fx.PHONES["rosin"], "--json")
            fails = {(c["category"], c["check"]) for c in json.loads(out)["checks"] if c["status"] == "FAIL"}
            self.assertEqual(code, 1)
            self.assertIn(("vape-cartridges", "not_last_purchase"), fails)   # the cart from 8 days ago leads

    def test_flags_an_affinity_that_disagrees_with_the_history(self):
        CustomerProfile.objects.filter(pk=self.p["conn"].pk).update(category_affinity={"edibles": 1.0}, total_orders=3)
        rep = json.loads(run("--phone", fx.PHONES["conn"], "--json")[1])
        joined = " ".join(rep["flags"])
        self.assertIn("category_affinity leads with 'edibles'", joined)
        self.assertIn("total_orders=3", joined)

    def test_flags_a_stale_stored_derived(self):
        CustomerProfile.objects.filter(pk=self.p["rosin"].pk).update(
            memory={"v": 1, "derived": {**customer_model.compute_derived(self.p["rosin"]), "ratio_pref": ["1:1"]}})
        rep = json.loads(run("--phone", fx.PHONES["rosin"], "--json")[1])
        self.assertTrue(any("stale on: ratio_pref" in f for f in rep["flags"]), rep["flags"])

    def test_a_shared_row_is_audited_with_a_flag(self):
        CustomerProfile.objects.filter(pk=self.p["budget"].pk).update(dutchie_ids=["1", "2", "3", "4"])
        rep = json.loads(run("--phone", fx.PHONES["budget"], "--json")[1])
        self.assertTrue(any(f.startswith("SHARED row") for f in rep["flags"]))

    def test_text_output_and_unknown_numbers(self):
        code, out = run("--phone", fx.PHONES["ratio"])
        self.assertEqual(code, 0)
        self.assertIn("DERIVED ", out)
        self.assertIn("RESULT PASS", out)
        with self.assertRaises(CommandError):
            call_command("audit_customer", "--phone", "+15095559999", stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command("audit_customer", "--phone", "12", stdout=io.StringIO())

    def test_read_only(self):
        before = list(CustomerProfile.objects.order_by("id").values())
        n = SuggestedProduct.objects.count()
        run("--phone", fx.PHONES["ratio"], "--json")
        self.assertEqual(list(CustomerProfile.objects.order_by("id").values()), before)
        self.assertEqual(SuggestedProduct.objects.count(), n)
