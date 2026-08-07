"""What actually happens when a crowd presses "Prepare order for pickup" at once.

The owner's question, verbatim: 20 shoppers sit on /custom-order with carts built,
20 more arrive a minute later, and then everyone submits at the same instant. Does
anything cross over — one shopper's phone on another's order, one cart releasing
twice, an order silently lost — and does anything reach Dutchie in a damaging way.

This module answers it by MEASURING rather than reading: N independent Django test
clients each build their own cart (their own cookie, their own draft row, their own
line-up), park, and then all POST /custom-order/checkout through a `threading.Barrier`
so the submits genuinely overlap instead of queueing behind each other.

What is deliberately NOT real here, and why:
  * `bundles.cart.pos_catalog.get_inventory` is stubbed, so no register pull.
  * `bundles.cart.confirm_live_price` is stubbed. Its own in-code guard only trips
    under pytest/BUDTENDER_TESTING, and `manage.py test` is neither — without this
    patch every cart line would fire a real /price-check at a live store.
  * `bundles.customers._client` is stubbed, so `customers.attach` never reaches
    Dutchie's guest search.
  * SMTP is locmem and the cache is locmem, so neither the mail host nor the shared
    production Redis sees test traffic.
Everything else — the view, the repricing, the draft writes, the DB — is the real
code path on a real Postgres.

The numbers this prints are single-process: one Python process, N threads. Production
serves this endpoint from gunicorn `web` with 5 workers x 4 threads (docker-compose.yml),
so the app-level concurrency ceiling there is 20 in-flight requests, and the DB
contention is spread over 5 connections rather than N. Read the correctness results as
authoritative and the wall-clock as a floor, not a forecast.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import Client, TransactionTestCase, override_settings

from budtender.models import PhoneCartDraft
from bundles import cart as cart_mod
from bundles.tests.test_resolver import live

SECRET = "burst-test-secret-value"
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Priced so the biggest cart below (3x25 + 2x8 + 15 = $106) stays under the $300 cap.
CATALOG = {
    "1": 25.0,      # Blue Dream 3.5g
    "10": 8.0,      # Sunset Pre-Roll
    "20": 15.0,     # Marionberry Gummies
}


def inventory():
    return [
        live(product_id="1", name="Blue Dream 3.5g", brand="Athenry", price=25.0, qty=10),
        live(product_id="10", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="Sunset Pre-Roll", brand="Athenry", unit_grams=1.0, price=8.0, qty=10),
        live(product_id="20", cat_key="edibles", cat_label="Edibles", subcategory="10pk",
             name="Marionberry Gummies", brand="Wyld", unit_grams=None, price=15.0, qty=10),
    ]


def basket_for(i: int) -> dict[str, int]:
    """Shopper i's cart — deliberately different per shopper so a mix-up is visible."""
    return {"1": (i % 3) + 1, "10": (i % 2) + 1, "20": 1}


class Shopper:
    """One browser: its own client, its own cart cookie, its own identity."""

    def __init__(self, i: int, ip: str):
        self.i = i
        self.ip = ip
        # raise_request_exception=False: the test Client connects a `got_request_exception`
        # receiver per request, and every connected receiver fires for every exception. With
        # N clients live at once, one thread's 500 would be re-raised inside all the others
        # and destroy the measurement. Take the 500 as a status code instead.
        self.client = Client(raise_request_exception=False, REMOTE_ADDR=ip)
        self.phone = f"509420{i:04d}"
        self.first, self.last = "Burst", f"Shopper{i:02d}"
        self.email = f"burst{i:02d}@example.invalid"
        self.basket = basket_for(i)
        self.token = ""          # draft_token of the cart, captured after building it
        self.status = None       # HTTP status of the checkout POST
        self.elapsed = 0.0
        self.error = ""


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=LOCMEM, BUNDLE_MIN_STOCK=2,
                   BUNDLE_MAX_ORDER_TOTAL=300,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="orders@happytimeweed.com")
