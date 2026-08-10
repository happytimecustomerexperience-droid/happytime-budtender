"""A phone number in, a loyalty balance out, with no login in front of it.

The page answers three ways — found / not registered / couldn't check — and the
whole point is that the third never collapses into the second. A register outage
that tells a fifteen-year customer they aren't registered sends them off to sign up
again, and their points end up split across two accounts.

It said "not registered" and "couldn't check" identically until 2026-08-10, so that
someone testing numbers could not learn which are real. The owner asked for the
plain answer, which makes this a membership oracle; the throttle (5/min, 30/hour
per IP) is what stops enumeration now, and it was always the control that mattered.

Nothing here touches the network — the register client is patched.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from bundles import loyalty

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class TheLadderTests(TestCase):
    """Owner-supplied 2026-08-05: 125/10, 250/15, 450/20, 600/25, 900/30, percent OFF."""

    def test_a_balance_below_the_first_rung_is_worth_nothing(self):
        # The honest answer. Copy that implied otherwise would be the whole bug.
        for points in (0, 1, 124):
            self.assertEqual(loyalty.percent_for(points), 0)

    def test_the_ladder_pays_at_the_step_not_between_them(self):
        # 500 points is the 450 rung (20%), never "nearly 25%".
        self.assertEqual(loyalty.percent_for(500), 20)
        self.assertEqual(loyalty.percent_for(449), 15)
        self.assertEqual(loyalty.percent_for(450), 20)

    def test_the_top_rung_holds_above_it(self):
        self.assertEqual(loyalty.percent_for(900), 30)
        self.assertEqual(loyalty.percent_for(99999), 30)

    def test_the_next_rung_is_the_distance_to_it(self):
        self.assertEqual(loyalty.next_tier(0), (125, 10))
        self.assertEqual(loyalty.next_tier(500), (100, 25))   # 600 - 500
        self.assertIsNone(loyalty.next_tier(900))

    def test_the_ladder_is_not_alpineiq_s(self):
        """AlpineIQ's object says 100/5, 200/10, 400/15, 600/20, 800/25 and describes
        nothing the register does. Two rounds of reasoning have already been wasted on
        it; this fails if anyone re-derives the ladder from that system again."""
        self.assertEqual(loyalty.TIERS[0], (125, 10))
        self.assertNotIn((100, 5), loyalty.TIERS)


@override_settings(CACHES=CACHES_LOCMEM)
class TheLookupTells(TestCase):
    URL = "/loyalty/"

    def setUp(self):
        # The throttle counts into a process-global LocMemCache keyed on (scope, ip),
        # and every test client is 127.0.0.1 — so budget spent by one test is gone for
        # the next. Without this, the tests pass alone and 429 in a suite.
        cache.clear()
        self.addCleanup(cache.clear)

    def _post(self, phone="5095551212", client=None):
        return (client or Client()).post(self.URL, {"phone": phone})

    def test_the_form_renders_without_asking_anything(self):
        body = Client().get(self.URL).content.decode()
        self.assertIn('name="phone"', body)
        self.assertIn("125", body)          # the ladder is on the page unprompted

    def test_a_member_sees_points_and_what_they_are_worth(self):
        with patch.object(loyalty, "balance_for_phone", return_value=("found", {
                "points": 500, "is_member": True, "tier_name": "Gold",
                "percent": 20, "next": (100, 25)})):
            body = self._post().content.decode()
        self.assertIn("500", body)
        self.assertIn("20% off", body)      # never a number without its offer
        self.assertIn("100 more points", body)

    def test_a_balance_below_the_first_rung_says_so_plainly(self):
        with patch.object(loyalty, "balance_for_phone", return_value=("found", {
                "points": 40, "is_member": True, "tier_name": "",
                "percent": 0, "next": (85, 10)})):
            body = self._post().content.decode()
        self.assertIn("Not enough to redeem yet", body)

    # ── the three outcomes ───────────────────────────────────────────────────
    def test_an_unregistered_number_is_told_so_plainly(self):
        # Owner, 2026-08-10. This does make the page a membership oracle; the
        # throttle below is what stops enumeration now.
        with patch.object(loyalty, "balance_for_phone", return_value=("none", None)):
            body = self._post().content.decode()
        self.assertIn("isn't registered yet", body)

    def test_a_register_outage_never_says_you_are_not_registered(self):
        """THE distinction. Told they're new, a fifteen-year customer re-registers
        and their points end up split across two accounts."""
        with patch("bundles.loyalty.customers.lookup_by_phone", side_effect=OSError("down")):
            body = self._post().content.decode()
        self.assertIn("couldn't check right now", body.lower())
        self.assertNotIn("isn't registered", body)

    def test_one_store_being_down_is_not_a_clean_no(self):
        # A number registered at the store we failed to reach is not absent.
        def yakima_down(slug, phone):
            if slug == "yakima":
                raise OSError("down")
            return ("", "", "new")

        with patch("bundles.loyalty.customers.lookup_by_phone", side_effect=yakima_down):
            state, _ = loyalty.balance_for_phone("5095551212")
        self.assertEqual(state, "unavailable")

    def test_a_swallowed_dutchie_error_is_not_a_clean_no_either(self):
        # lookup_by_phone catches its own errors and returns `unresolved`; reading
        # that as "no account" is the same bug wearing a return value.
        with patch("bundles.loyalty.customers.lookup_by_phone",
                   return_value=("", "", "unresolved")):
            state, _ = loyalty.balance_for_phone("5095551212")
        self.assertEqual(state, "unavailable")

    def test_every_store_answering_no_is_a_clean_no(self):
        with patch("bundles.loyalty.customers.lookup_by_phone", return_value=("", "", "new")):
            state, balance = loyalty.balance_for_phone("5095551212")
        self.assertEqual((state, balance), ("none", None))

    def test_a_short_number_never_reaches_the_register(self):
        with patch.object(loyalty, "balance_for_phone") as spy:
            body = self._post(phone="509").content.decode()
        spy.assert_not_called()
        self.assertIn("10-digit", body)

    def test_the_page_never_says_who_the_number_belongs_to(self):
        """It answers "how many points", not "whose number is this". The Dutchie row
        behind it carries a name, DOB, address, email and every purchase."""
        row = {"LoyaltyPoints": 500.0, "IsLoyaltyMember": True, "LoyaltyTierName": "Gold",
               "FirstName": "Jane", "LastName": "Doe", "Email": "jane@example.test",
               "DOB": "1990-01-15", "Address": "123 Main St"}
        with patch("bundles.loyalty.customers.lookup_by_phone",
                   return_value=("710000099", "Jane Doe", "matched")), \
             patch("bundles.loyalty._details", return_value=row):
            body = self._post().content.decode()
        for secret in ("Jane", "Doe", "jane@example.test", "1990-01-15", "123 Main"):
            self.assertNotIn(secret, body, f"{secret!r} leaked onto a public page")
        self.assertIn("500", body)

    def test_enumeration_is_throttled(self):
        c = Client()
        with patch.object(loyalty, "balance_for_phone", return_value=("none", None)):
            codes = [self._post(phone=f"50955512{i:02d}", client=c).status_code
                     for i in range(8)]
        self.assertIn(429, codes, "a number-testing script is never slowed down")
        self.assertEqual(codes.count(200), 5, "the per-minute budget is not 5")


@override_settings(CACHES=CACHES_LOCMEM)
class TheSearchSpansEveryStore(TestCase):
    """Dutchie's guest search is location-scoped, so a Pullman signup is invisible to a
    Yakima-only search. The number is the customer's; the store is an accident of where
    they first shopped."""

    def test_a_pullman_signup_is_found_from_the_public_page(self):
        def only_pullman(slug, phone):
            return ("710000099", "X", "matched") if slug == "pullman" else ("", "", "new")

        with patch("bundles.loyalty.customers.lookup_by_phone", side_effect=only_pullman), \
             patch("bundles.loyalty._details", return_value={"LoyaltyPoints": 300.0,
                                                            "IsLoyaltyMember": True}) as det:
            state, got = loyalty.balance_for_phone("5095551212")
        self.assertEqual((state, got["points"]), ("found", 300))
        self.assertEqual(det.call_args[0][0], "pullman")

    def test_one_store_being_down_does_not_hide_a_balance_held_at_another(self):
        def yakima_explodes(slug, phone):
            if slug == "yakima":
                raise OSError("register down")
            return ("710000099", "X", "matched") if slug == "pullman" else ("", "", "new")

        with patch("bundles.loyalty.customers.lookup_by_phone", side_effect=yakima_explodes), \
             patch("bundles.loyalty._details", return_value={"LoyaltyPoints": 300.0}):
            self.assertEqual(loyalty.balance_for_phone("5095551212")[1]["points"], 300)
