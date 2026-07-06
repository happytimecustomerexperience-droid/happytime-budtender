"""Insights dashboard views — staff gating, render, degrade, self-service scope."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from customers.models import ShopVisit

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(db):
    # independent Client (not the shared `client`) so staff + worker can coexist in one test
    c = Client()
    c.force_login(User.objects.create_user("mgr", password="pw12345!", is_staff=True))
    return c


@pytest.fixture
def worker(db):
    c = Client()
    c.force_login(User.objects.create_user("bud", password="pw12345!"))
    return c


@pytest.mark.parametrize("name", ["insights_overview", "insights_products",
                                   "insights_customers", "insights_budtenders"])
def test_manager_dashboards_staff_only(staff, worker, name):
    url = reverse(name)
    assert staff.get(url).status_code == 200
    assert worker.get(url).status_code == 403          # non-staff forbidden


def test_overview_renders(staff):
    ShopVisit.objects.create(store="yakima", budtender="bud", outcome="checked_out",
                             items_added=2, items_viewed=3, cart_total=40,
                             ended_at=timezone.now())
    r = staff.get(reverse("insights_overview"))
    assert r.status_code == 200 and b"Checkout rate" in r.content


def test_customers_degrades_without_profile_db(staff):
    # no CUSTOMER_DB_DSN configured in tests -> empty state, never a 500
    r = staff.get(reverse("insights_customers"))
    assert r.status_code == 200 and b"CUSTOMER_DB_DSN" in r.content


def test_my_stats_is_self_service_no_staff_needed(worker):
    r = worker.get(reverse("my_stats"))
    assert r.status_code == 200 and b"My performance" in r.content


def test_dashboards_require_login(client):
    assert client.get(reverse("insights_overview")).status_code == 302   # -> login
