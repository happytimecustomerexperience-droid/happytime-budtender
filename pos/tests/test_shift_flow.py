"""One whole shift, walked end to end: sign in -> pick a till -> door -> queue -> sell.

Every hop in this chain already had a test. What none of them proved is that the
hops CONNECT — that the register the door person picks is one Dutchie actually has,
that the door and budtender screens really are different screens, and that a person
scanned at the door arrives on the budtender's queue as a customer they can claim
and take to the menu. A chain of green links is not a green chain.

Anchored to a live capture of the real Dutchie POS signing in
(net-capture ash.pos.dutchie.com, 2026-08-09) and to a live probe of our own three
stores. Two facts from those runs are load-bearing here:

  * the register list is a genuine server call, POST /api/posv3/registers/get, and it
    returns {id, TerminalName, RoomNo} — Yakima has 6 tills, Pullman 2, Mount
    Vernon 2. A hardcoded picker would drift the day someone adds a register.
  * Dutchie's own EmployeeLogin response carries no role of any kind, which is why
    door-vs-budtender is ours to decide and ours to enforce.

Dutchie is never contacted here: the register client, the store table and the
scanner are all patched.
"""
import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from customers.models import ShopVisit
from dutchie.session import Store
from pos import views as V
from pos.dutchie_auth import DOOR_ONLY_GROUP

pytestmark = pytest.mark.django_db

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=8002, lsp_id=1745, loc_id=3498, register_id=8318,
              username="svc", password="p", api_key="k")

# Verbatim from the capture — the shape registers/get really returns.
LIVE_REGISTERS = [
    {"id": 8239, "TerminalName": "Register 1", "RoomId": 26320, "RoomNo": "Sales Floor"},
    {"id": 8264, "TerminalName": "Register 2", "RoomId": 26320, "RoomNo": "Sales Floor"},
    {"id": 8318, "TerminalName": "Register Test", "RoomId": 26320, "RoomNo": "Sales Floor"},
]

SCAN_OK = {"first_name": "Jane", "last_name": "Doe", "accts_name": "Jane Doe",
           "birth_date": "1990-01-15", "id_number": "DL-7", "over_21": True}
SCAN_MINOR = {**SCAN_OK, "accts_name": "Kid Doe", "birth_date": "2010-01-15", "over_21": False}


class DutchieDouble:
    """Only the register-client methods this walk touches."""

    def __init__(self):
        self.checked_in = []

    def get_registers(self):
        return LIVE_REGISTERS

    def guest_search(self, q=""):
        return {"Data": []}                      # nobody on file -> the create path

    def create_guest(self, **kw):
        return 710000099

    def guest_details_light(self, acct_id):
        return {"Data": {"Guest_id": acct_id, "Name": "Jane Doe", "PhoneNo": "5095550100"}}


@pytest.fixture
def dutchie(monkeypatch):
    double = DutchieDouble()
    monkeypatch.setattr(V, "load_stores", lambda: {STORE.name: STORE})
    monkeypatch.setattr(V, "_client", lambda store: double)
    monkeypatch.setattr(V, "_rest_client", lambda store: None)
    monkeypatch.setattr(V, "_backoffice_client", lambda store: None)
    monkeypatch.setattr(V.dutchie_auth, "verify",
                        lambda store, u, p: {"user_id": 95602, "session_gid": "S",
                                             "cookie_header": "LLSession=x"})
    # _all_registers builds its own client, so patch the class it reaches for.
    monkeypatch.setattr(V, "PosRegisterClient", lambda store: double)
    return double


def sign_in(client, role, register="8318", username="ann"):
    return client.post(reverse("login"),
                       {"username": username, "password": "pw", "location": "yakima",
                        "role": role, "register": register}, SERVER_NAME="localhost")


# ── 1. the till list is Dutchie's, not ours ──────────────────────────────────
def test_the_register_picker_is_fed_by_dutchie(client, dutchie):
    regs = V._all_registers()
    assert [r["id"] for r in regs] == [8239, 8264, 8318]
    assert [r["name"] for r in regs] == ["Register 1", "Register 2", "Register Test"]
    assert all(r["store"] == "yakima" and r["room"] == "Sales Floor" for r in regs)

    page = client.get(reverse("login"), SERVER_NAME="localhost").content.decode()
    for name in ("Register 1", "Register 2", "Register Test"):
        assert name in page, f"{name} is missing from the sign-in page"


def test_a_till_that_is_not_on_that_store_is_refused(client, dutchie):
    sign_in(client, "budtender")
    r = client.post(reverse("set_register"), {"register": "999999"}, SERVER_NAME="localhost")
    assert client.session["register_id"] == "8318", "an unknown till was accepted"
    assert r.status_code in (200, 400)


def test_the_picked_till_is_recorded_by_name_on_the_shift(client, dutchie):
    sign_in(client, "budtender", register="8264")
    from customers.models import StaffSession
    shift = StaffSession.objects.get()
    assert shift.register_id == "8264" and shift.register_name == "Register 2"


# ── 2. door and budtender are different screens ──────────────────────────────
def test_door_lands_on_the_door_screen_and_budtender_on_the_station(client, dutchie):
    assert sign_in(client, "door")["Location"] == reverse("door")
    client.logout()
    assert sign_in(client, "budtender")["Location"] == reverse("screen")