class BurstTestCase(TransactionTestCase):
    """TransactionTestCase, not TestCase: the worker threads need to see committed rows.

    Under TestCase every test runs inside one uncommitted transaction on one
    connection, which the threads cannot join — the carts built in setUp would be
    invisible to the thread that submits them.
    """

    databases = {"default"}

    def setUp(self):
        # Fresh inventory list per call, the way the Redis-backed snapshot deserialises
        # a new object for every request — so no thread can mutate another's rows.
        self.inv = patch("bundles.cart.pos_catalog.get_inventory",
                         side_effect=lambda *a, **k: inventory())
        self.inv.start()
        self.addCleanup(self.inv.stop)

        # Hard stop on the live register. `confirm_live_price` only self-disables under
        # pytest or BUDTENDER_TESTING; this runs under `manage.py test`.
        self.price_check = patch("bundles.cart.confirm_live_price", return_value=None)
        self.price_check.start()
        self.addCleanup(self.price_check.stop)

        self.dutchie = MagicMock(name="PosRegisterClient")
        self.dutchie.guest_search.return_value = {"Data": []}
        self.factory = patch("bundles.customers._client", return_value=self.dutchie)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    # ── building the crowd ───────────────────────────────────────────────────
    def _build(self, n: int, *, same_ip: bool = False) -> list[Shopper]:
        """N shoppers who have already filled a cart and are sitting on the page."""
        shoppers = []
        for i in range(n):
            ip = "203.0.113.7" if same_ip else f"198.51.100.{i + 1}"
            s = Shopper(i, ip)
            for pid, qty in s.basket.items():
                r = s.client.post("/custom-order/cart/add",
                                  {"loc": "yakima", "product_id": pid, "qty": qty})
                self.assertEqual(r.status_code, 200, f"cart build failed for shopper {i}")
            s.token = s.client.cookies[cart_mod.COOKIE].value
            self.assertTrue(s.token, f"shopper {i} got no cart cookie")
            shoppers.append(s)
        # Every shopper must own a DISTINCT draft, or the burst proves nothing.
        self.assertEqual(len({s.token for s in shoppers}), n, "carts collided before the burst")
        return shoppers

    # ── the burst ────────────────────────────────────────────────────────────
    def _fire(self, shoppers: list[Shopper]) -> float:
        n = len(shoppers)
        gate = threading.Barrier(n + 1, timeout=60)   # +1: the main thread releases it

        def submit(s: Shopper):
            try:
                gate.wait()
                t = time.perf_counter()
                r = s.client.post("/custom-order/checkout", {
                    "loc": "yakima", "first_name": s.first, "last_name": s.last,
                    "phone": s.phone, "email": s.email,
                })
                s.elapsed = time.perf_counter() - t
                s.status = r.status_code
            except Exception as exc:                  # noqa: BLE001 — record, never abort
                s.error = f"{type(exc).__name__}: {exc}"
            finally:
                connection.close()                    # thread-local; don't leak it

        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(submit, s) for s in shoppers]
            gate.wait()                               # everyone POSTs from here
            t0 = time.perf_counter()
            for f in futures:
                f.result()
            wall = time.perf_counter() - t0
        return wall

    # ── the report ───────────────────────────────────────────────────────────
    def _report(self, label: str, shoppers: list[Shopper], wall: float) -> dict:
        n = len(shoppers)
        codes = Counter(s.status for s in shoppers)
        errors = [s.error for s in shoppers if s.error]

        drafts = {d.draft_token: d for d in PhoneCartDraft.objects.all()}
        released = [d for d in drafts.values() if d.status == PhoneCartDraft.Status.RELEASED]

        # 1. Did any cart turn into more than one order? One cart == one draft row, so
        #    a second release would have to show up as a second `online_order_placed`
        #    stamp in that draft's audit trail, or as extra draft rows appearing.
        double_released = [d.draft_token for d in released
                           if sum(1 for a in (d.audit or [])
                                  if a.get("action") == "online_order_placed") > 1]
        extra_rows = len(drafts) - n

        # 2. Did anyone's identity or lines land on someone else's order?
        wrong_identity, wrong_lines = [], []
        for s in shoppers:
            d = drafts.get(s.token)
            if d is None or d.status != PhoneCartDraft.Status.RELEASED:
                continue
            if (d.contact_phone != s.phone or d.pickup_name != f"{s.first} {s.last}"
                    or d.contact_email != s.email):
                wrong_identity.append(
                    f"{s.token[:12]} expected {s.phone}/{s.first} {s.last} "
                    f"got {d.contact_phone}/{d.pickup_name}")
            got = {str(x.get("product_id")): int(x.get("quantity") or 0) for x in (d.lines or [])}
            if got != s.basket:
                wrong_lines.append(f"{s.token[:12]} expected {s.basket} got {got}")

        # 3. Did every distinct phone stay distinct? Two orders sharing a phone would
        #    mean one shopper's form overwrote another's row.
        phones = Counter(d.contact_phone for d in released)
        dupe_phones = [p for p, c in phones.items() if c > 1]

        # 4. Which shoppers ended with no order at all, and why.
        lost = [s.i for s in shoppers
                if drafts.get(s.token) is None
                or drafts[s.token].status != PhoneCartDraft.Status.RELEASED]

        lat = sorted(s.elapsed for s in shoppers if s.elapsed)
        summary = {
            "n": n, "wall": wall, "codes": dict(codes), "released": len(released),
            "draft_rows": len(drafts), "extra_rows": extra_rows,
            "double_released": double_released, "wrong_identity": wrong_identity,
            "wrong_lines": wrong_lines, "dupe_phones": dupe_phones, "lost": lost,
            "errors": errors,
        }
        print(f"\n╭─ {label} " + "─" * max(0, 58 - len(label)))
        print(f"│ shoppers submitting at once : {n}")
        print(f"│ wall clock for the whole burst: {wall * 1000:.0f} ms")
        if lat:
            print(f"│ per-request latency  min/median/max: "
                  f"{lat[0] * 1000:.0f} / {lat[len(lat) // 2] * 1000:.0f} / {lat[-1] * 1000:.0f} ms")
        print(f"│ HTTP status codes    : {dict(sorted(codes.items(), key=lambda kv: str(kv[0])))}")
        print(f"│ PhoneCartDraft rows  : {len(drafts)} (expected {n}, extra {extra_rows})")
        print(f"│ released orders      : {len(released)}")
        print(f"│ carts released twice : {len(double_released)} {double_released or ''}")
        print(f"│ orders w/ wrong identity : {len(wrong_identity)} {wrong_identity[:3]}")
        print(f"│ orders w/ wrong lines    : {len(wrong_lines)} {wrong_lines[:3]}")
        print(f"│ phone numbers on 2+ orders: {len(dupe_phones)} {dupe_phones[:3]}")
        print(f"│ shoppers with NO order   : {len(lost)} {lost[:10]}")
        print(f"│ unhandled exceptions     : {len(errors)} {errors[:2]}")
        print(f"│ dutchie guest_search calls: {self.dutchie.guest_search.call_count}")
        print("╰" + "─" * 62)
        return summary

    def _assert_no_crossover(self, r: dict):
        """The properties that must hold no matter how many people submit at once."""
        self.assertEqual(r["errors"], [], "a checkout raised out of the view")
        self.assertEqual(r["extra_rows"], 0, "the burst created draft rows nobody owns")
        self.assertEqual(r["double_released"], [], "a cart released twice")
        self.assertEqual(r["wrong_identity"], [], "an order carries the wrong shopper")
        self.assertEqual(r["wrong_lines"], [], "an order carries the wrong cart's lines")
        self.assertEqual(r["dupe_phones"], [], "two orders share one phone number")
        self.assertNotIn(500, r["codes"], "the burst produced a 500")


