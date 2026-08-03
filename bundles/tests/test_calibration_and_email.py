"""The order cap is derived from real baskets, and shoppers get a confirmation."""
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from budtender.models import PhoneCartDraft, Setting
from bundles import calibration, emails

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
SMTP = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "DEFAULT_FROM_EMAIL": "orders@happytimeweed.com",
}


def _tx(*totals):
    """/reporting/transactions?includeDetail=true rows, one per basket."""
    return [{"items": [{"unitPrice": t, "quantity": 1}]} for t in totals]


class PercentileTests(TestCase):
    def test_nearest_rank(self):
        values = list(range(1, 101))          # 1..100
        self.assertEqual(calibration.percentile(values, 50), 50)
        self.assertEqual(calibration.percentile(values, 99), 99)
        self.assertEqual(calibration.percentile(values, 100), 100)

    def test_empty(self):
        self.assertEqual(calibration.percentile([], 99), 0.0)

    def test_single_value(self):
        self.assertEqual(calibration.percentile([42.0], 99), 42.0)


class BasketTotalTests(TestCase):
    def test_sums_line_items(self):
        rows = [{"items": [{"unitPrice": 10, "quantity": 2}, {"unitPrice": 5, "quantity": 1}]}]
        self.assertEqual(calibration.basket_totals(rows), [25.0])

    def test_refunds_and_zero_baskets_are_dropped(self):
        # A return is not a basket; letting negatives in would drag the cap down.
        rows = [{"items": [{"unitPrice": -20, "quantity": 1}]},
                {"items": [{"unitPrice": 0, "quantity": 1}]},
                {"items": [{"unitPrice": 30, "quantity": 1}]}]
        self.assertEqual(calibration.basket_totals(rows), [30.0])

    def test_header_only_feed_falls_back_to_the_row_total(self):
        self.assertEqual(calibration.basket_totals([{"total": 47.5}]), [47.5])

    def test_junk_rows_do_not_raise(self):
        self.assertEqual(
            calibration.basket_totals([None, "x", {}, {"items": [{"unitPrice": "abc"}]}]), [])


@override_settings(CACHES=CACHES_LOCMEM, BUNDLE_MAX_ORDER_TOTAL=300)
class CalibrateTests(TestCase):
    def _patch(self, rows):
        return patch("budtender.dutchie.get_transactions_detailed", return_value=rows)

    def test_cap_is_derived_from_the_p99_basket(self):
        # A store whose baskets genuinely run large: 100 evenly spread $10..$1000.
        # p99 = $990, comfortably above the floor, so the data wins.
        with self._patch(_tx(*[float(i * 10) for i in range(1, 101)])):
            dist = calibration.calibrate("yakima")
        self.assertEqual(dist["sample"], 100)
        self.assertEqual(dist["p99"], 990.0)
        self.assertEqual(dist["applied"], 990.0)
        self.assertEqual(calibration.cap_for("yakima"), 990.0)

    def test_p99_excludes_the_single_outlier_basket(self):
        # This is the whole point of p99 over max: 199 normal baskets and one
        # enormous one must NOT drag the cap up to the outlier.
        with self._patch(_tx(*([400.0] * 199 + [9000.0]))):
            dist = calibration.calibrate("yakima")
        self.assertEqual(dist["max"], 9000.0)
        self.assertEqual(dist["p99"], 400.0)
        self.assertEqual(dist["applied"], 400.0)

    def test_a_small_sample_never_moves_the_cap(self):
        with self._patch(_tx(*([40.0] * 10))):
            dist = calibration.calibrate("yakima")
        self.assertIsNone(dist["applied"])
        self.assertEqual(calibration.cap_for("yakima"), 300)
        self.assertFalse(Setting.objects.exists())

    def test_a_quiet_period_can_never_calibrate_below_the_floor(self):
        # 200 tiny baskets: p99 is ~$20, but dropping the cap to $20 would reject
        # ordinary orders. The configured floor wins.
        with self._patch(_tx(*([20.0] * 200))):
            dist = calibration.calibrate("yakima")
        self.assertEqual(dist["applied"], 300)
        self.assertEqual(calibration.cap_for("yakima"), 300)

    def test_a_pull_failure_leaves_the_cap_alone(self):
        with patch("budtender.dutchie.get_transactions_detailed",
                   side_effect=RuntimeError("dutchie down")):
            dist = calibration.calibrate("yakima")
        self.assertEqual(dist.get("error"), "pull_failed")
        self.assertEqual(calibration.cap_for("yakima"), 300)

    def test_caps_are_per_store(self):
        with self._patch(_tx(*([500.0] * 100))):
            calibration.calibrate("yakima")
        self.assertEqual(calibration.cap_for("yakima"), 500.0)
        self.assertEqual(calibration.cap_for("pullman"), 300)   # untouched

    def test_corrupt_setting_falls_back_rather_than_raising(self):
        Setting.objects.create(key="bundle_max_order_total:yakima", value={"cap": "nonsense"})
        self.assertEqual(calibration.cap_for("yakima"), 300)