def test_the_door_screen_has_no_way_to_sell(client, dutchie):
    sign_in(client, "door")
    body = client.get(reverse("door"), SERVER_NAME="localhost").content.decode()
    assert 'id="cart-drawer"' not in body, "the door screen offers a cart"
    assert "cart_submit" not in body and "photo_match" not in body
    assert reverse("shop") not in body, "the door screen links to the menu"


def test_the_station_is_the_other_screen_entirely(client, dutchie):
    sign_in(client, "budtender")
    body = client.get(reverse("screen"), SERVER_NAME="localhost").content.decode()
    assert 'class="queuewrap queuegrid"' in body          # queue + saved orders
    assert 'name="phone"' in body                          # the one search box
    assert 'data-autostart="1"' in body                    # camera already live
    assert 'id="cart-drawer"' in body                      # and it CAN sell


def test_a_door_shift_cannot_reach_the_station_or_the_queue_feed(client, dutchie):
    sign_in(client, "door")
    station = client.get(reverse("screen"), SERVER_NAME="localhost").content.decode()
    assert "Door role" in station and 'class="queuewrap' not in station
    assert client.get(reverse("queue_panel"), SERVER_NAME="localhost").status_code == 403


# ── 3. door scan -> account -> queue ─────────────────────────────────────────
def test_a_walk_in_scanned_at_the_door_reaches_the_budtender_queue(client, dutchie, monkeypatch):
    """The whole point of the door role, in one walk."""
    monkeypatch.setattr(V, "_run_scan", lambda request: dict(SCAN_OK))
    sign_in(client, "door")

    # The scan alone does not queue anyone — the door person confirms first.
    preview = client.post(reverse("door_scan"), {"id_payload": "barcode"},
                          SERVER_NAME="localhost").content.decode()
    assert "Create and queue customer" in preview
    assert ShopVisit.objects.count() == 0

    queued = client.post(reverse("create_customer"),
                         {"queue": "1", "phone": "5095550100"},
                         SERVER_NAME="localhost").content.decode()
    assert "Added to queue" in queued
    visit = ShopVisit.objects.get()
    assert visit.status == "queued" and visit.acct_id == 710000099
    assert visit.how_started == "created" and visit.store == "yakima"
    # The door person never becomes the customer's budtender.
    assert "acct_id" not in client.session

    # ── now the budtender, on their own tablet ──
    bud = _second_tablet(dutchie)
    panel = bud.get(reverse("queue_panel"), SERVER_NAME="localhost").content.decode()
    assert "Jane Doe" in panel, "the door's customer never reached the queue"

    claimed = bud.post(reverse("claim", args=[visit.id]), SERVER_NAME="localhost")
    assert claimed.status_code == 200
    visit.refresh_from_db()
    assert visit.status == "claimed" and visit.budtender == "bud"
    assert bud.session["acct_id"] == 710000099

    menu = bud.get(reverse("shop"), SERVER_NAME="localhost")
    assert menu.status_code == 200, "the claimed customer could not be taken to the menu"


def test_two_budtenders_cannot_claim_the_same_person(client, dutchie, monkeypatch):
    monkeypatch.setattr(V, "_run_scan", lambda request: dict(SCAN_OK))
    sign_in(client, "door")
    client.post(reverse("door_scan"), {"id_payload": "barcode"}, SERVER_NAME="localhost")
    client.post(reverse("create_customer"), {"queue": "1", "phone": "5095550100"},
                SERVER_NAME="localhost")
    visit = ShopVisit.objects.get()

    first, second = _second_tablet(dutchie, "bud1"), _second_tablet(dutchie, "bud2")
    assert first.post(reverse("claim", args=[visit.id]), SERVER_NAME="localhost").status_code == 200
    losing = second.post(reverse("claim", args=[visit.id]), SERVER_NAME="localhost")
    assert b"already taken" in losing.content
    assert "acct_id" not in second.session


def test_an_under_21_is_never_queued(client, dutchie, monkeypatch):
    monkeypatch.setattr(V, "_run_scan", lambda request: dict(SCAN_MINOR))
    sign_in(client, "door")
    r = client.post(reverse("door_scan"), {"id_payload": "barcode"}, SERVER_NAME="localhost")
    assert b"UNDER 21" in r.content and b"DO NOT ADMIT" in r.content
    assert ShopVisit.objects.count() == 0, "a minor was put on the queue"


# ── 4. the pin still wins over the picker ────────────────────────────────────
def test_a_pinned_door_employee_cannot_choose_the_station(client, dutchie):
    Group.objects.get(name=DOOR_ONLY_GROUP).user_set.add(User.objects.create_user("ann"))
    assert sign_in(client, "budtender")["Location"] == reverse("door")
    assert client.session["role"] == "door"


def _second_tablet(dutchie, username="bud"):
    """A separate signed-in budtender client, as a second device would be."""
    from django.test import Client
    c = Client()
    c.post(reverse("login"), {"username": username, "password": "pw", "location": "yakima",
                              "role": "budtender", "register": "8239"}, SERVER_NAME="localhost")
    return c
