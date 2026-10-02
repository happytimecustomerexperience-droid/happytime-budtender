"""Three ways to abuse the public checkout, found by an adversarial read of the deployed
/custom-order storefront (round W9). Each test here fails on the code that review ran against.

Orders arrive through the happytimeweed.com Vercel rewrite, so every shopper shares one
egress IP and an IP bucket is one bucket for the whole site — the only limits that mean
anything key on the thing abused (see caps.py) or count what exists in the database.

  1. Shelf hold: a RELEASED order holds stock for DRAFT_TTL_HOURS, the per-phone cap is
     dodged by inventing a phone, and the 300/h throttle is shared by every real shopper.
     A script could hold the shelf and flood the staff queue.
  2. Mail-bomb: the per-email cap keyed on the typed string, so `victim+1@gmail.com`,
     `v.ictim@gmail.com` ... were all "different" and all reached one inbox.
  3. Oversell: stock was re-checked, then — with nothing held in between — the order was
     released, so N simultaneous checkouts for the last two units were all confirmed.

Nothing here touches the network: Dutchie is stubbed by the shared base class.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core import mail
from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings
from django.utils import timezone

from budtender.models import PhoneCartDraft
from bundles import cart as cart_mod
from bundles import views
from bundles.catalog import store_info
from bundles.tests.test_checkout_flow import FIRST, LAST, SMTP, CheckoutFlowTestCase
from bundles.tests.test_resolver import live

ONLINE, VOICE = PhoneCartDraft.Source.ONLINE, PhoneCartDraft.Source.VOICE
RELEASED, CLAIMED = PhoneCartDraft.Status.RELEASED, PhoneCartDraft.Status.CLAIMED


def phones():
    return (f"50942{n:05d}" for n in range(1, 100000))


def held_order(units=1, *, store="yakima", status=RELEASED, source=ONLINE, ttl_hours=4,
               released_ago_hours=0, pid="77"):
    """An order already in the staff queue — what a bot (or a rival shopper) leaves behind."""
    now = timezone.now()
    return PhoneCartDraft.objects.create(
        location_slug=store, source=source, status=status,
        released_at=now - timedelta(hours=released_ago_hours),
        expires_at=now + timedelta(hours=ttl_hours),
        lines=[{"product_id": pid, "name": "Held Item", "quantity": units, "in_stock": True}])


def last_two():
    return [live(product_id="1", name="Last Two 3.5g", price=25.0, qty=2)]


class OrderRig(CheckoutFlowTestCase):
    """One browser, a fresh cart per order (checkout clears the cookie), a new phone each time."""

    def setUp(self):
        super().setUp()
        self.fresh = phones()

    def _order(self, *, qty=1, loc="yakima", phone=None, email="", inv=None):
        # A shopper whose checkout was refused keeps their cart (and cookie); a new
        # order is a new browser, or adding one more unit would grow the refused cart.
        self.client.cookies.clear()
        self._add("1", qty=qty, loc=loc, inv=inv)
        return self._checkout(phone=phone or next(self.fresh), email=email, loc=loc, inv=inv)

    def _call_line(self, store="yakima"):
        info = store_info(store)
        return f"Please call {info['label']} at {info['phone']}"


# ── 1. a script cannot hold a store's shelf ──────────────────────────────────
class ShelfHoldCapTests(OrderRig):
    @override_settings(BUNDLES_MAX_OPEN_ONLINE_ORDERS_PER_STORE=2)
    def test_open_online_orders_are_capped_per_store_even_with_a_new_phone_each_time(self):
        codes = [self._order().status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 429, 429])
        self.assertEqual(self._released().count(), 2)

    @override_settings(BUNDLES_MAX_OPEN_ONLINE_ORDERS_PER_STORE=1)
    def test_the_refusal_tells_the_shopper_to_call_and_holds_nothing(self):
        self._order()
        r = self._order()
        self.assertContains(r, "We can&#x27;t take another online order right now.", status_code=429)
        self.assertContains(r, self._call_line(), status_code=429)
        cart = PhoneCartDraft.objects.exclude(status=RELEASED).get()
        self.assertEqual(cart.status, PhoneCartDraft.Status.OPEN)
        self.assertIsNone(cart.released_at)
        self.assertEqual(cart.contact_phone, "")
        self.assertEqual(cart_mod.online_holds("yakima"), (1, 1))      # only the first order holds

    def test_the_default_is_forty_orders_a_store_counted_from_the_database(self):
        for _ in range(40):
            held_order()
        self.assertEqual(self._order().status_code, 429)
        cache.clear()                  # a flushed cache must not reset a limit on what exists
        self.assertEqual(self._order().status_code, 429)
        self.assertEqual(self._released().count(), 40)

    def test_another_store_is_not_locked_out(self):
        for _ in range(40):
            held_order(store="yakima")
        self.assertEqual(self._order(loc="pullman").status_code, 200)

    @override_settings(BUNDLES_MAX_OPEN_ONLINE_ORDERS_PER_STORE=2)
    def test_only_open_unclaimed_online_orders_count(self):
        held_order(ttl_hours=-1)                    # expired: stopped holding
        held_order(status=CLAIMED)                  # a budtender has it
        held_order(source=VOICE)                    # a phone call, not the storefront
        held_order(store="pullman")
        self.assertEqual([self._order().status_code for _ in range(3)], [200, 200, 429])

    @override_settings(BUNDLES_MAX_HELD_UNITS_PER_STORE=3)
    def test_units_held_are_capped_per_store(self):
        self.assertEqual(self._order(qty=2).status_code, 200)
        self.assertEqual(self._order(qty=2).status_code, 429)          # 2 + 2 > 3
        self.assertEqual(self._order(qty=1).status_code, 200)          # 2 + 1 fits
        self.assertEqual(cart_mod.online_holds("yakima"), (2, 3))

    def test_the_default_is_one_hundred_fifty_units(self):
        held_order(units=149)
        self.assertEqual(self._order(qty=2).status_code, 429)
        self.assertEqual(self._order(qty=1).status_code, 200)

    @override_settings(BUNDLES_MAX_ORDERS_PER_HOUR=3)
    def test_the_site_places_a_bounded_number_of_orders_an_hour_counted_from_the_database(self):
        held_order(status=CLAIMED, released_ago_hours=2)               # last hour's news
        held_order(source=VOICE, status=CLAIMED)                       # not the storefront
        self.assertEqual([self._order().status_code for _ in range(3)], [200, 200, 200])
        cache.clear()                  # the per-phone caps and the IP throttle reset...
        r = self._order()
        self.assertContains(r, self._call_line(), status_code=429)     # ...this does not
        self.assertEqual(self._released().filter(source=ONLINE).count(), 3)

    @override_settings(BUNDLES_MAX_ORDERS_PER_HOUR=2)
    def test_a_claimed_order_still_counts_as_placed_this_hour(self):
        held_order(status=CLAIMED)
        held_order(status=CLAIMED)
        self.assertEqual(self._order().status_code, 429)

    @override_settings(BUNDLES_MAX_OPEN_ONLINE_ORDERS_PER_STORE=1)
    def test_a_refusal_hands_back_the_shoppers_phone_and_email_budget(self):
        self._order()
        phone, email = next(self.fresh), "sam.reyes@example.com"
        self.assertEqual(self._order(phone=phone, email=email).status_code, 429)
        # The refused cart is still theirs and still OPEN; once there is room the same
        # shopper may place all three of their daily orders — the refusal cost them none.
        with override_settings(BUNDLES_MAX_OPEN_ONLINE_ORDERS_PER_STORE=40):
            codes = [self._checkout(phone=phone, email=email).status_code]
            codes += [self._order(phone=phone, email=email).status_code for _ in range(2)]
        self.assertEqual(codes, [200, 200, 200])

    @override_settings(BUNDLES_MAX_OPEN_ONLINE_ORDERS_PER_STORE="forty")
    def test_a_malformed_setting_falls_back_to_the_default_instead_of_a_500(self):
        self.assertEqual(self._order().status_code, 200)


# ── 2. our sender is not a mail cannon ───────────────────────────────────────
@override_settings(**SMTP)
class MailBombTests(OrderRig):
    def test_plus_tags_and_gmail_dots_count_as_one_inbox(self):
        typed = ("victim+1@gmail.com", "v.ictim@gmail.com", "VICTIM+news@googlemail.com",
                 "victim@gmail.com")
        codes = [self._order(email=e).status_code for e in typed]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(len(mail.outbox), 3)

    def test_the_confirmation_still_goes_to_the_address_as_typed(self):
        self._order(email="Victim+1@Gmail.com")
        self.assertEqual(mail.outbox[0].to, ["Victim+1@Gmail.com"])

    def test_a_plus_tag_is_dropped_on_every_domain(self):
        codes = [self._order(email=f"a+{n}@example.com").status_code for n in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    @override_settings(BUNDLES_MAX_CONFIRMATION_EMAILS_PER_HOUR=2)
    def test_a_site_wide_hourly_ceiling_stops_the_mail_but_not_the_order(self):
        codes = [self._order(email=f"person{n}@example.com").status_code for n in range(2)]
        with self.assertLogs("bundles.views", "WARNING") as logged:
            codes.append(self._order(email="person9@example.com").status_code)
        self.assertEqual(codes, [200, 200, 200])
        self.assertEqual(self._released().count(), 3)
        self.assertEqual([m.to for m in mail.outbox], [["person0@example.com"], ["person1@example.com"]])
        self.assertIn("no email sent", logged.output[0])

    @override_settings(BUNDLES_MAX_CONFIRMATION_EMAILS_PER_HOUR=1)
    def test_orders_without_an_email_do_not_spend_the_ceiling(self):
        for _ in range(3):
            self._order()
        self.assertEqual(self._order(email="person0@example.com").status_code, 200)
        self.assertEqual(len(mail.outbox), 1)


class EmailKeyTests(SimpleTestCase):
    def test_the_counting_key(self):
        cases = {
            "Victim@Gmail.com": "victim@gmail.com",
            "victim+1@gmail.com": "victim@gmail.com",
            "v.i.c.t.i.m+x.y@gmail.com": "victim@gmail.com",
            "victim@googlemail.com": "victim@gmail.com",
            "victim@gmail.com.": "victim@gmail.com",
            "a+tag@example.com": "a@example.com",
            "a.b@example.com": "a.b@example.com",     # dots only mean nothing at Gmail
            "+tag@example.com": "+tag@example.com",     # nothing left to count: keep it
        }
        for typed, key in cases.items():
            with self.subTest(typed=typed):
                self.assertEqual(views._email_key(typed), key)

    def test_different_mailboxes_stay_different(self):
        self.assertNotEqual(views._email_key("a.b@example.com"), views._email_key("ab@example.com"))
        self.assertNotEqual(views._email_key("a@gmail.com"), views._email_key("a@example.com"))


# ── 3. the last two units are not confirmed N times ──────────────────────────
@override_settings(**SMTP)
class OversellTests(OrderRig):
    def _rival_places(self, units):
        """Another checkout commits after this shopper's page was priced but before this
        one is written — the window that used to be unguarded. `customers.attach` runs
        exactly there, so it is where the rival lands."""
        return lambda draft: held_order(units, pid="1")

    def test_a_rival_that_lands_between_the_price_check_and_the_release_wins_the_unit(self):
        self._add("1", qty=2, inv=last_two())
        with patch("bundles.customers.attach", side_effect=self._rival_places(1)):
            r = self._checkout(inv=last_two(), email="")
        self.assertContains(r, "Some items changed", status_code=400)
        orders = self._released()
        self.assertEqual(orders.count(), 1, "the shelf holds 2 units; 3 were promised")
        self.assertEqual(sum(x["quantity"] for o in orders for x in o.lines), 1)
        self.assertEqual(len(mail.outbox), 0)
        mine = PhoneCartDraft.objects.exclude(status=RELEASED).get()
        self.assertEqual(mine.status, PhoneCartDraft.Status.OPEN)
        self.assertEqual(mine.contact_phone, "")

    def test_the_refused_cart_is_put_back_for_the_shopper_to_fix(self):
        self._add("1", qty=2, inv=last_two())
        with patch("bundles.customers.attach", side_effect=self._rival_places(1)):
            self._checkout(inv=last_two())
        mine = PhoneCartDraft.objects.exclude(status=RELEASED).get()
        self.assertEqual([(x["quantity"], x.get("issue")) for x in mine.lines], [(1, "reduced")])
        # ...and the shopper can simply check out the one that is left.
        self.assertEqual(self._checkout(inv=last_two()).status_code, 200)

    def test_two_checkouts_in_sequence_for_the_last_units_give_one_confirmation(self):
        carts = []
        for _ in range(2):
            c = Client()
            with self._patch_inv(last_two()):
                c.post("/custom-order/cart/add", {"loc": "yakima", "product_id": "1", "qty": 2})
            carts.append(c)
        codes = []
        for c in carts:
            with self._patch_inv(last_two()):
                codes.append(c.post("/custom-order/checkout", {
                    "loc": "yakima", "first_name": FIRST, "last_name": LAST,
                    "phone": next(self.fresh), "email": "sam.reyes@example.com"}).status_code)
        self.assertEqual(codes, [200, 400])
        self.assertEqual(len(mail.outbox), 1)

    def test_a_double_click_whose_first_tab_already_released_it_is_not_refused_as_oversold(self):
        # The other tab released THIS cart, so its own two units are "held" — they must not
        # be counted against it, or the shopper who did nothing wrong sees an error page for
        # an order that exists.
        def other_tab_wins(draft):
            PhoneCartDraft.objects.filter(pk=draft.pk).update(
                status=RELEASED, released_at=timezone.now(),
                expires_at=timezone.now() + timedelta(hours=4))

        self._add("1", qty=2, inv=last_two())
        with patch("bundles.customers.attach", side_effect=other_tab_wins):
            r = self._checkout(inv=last_two())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)          # the winning tab sends it, not this one

    def test_the_customer_lookup_runs_outside_the_lock_and_the_recheck_inside_it(self):
        calls = []
        self._add("1")
        with patch("bundles.customers.attach", side_effect=lambda d: calls.append("lookup")), \
                patch("bundles.cart.lock_store", side_effect=lambda s: calls.append(f"lock:{s}")), \
                patch("bundles.cart.order_blocker",
                      side_effect=lambda d, inv: calls.append("recheck") or ""):
            self._checkout()
        self.assertEqual(calls, ["lookup", "lock:yakima", "recheck"])


class StoreLockTests(SimpleTestCase):
    """The lock itself. No Postgres here, so the SQL is checked against a stand-in connection."""

    def _run(self, vendor, store):
        fake = MagicMock()
        fake.vendor = vendor
        with patch("bundles.cart.connection", fake):
            cart_mod.lock_store(store)
        return fake

    def test_postgres_takes_a_transaction_scoped_advisory_lock_per_store(self):
        fake = self._run("postgresql", "yakima")
        sql, params = fake.cursor.return_value.__enter__.return_value.execute.call_args[0]
        self.assertEqual(sql, "SELECT pg_advisory_xact_lock(%s)")
        self.assertIsInstance(params[0], int)
        self.assertTrue(0 < params[0] < 2 ** 63, "must fit Postgres bigint")

    def test_the_lock_key_is_stable_and_differs_per_store(self):
        def key(store):
            fake = self._run("postgresql", store)
            return fake.cursor.return_value.__enter__.return_value.execute.call_args[0][1][0]

        self.assertEqual(key("yakima"), key("yakima"))
        self.assertEqual(len({key(s) for s in ("yakima", "pullman", "mount-vernon")}), 3)

    def test_sqlite_has_no_advisory_lock_and_is_left_alone(self):
        self._run("sqlite", "yakima").cursor.assert_not_called()