class Burst20Tests(BurstTestCase):
    def test_twenty_distinct_shoppers_submit_at_the_same_instant(self):
        shoppers = self._build(20)
        wall = self._fire(shoppers)
        r = self._report("20 shoppers, 20 distinct IPs", shoppers, wall)
        self._assert_no_crossover(r)
        self.assertEqual(r["released"], 20, "not every shopper got an order")
        self.assertEqual(r["codes"].get(200), 20)


class Burst40Tests(BurstTestCase):
    def test_forty_distinct_shoppers_submit_at_the_same_instant(self):
        """The owner's real number: 20 parked for a minute, 20 more, all submit together.

        Modelled as 40 carts built up front and one simultaneous release — strictly
        harsher than the staggered arrival described, since nothing has drained.
        """
        shoppers = self._build(40)
        wall = self._fire(shoppers)
        r = self._report("40 shoppers, 40 distinct IPs", shoppers, wall)
        self._assert_no_crossover(r)
        self.assertEqual(r["released"], 40, "not every shopper got an order")
        self.assertEqual(r["codes"].get(200), 40)


class BurstBehindOneIpTests(BurstTestCase):
    def test_forty_shoppers_behind_one_ip_hit_the_hourly_throttle(self):
        """Same crowd, one shared egress IP — an office, a hotel, carrier-grade NAT.

        `@rate_limit("bundle-checkout", limit=20, window=3600)` in bundles/views.py is
        keyed on (scope, ip), so a shared IP is a shared budget: the 21st checkout in
        the hour gets a bare 429 and loses nothing but its order. Pinned here because
        it is the ONLY way this burst drops orders, and it is invisible from the code
        until you count.
        """
        shoppers = self._build(40, same_ip=True)
        wall = self._fire(shoppers)
        r = self._report("40 shoppers, ONE shared IP", shoppers, wall)
        # Still no corruption — throttled shoppers simply never reach the view.
        self._assert_no_crossover(r)
        self.assertEqual(r["released"], 20, "the throttle let a different number through")
        self.assertEqual(r["codes"].get(429), 20)
        # A 429 must leave the cart intact so a retry works.
        for i in r["lost"]:
            draft = PhoneCartDraft.objects.get(draft_token=shoppers[i].token)
            self.assertEqual(draft.status, PhoneCartDraft.Status.OPEN)
            self.assertEqual(draft.contact_phone, "")


