"""budtender.deals — today's deals from a store's Dutchie online menu (HTTP mocked, never live).

Fixtures are trimmed copies of real records from the 2026-10-01 probe. "Now" is pinned to noon
Pacific on 2026-10-01; Dutchie stores a deal's end as an EXCLUSIVE instant (midnight at the start
of the day after its last day), which is what the expiry cases below pin.
"""
from datetime import datetime
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from budtender import deals

NOW = datetime(2026, 10, 1, 12, 0)
KEYS = override_settings(
    DUTCHIE={"stores": {s: {"pos_key": "k"} for s in ("yakima", "mount-vernon", "pullman")}},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    HHT_BACKEND_TOKEN="t",
)
ALL_DAYS = dict.fromkeys(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), True)


def menu(id_, title, frm, to, method="PERCENT_OFF", value=0.3, **over):
    rec = {
        "id": id_, "onlineName": title, "isActive": True, "applicationMethod": "Automatic",
        "validDateFrom": f"{frm}T07:00:00.0000000Z", "validDateTo": f"{to}T07:00:00.0000000Z",
        "startTime": None, "endTime": None,
        "monday": None, "tuesday": None, "wednesday": None, "thursday": None, "friday": None,
        "saturday": None, "sunday": None,
        "reward": {"calculationMethod": method, "discountValue": value, "thresholdType": None,
                   "applyToOnlyOneItem": False},
        "menuDisplay": {"menuDisplayName": title, "menuDisplayDescription": ""},
    }
    rec.update(over)
    return rec


def manual(id_, name, frm, to, online=True, amount=0.3, kind="Percent", **over):
    rec = {
        "discountId": id_, "discountName": name, "applicationMethod": "Manual", "isAvailableOnline": online,
        "isDeleted": False, "isActive": False, "discountType": kind, "discountAmount": amount,
        "validFrom": f"{frm}T07:00:00.0000000Z", "validUntil": f"{to}T07:00:00.0000000Z",
        "weeklyRecurrenceInfo": None,
    }
    rec.update(over)
    return rec


MENU = [
    menu(291758, "ALL REDBIRD EVERYTHING 30% off Special", "2026-07-05", "2026-10-31"),
    menu(292182, "WOOK TEA SPECIAL - 40% OFF", "2026-06-22", "4200-04-19", value=0.4),
    menu(291486, "Chewee's Special", "2026-06-22", "4200-04-19", method="PRICE_TO_AMOUNT", value=14.0),
    menu(292725, "October Happy Hour 20% OFF", "2026-10-01", "2026-11-01", value=0.2,
         startTime="09:59:00", endTime="12:05:00", **ALL_DAYS),
    menu(293110, "HAPPY HOUR Morning Buy One Get The Next Item 50% OFF Pullman", "2026-09-01", "2026-11-01",
         value=0.5, startTime="09:00:00", endTime="10:00:00", monday=True, friday=True,
         reward={"calculationMethod": "PERCENT_OFF", "discountValue": 0.5,
                 "thresholdType": "NUMBER_OF_ITEMS", "applyToOnlyOneItem": True}),
    # dropped: ended at midnight starting today (exclusive end) / not started / disabled / no menu name
    menu(293092, "Method Flower Special 3.5g", "2026-09-28", "2026-10-01"),
    menu(293200, "Starts tomorrow", "2026-10-02", "2026-10-09"),
    menu(293201, "Disabled but in window", "2026-09-01", "2026-12-01", isActive=False),
    menu(293202, "", "2026-09-01", "2026-12-01", menuDisplay={}),
]
REPORT = [
    manual(292726, "October Special Add A 3rd Get 30% off", "2026-10-01", "2026-11-01"),
    manual(292903, "Happy Hour 20% OFF BUTTON", "2026-10-01", "2026-12-01", amount=0.2,
           weeklyRecurrenceInfo={"startTime": "09:50:00", "endTime": "13:00:00",
                                 **{f"appliesOn{d.capitalize()}": True for d in ALL_DAYS}}),
    manual(290216, "Shatter Pre-Roll Special BUTTON", "2026-04-01", "4200-04-19", amount=9.0, kind="Price To Amount"),
    # dropped: POS-only, expired, an automatic row (already in the menu feed)
    manual(100001, "Senior 10%", "2023-01-01", "4200-04-19", online=False, amount=0.1),
    manual(292204, "Aug Special 25% off BOGO", "2026-08-01", "2026-09-01", amount=0.25),
    manual(300000, "Automatic twin", "2026-09-01", "2026-12-01", applicationMethod="Automatic"),
]


def feeds(menu_rows=MENU, report_rows=REPORT):
    def get(_key, path, params=None):
        return {"/discounts/v2/list": menu_rows, "/reporting/discounts": report_rows}[path]
    return get


