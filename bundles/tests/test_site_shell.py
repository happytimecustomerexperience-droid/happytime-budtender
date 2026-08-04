"""The storefront is proxied to happytimeweed.com/custom-order, so it carries the
site's chrome itself.

A Next.js rewrite forwards HTML — it does NOT run the site's React layout. That has
two consequences this file guards:

  * the header, footer and branding have to be in THIS template, or the page lands
    on happytimeweed.com looking like a different website;
  * the site's age gate is a React component, so it never runs here — without the
    inline gate below, the storefront would be the one un-gated page on a cannabis
    site.

Site assets are absolute (SITE_ORIGIN) rather than root-relative on purpose: they
must resolve both through the proxy and when budtender-api is opened directly,
where /media/* does not exist.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from bundles import signing
from bundles.tests.test_resolver import live

SECRET = "unit-test-secret-value"
SITE = "https://happytimeweed.com"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def inventory():
    return [
        live(product_id="1", name="Blue Dream 3.5g", price=25.0),
        live(product_id="10", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="PR One", unit_grams=1.0, price=8.0),
        live(product_id="20", cat_key="edibles", cat_label="Edibles", subcategory="10pk",
             name="Gummies", unit_grams=None, price=15.0),
    ]


def patch_inv(inv=None):
    return patch("bundles.cart.pos_catalog.get_inventory",
                 return_value=inv if inv is not None else inventory())


@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM,
                   SITE_ORIGIN=SITE)
class SiteChromeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

    def _pages(self):
        """Every FULL page a shopper can land on.

        /cart and /results are deliberately excluded — they return HTMX fragments
        that get swapped into a page that already has the chrome. Asserting chrome
        on them would be asserting a bug.

        The broken-link page IS included. It renders base.html like the rest, and it
        once shipped without the shell context — "Pickup at ." and an empty footer,
        shown to the one visitor whose link already failed.
        """
        url = signing.build_url("/custom-order/", bundle="roll-relax", store="yakima",
                                items=[("1", 1), ("10", 2), ("20", 1)])
        with patch_inv():
            return {
                "landing": self.client.get(url),
                "menu": self.client.get("/custom-order/menu?loc=yakima"),
                "checkout": self.client.get("/custom-order/checkout?loc=yakima"),
                "invalid": self.client.get("/custom-order/?b=roll-relax&sig=nope"),
            }

    def test_the_broken_link_page_is_a_finished_page(self):
        r = self._pages()["invalid"]
        body = r.content.decode()
        self.assertEqual(r.status_code, 400)
        self.assertIn("This link didn't open", body)
        self.assertIn("Pickup at Happy Time — Yakima", body)
        self.assertIn("1315 N 1st St", body)
        self.assertNotIn("Pickup at </strong>", body)

    def test_the_htmx_fragments_stay_fragments(self):
        """Guard the exclusion above: if /cart ever grows a <html> it is being
        swapped into a page that already has one, and the result is a nested
        document."""
        with patch_inv():
            for path in ("/custom-order/cart?loc=yakima", "/custom-order/results?loc=yakima"):
                body = self.client.get(path).content.decode().lower()
                self.assertNotIn("<html", body, f"{path} should be a fragment")
                self.assertNotIn("<header", body, f"{path} should be a fragment")

    def test_every_page_carries_the_site_logo_and_nav(self):
        for name, r in self._pages().items():
            body = r.content.decode()
            self.assertIn(f"{SITE}/media/logo-happy-time.png", body, f"{name}: no site logo")
            self.assertIn(f"{SITE}/specials", body, f"{name}: no site nav")
            self.assertIn(f"{SITE}/locations", body, f"{name}: no site nav")

    def test_site_links_are_absolute_so_they_work_on_both_hosts(self):
        # Root-relative /media/... 404s when budtender-api is opened directly.
        body = self._pages()["menu"].content.decode()
        self.assertNotIn('src="/media/', body)
        self.assertNotIn('href="/specials"', body)

    def test_every_page_still_names_the_store(self):
        for name, r in self._pages().items():
            self.assertIn("Happy Time — Yakima", r.content.decode(), f"{name}: store missing")

    def test_footer_keeps_the_required_warning(self):
        for name, r in self._pages().items():
            self.assertIn("intoxicating effects", r.content.decode(),
                          f"{name}: WAC 314-55-155(7) warning missing")

    def test_site_origin_is_configurable(self):
        with override_settings(SITE_ORIGIN="https://staging.example.com"):
            body = self._pages()["menu"].content.decode()
        self.assertIn("https://staging.example.com/media/logo-happy-time.png", body)


@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2, CACHES=CACHES_LOCMEM,
                   SITE_ORIGIN=SITE)
class AgeGateTests(TestCase):
    """The site gates every other page in React; a rewrite skips React entirely."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()

    def _body(self, path="/custom-order/menu?loc=yakima"):
        with patch_inv():
            return self.client.get(path).content.decode()

    def test_the_gate_is_present_on_every_full_page(self):
        # Fragments (/cart, /results) are swapped into a page that already has it.
        for path in ("/custom-order/menu?loc=yakima", "/custom-order/checkout?loc=yakima"):
            self.assertIn('id="htco-age"', self._body(path), f"{path}: no age gate")

    def test_it_reuses_the_site_storage_keys_so_nobody_verifies_twice(self):
        # If these drift from AgeVerification.tsx, a shopper who already answered on
        # the marketing site gets asked again the moment they open the storefront.
        body = self._body()
        self.assertIn("happytime-age-session", body)
        self.assertIn("happytime-age-verified", body)

    def test_the_gate_script_is_inline_and_not_deferred(self):
        # A deferred gate paints the storefront first and then covers it — the
        # products are visible to an unverified visitor for that frame.
        body = self._body()
        gate_at = body.index('id="htco-age"')
        script_at = body.index("happytime-age-session")
        self.assertLess(script_at - gate_at, 2000,
                        "the gate script should sit immediately after its markup")
        self.assertNotIn('src="/static/bundles/bundle.js" defer></script>\n<div id="htco-age"',
                         body)

    def test_it_fails_closed_when_storage_is_unavailable(self):
        # Private mode / blocked storage throws on getItem; the catch must return
        # false (show the gate) rather than true (let everyone through).
        body = self._body()
        self.assertIn("catch (e) { return false; }", body)