class DoubleClickRaceTests(BurstTestCase):
    """The OTHER burst: one shopper, one cart, several submits in flight together.

    `test_checkout_flow.CheckoutIdempotencyTests` pins the SEQUENTIAL double-submit —
    the second request finds the draft no longer `open` and lands on the empty-cart
    page. That guard is a read (`get_cart` filtering `status=open`) and a write
    (`draft.save()`) with nothing holding the row in between, so it only works when
    the first write commits before the second read. Fire them together and it doesn't.

    Measured, 10 consecutive runs at 5 tabs: 2-5 confirmation emails every time, and
    always exactly ONE stamp in the audit trail. Both follow from the same thing —
    every racing request loads its own copy of the row, sends its own email, appends
    its stamp to the `audit` list it read, and saves the whole row. Last write wins,
    so the extra releases leave no trace in the order they overwrote.

    The order itself is never duplicated: a `PhoneCartDraft` IS the cart, so a second
    release rewrites that same row rather than creating a second one. The staff queue
    shows one order with the right lines. What escapes is the side effects.
    """

    def _race(self, tabs_n: int, label: str):
        from django.core import mail

        base = self._build(1)[0]
        tabs = []
        for _ in range(tabs_n):
            c = Client(raise_request_exception=False, REMOTE_ADDR=base.ip)
            c.cookies[cart_mod.COOKIE] = base.token
            tab = Shopper(base.i, base.ip)
            tab.client, tab.token, tab.basket = c, base.token, base.basket
            tab.phone, tab.first, tab.last, tab.email = (
                base.phone, base.first, base.last, base.email)
            tabs.append(tab)

        mail.outbox = []
        lookups_before = self.dutchie.guest_search.call_count
        wall = self._fire(tabs)

        draft = PhoneCartDraft.objects.get(draft_token=base.token)
        stamps = sum(1 for a in (draft.audit or []) if a.get("action") == "online_order_placed")
        codes = Counter(t.status for t in tabs)
        print(f"\n╭─ {label} " + "─" * max(0, 58 - len(label)))
        print(f"│ wall clock          : {wall * 1000:.0f} ms")
        print(f"│ HTTP status codes   : {dict(codes)}")
        print(f"│ PhoneCartDraft rows : {PhoneCartDraft.objects.count()} (a second ORDER is "
              f"impossible — the cart IS the row)")
        print(f"│ requests that got past the `status=open` guard (emails sent): {len(mail.outbox)}")
        print(f"│ 'online_order_placed' stamps left on the row: {stamps}")
        print(f"│ Dutchie guest_search calls for this one order: "
              f"{self.dutchie.guest_search.call_count - lookups_before}")
        print(f"│ final status / phone / lines: {draft.status} / {draft.contact_phone} / "
              f"{[(x.get('product_id'), x.get('quantity')) for x in draft.lines]}")
        print("╰" + "─" * 62)

        # The order survives intact — the property that protects money and trust.
        self.assertEqual(PhoneCartDraft.objects.count(), 1)
        self.assertEqual(draft.status, PhoneCartDraft.Status.RELEASED)
        self.assertEqual(draft.contact_phone, base.phone)
        self.assertEqual({str(x["product_id"]): x["quantity"] for x in draft.lines}, base.basket)
        self.assertNotIn(500, codes)
        # Bounded, not pinned to 1: this IS the finding, and forcing it to 1 here would
        # only make the test lie about a race that is timing-dependent by nature.
        self.assertGreaterEqual(len(mail.outbox), 1, "the shopper was never confirmed")
        self.assertLessEqual(len(mail.outbox), tabs_n)
        return len(mail.outbox), stamps

    def test_a_double_click_can_confirm_the_same_order_twice(self):
        self._race(2, "1 cart, 2 simultaneous submits (a double-click)")

    def test_five_tabs_on_one_cart_still_produce_one_order(self):
        emails, stamps = self._race(5, "1 cart, 5 simultaneous submits")
        # The audit trail cannot show what the last writer overwrote.
        self.assertEqual(stamps, 1, "the audit trail suddenly records the extra releases")
        if emails > 1:
            print(f"  → {emails} shoppers-worth of email for ONE order; audit says {stamps}")


class BurstWithDutchieLatencyTests(BurstTestCase):
    def test_twenty_submit_while_dutchie_guest_search_takes_300ms(self):
        """Every checkout blocks on one Dutchie guest lookup (`customers.attach`).

        Stubbing it to return instantly measures the app in a vacuum; the register
        answers in hundreds of milliseconds. This pins whether 20 overlapping lookups
        overlap (wall ~= one lookup) or serialise (wall ~= 20 lookups).
        """
        def slow(*a, **k):
            time.sleep(0.3)
            return {"Data": []}

        self.dutchie.guest_search.side_effect = slow
        shoppers = self._build(20)
        wall = self._fire(shoppers)
        r = self._report("20 shoppers, Dutchie lookup at 300 ms", shoppers, wall)
        self._assert_no_crossover(r)
        self.assertEqual(r["released"], 20)
        # Overlapping, not queued: 20 serial lookups alone would be 6 s.
        self.assertLess(wall, 6.0, "the Dutchie lookups serialised")