@KEYS
class CurrentDealsTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def deals(self, **kw):
        with mock.patch("budtender.deals._pos_get", side_effect=feeds(**kw)):
            return {d["id"]: d for d in deals.current_deals("yakima", now=NOW)}

    def test_only_deals_running_today_survive(self):
        got = self.deals()
        self.assertEqual(
            set(got),
            {291758, 292182, 291486, 292725, 293110, 292726, 292903, 290216},
        )

    def test_end_is_exclusive_and_ends_is_last_day_it_runs(self):
        got = self.deals()
        self.assertNotIn(293092, got)  # ended 00:00 today
        self.assertEqual(got[291758]["ends"], "2026-10-30")  # stored Oct 31 00:00
        self.assertEqual(got[292725]["ends"], "2026-10-31")  # stored Nov 1 00:00
        self.assertEqual(got[291758]["starts"], "2026-07-05")

    def test_year_4200_means_no_end(self):
        self.assertIsNone(self.deals()[292182]["ends"])

    def test_happy_hour_kept_with_its_hours(self):
        hh = self.deals()[292725]
        self.assertEqual((hh["start_time"], hh["end_time"], hh["days"]), ("09:59", "12:05", None))
        self.assertEqual((hh["kind"], hh["value"], hh["source"]), ("percent", 20, "menu"))

    def test_kinds_values_and_days(self):
        got = self.deals()
        self.assertEqual((got[291486]["kind"], got[291486]["value"]), ("price", 14))
        bogo = got[293110]
        self.assertEqual((bogo["kind"], bogo["value"], bogo["days"]), ("bogo", 50, ["monday", "friday"]))

    def test_manual_online_included_pos_only_dropped(self):
        got = self.deals()
        self.assertEqual(got[292726]["source"], "manual")
        self.assertEqual((got[292726]["kind"], got[292726]["value"]), ("percent", 30))
        self.assertEqual(got[290216]["kind"], "price")
        for pos_only in (100001, 292204, 300000):
            self.assertNotIn(pos_only, got)

    def test_manual_name_loses_button_jargon_and_keeps_hours(self):
        hh = self.deals()[292903]
        self.assertEqual(hh["title"], "Happy Hour 20% OFF")
        self.assertEqual((hh["start_time"], hh["end_time"], hh["days"]), ("09:50", "13:00", None))

    def test_authoritative_empty_is_a_list_not_an_error(self):
        self.assertEqual(self.deals(menu_rows=[], report_rows=[]), {})

    def test_unreadable_feed_raises_never_empty(self):
        for bad in ("menu", "report"):
            rows = {"menu_rows": None} if bad == "menu" else {"report_rows": None}
            with mock.patch("budtender.deals._pos_get", side_effect=feeds(**rows)):
                with self.assertRaises(deals.DealsUnavailable):
                    deals.current_deals("yakima", now=NOW)

    def test_unexpected_record_shape_raises(self):
        with mock.patch("budtender.deals._pos_get", side_effect=feeds(menu_rows=[{"id": 1, "isActive": True}])):
            with self.assertRaises(deals.DealsUnavailable):
                deals.current_deals("yakima", now=NOW)

    @override_settings(DUTCHIE={"stores": {"yakima": {"pos_key": ""}}})
    def test_missing_key_raises(self):
        with self.assertRaises(deals.DealsUnavailable):
            deals.current_deals("yakima", now=NOW)


@KEYS
class DealsEndpointTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_requires_service_token(self):
        self.assertIn(self.client.get("/api/v1/deals/").status_code, (401, 403))

    def test_unreachable_store_is_null_and_not_cached(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer t")
        down = {"mount-vernon": True}

        def fake(slug, now=None):
            if down.get(slug):
                raise deals.DealsUnavailable("boom")
            return [] if slug == "yakima" else [{"id": 1, "store": slug}]

        with mock.patch("budtender.deals.current_deals", side_effect=fake) as m:
            body = self.client.get("/api/v1/deals/").json()
            self.assertEqual(body["stores"]["yakima"], [])  # authoritative none
            self.assertIsNone(body["stores"]["mount-vernon"])  # unknown, NOT []
            self.assertEqual(body["stores"]["pullman"], [{"id": 1, "store": "pullman"}])
            self.assertEqual(body["errors"], {"mount-vernon": "unreachable"})
            self.assertFalse(body["ok"])
            self.assertIn("fetched_at", body)
            self.assertEqual(m.call_count, 3)

            down.clear()  # Dutchie recovers: the failed store is retried, the others come from cache
            body = self.client.get("/api/v1/deals/").json()
            self.assertEqual(body["stores"]["mount-vernon"], [{"id": 1, "store": "mount-vernon"}])
            self.assertEqual((body["errors"], body["ok"]), ({}, True))
            self.assertEqual(m.call_count, 4)