def _draft(**kw):
    d = dict(location_slug="yakima", source=PhoneCartDraft.Source.ONLINE,
             status=PhoneCartDraft.Status.RELEASED, pickup_name="Sam Reyes",
             contact_phone="5095551212", contact_email="sam@example.com",
             expires_at=timezone.now(),
             lines=[{"product_id": "1", "name": "Blue Dream 3.5g", "brand": "Athenry",
                     "quantity": 2, "line_total": 50.0, "in_stock": True}],
             quote={"total": 50.0})
    d.update(kw)
    return PhoneCartDraft.objects.create(**d)


@override_settings(CACHES=CACHES_LOCMEM, **SMTP)
class ConfirmationEmailTests(TestCase):
    def test_sends_the_order_to_the_shopper(self):
        draft = _draft()
        self.assertTrue(emails.send_order_confirmation(draft, "Happy Time — Yakima", "Yakima, WA"))
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["sam@example.com"])
        self.assertIn("Happy Time — Yakima", msg.subject)
        self.assertIn("Blue Dream 3.5g", msg.body)
        self.assertIn("Sam Reyes", msg.body)
        self.assertIn("21+", msg.body)

    def test_includes_the_bundle_reminder_when_there_is_one(self):
        draft = _draft(quote={"total": 50.0, "bundle_name": "Weekend Bundle",
                              "bundle_discount_pct": 30})
        emails.send_order_confirmation(draft, "Happy Time — Yakima", "Yakima, WA")
        self.assertIn("Weekend Bundle", mail.outbox[0].body)
        self.assertIn("30%", mail.outbox[0].body)

    def test_sold_out_lines_are_not_listed(self):
        draft = _draft(lines=[
            {"name": "Kept", "quantity": 1, "line_total": 10.0, "in_stock": True},
            {"name": "Vanished", "quantity": 1, "line_total": 0.0, "in_stock": False}])
        emails.send_order_confirmation(draft, "Happy Time — Yakima", "Yakima, WA")
        self.assertIn("Kept", mail.outbox[0].body)
        self.assertNotIn("Vanished", mail.outbox[0].body)

    def test_no_email_address_means_no_send(self):
        self.assertFalse(emails.send_order_confirmation(_draft(contact_email=""), "S", "A"))
        self.assertEqual(len(mail.outbox), 0)

    def test_never_leaks_staff_signals(self):
        emails.send_order_confirmation(_draft(), "Happy Time — Yakima", "Yakima, WA")
        blob = (mail.outbox[0].body + str(mail.outbox[0].alternatives)).lower()
        for word in ("margin_pct", "velocity", "price_z", "bucket", "serialno"):
            self.assertNotIn(word, blob)


@override_settings(CACHES=CACHES_LOCMEM,
                   EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend",
                   DEFAULT_FROM_EMAIL="")
class EmailDisabledTests(TestCase):
    def test_unconfigured_mail_is_a_silent_no_op(self):
        # An unconfigured mail server must never become a failed checkout.
        self.assertFalse(emails.enabled())
        self.assertFalse(emails.send_order_confirmation(_draft(), "S", "A"))


@override_settings(CACHES=CACHES_LOCMEM, **SMTP)
class EmailFailureTests(TestCase):
    def test_smtp_failure_is_swallowed(self):
        with patch("bundles.emails.EmailMultiAlternatives.send", side_effect=OSError("no smtp")):
            self.assertFalse(emails.send_order_confirmation(_draft(), "S", "A"))
