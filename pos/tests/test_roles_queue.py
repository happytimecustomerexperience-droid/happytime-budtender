"""Roles (budtender/door/admin) + the live per-store queue + shift audit.

Dutchie is never hit: _client / load_stores / _all_registers are monkeypatched.
"""
import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from pos import views as V
from customers.models import ShopVisit, StaffSession
from dutchie.session import Store

pytestmark = pytest.mark.django_db

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=1, lsp_id=1, loc_id=1, register_id=700318, username="u", password="p", api_key="k")


@pytest.fixture
def user(db):
    return User.objects.create_user("bud", password="pw12345!")


@pytest.fixture
def auth(client, user):
    client.force_login(user)
    return client


def _use_store(monkeypatch):
    monkeypatch.setattr(V, "load_stores", lambda: {STORE.name: STORE})


def _sess(client, **kv):
    s = client.session
    for k, v in kv.items():
        s[k] = v
    s.save()


class FakeClient:
    def guest_search(self, q=""):
        return {"Data": [{"Guest_id": 555, "Name": "Jane Q", "PhoneNo": "5095551234"}]}

    def create_guest(self, **kw):
        return 777


# ── role gating ────────────────────────────────────────────────────────────
def test_door_blocked_from_cart_add(auth):
    _sess(auth, role="door", store="yakima")
    r = auth.post(reverse("cart_add"), {"ProductId": "1"}, SERVER_NAME="localhost")
    assert r.status_code == 403


def test_door_blocked_from_submit(auth):
    _sess(auth, role="door", store="yakima", acct_id=5)
    r = auth.post(reverse("cart_submit"), {}, SERVER_NAME="localhost")
    assert r.status_code == 403


def test_door_blocked_from_claim(auth):
    _sess(auth, role="door", store="yakima")
    v = ShopVisit.objects.create(store="yakima", status="queued")
    r = auth.post(reverse("claim", args=[v.id]), SERVER_NAME="localhost")
    assert r.status_code == 403


def test_budtender_not_blocked_from_cart(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V.catalog, "get_inventory", lambda store: [])
    _sess(auth, role="budtender", store="yakima")
    r = auth.post(reverse("cart_add"), {"ProductId": "nope"}, SERVER_NAME="localhost")
    assert r.status_code == 200      # renders "item unavailable", NOT a 403


# ── door -> queue ──────────────────────────────────────────────────────────
def test_door_scan_enqueues(auth):
    _sess(auth, role="door", store="yakima")
    r = auth.post(reverse("door_scan"), {"phone": "5095551234"}, SERVER_NAME="localhost")
    assert r.status_code == 200
    q = ShopVisit.objects.filter(status="queued", store="yakima")
    assert q.count() == 1
    v = q.first()
    assert v.budtender == "" and v.phone == "5095551234" and v.how_started == "door"


def test_queue_panel_lists_only_queued(auth, monkeypatch):
    _use_store(monkeypatch)
    _sess(auth, role="budtender", store="yakima")
    ShopVisit.objects.create(store="yakima", status="queued", acct_name="Waiting")
    ShopVisit.objects.create(store="yakima", status="claimed", acct_name="Shopping")
    r = auth.get(reverse("queue_panel"), SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Waiting" in r.content and b"Shopping" not in r.content


# ── claim ──────────────────────────────────────────────────────────────────
def test_claim_marks_claimed_resolves_guest_and_records_wait(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_client", lambda store: FakeClient())
    _sess(auth, role="budtender", store="yakima")
    v = ShopVisit.objects.create(store="yakima", status="queued", how_started="door",
                                 acct_name="Jane Q", phone="5095551234")
    ShopVisit.objects.filter(pk=v.pk).update(started_at=timezone.now() - timezone.timedelta(seconds=90))
    r = auth.post(reverse("claim", args=[v.id]), SERVER_NAME="localhost")
    assert r.status_code == 200
    v.refresh_from_db()
    assert v.status == "claimed" and v.claimed_at is not None and v.budtender == "bud"
    assert v.acct_id == 555                       # resolved via guest_search
    assert v.wait_seconds >= 85                   # ~90s wait captured
    assert auth.session.get("acct_id") == 555 and auth.session.get("visit_id") == v.id


def test_claim_conflict_when_already_taken(auth, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_client", lambda store: FakeClient())
    _sess(auth, role="budtender", store="yakima")
    v = ShopVisit.objects.create(store="yakima", status="claimed", acct_name="X")
    r = auth.post(reverse("claim", args=[v.id]), SERVER_NAME="localhost")
    assert r.status_code == 200 and b"already taken" in r.content


# ── durations ──────────────────────────────────────────────────────────────
def test_wait_and_service_durations():
    now = timezone.now()
    v = ShopVisit.objects.create(store="yakima", status="claimed")
    ShopVisit.objects.filter(pk=v.pk).update(
        started_at=now - timezone.timedelta(seconds=180),
        claimed_at=now - timezone.timedelta(seconds=120),
        ended_at=now, outcome="checked_out")
    v.refresh_from_db()
    assert 55 <= v.wait_seconds <= 65        # 180 - 120 = 60
    assert 115 <= v.service_seconds <= 125   # 120 - 0


# ── shift rollup ───────────────────────────────────────────────────────────
def test_staffsession_rollup(user):
    ss = StaffSession.objects.create(user=user, username="bud", role="budtender", store="yakima")
    ShopVisit.objects.create(store="yakima", staff_session=ss, outcome="checked_out", cart_total=40)
    ShopVisit.objects.create(store="yakima", staff_session=ss, outcome="checked_out", cart_total=60)
    ShopVisit.objects.create(store="yakima", staff_session=ss, outcome="abandoned", cart_total=0)
    assert ss.visit_count == 3 and ss.checkout_count == 2 and float(ss.revenue) == 100.0


# ── login: role/store/register + shift; superuser -> admin ──────────────────
def _dutchie_ok(monkeypatch, user_id=4242):
    """Dutchie says yes. Sign-in is gated on Dutchie now, not a Django password."""
    monkeypatch.setattr(V.dutchie_auth, "verify",
                        lambda store, u, p: {"user_id": user_id, "session_gid": "S", "cookie_header": "c=1"})


def test_login_sets_shift_and_role(client, user, monkeypatch):
    _use_store(monkeypatch)
    _dutchie_ok(monkeypatch)
    monkeypatch.setattr(V, "_all_registers",
                        lambda: [{"store": "yakima", "id": 8318, "name": "Register Test", "room": ""}])
    r = client.post(reverse("login"),
                    {"username": "bud", "password": "pw12345!",
                     "role": "door", "location": "yakima", "register": "8318"},
                    SERVER_NAME="localhost")
    assert r.status_code == 302 and r.url == reverse("door")
    assert client.session["role"] == "door" and client.session["store"] == "yakima"
    assert client.session["register_id"] == "8318"
    ss = StaffSession.objects.get()
    assert ss.role == "door" and ss.register_id == "8318" and ss.register_name == "Register Test"


def test_superuser_forced_to_admin(client, monkeypatch):
    _use_store(monkeypatch)
    monkeypatch.setattr(V, "_all_registers", lambda: [])
    _dutchie_ok(monkeypatch)
    User.objects.create_superuser("boss", password="pw12345!", email="")
    r = client.post(reverse("login"),
                    {"username": "boss", "password": "pw12345!",
                     "role": "door", "location": "yakima", "register": ""},
                    SERVER_NAME="localhost")
    assert r.status_code == 302 and r.url == reverse("screen")   # admin lands on the hub, not door
    assert client.session["role"] == "admin"


# ── register override reaches the Store the client is built from ────────────
def test_register_override(monkeypatch):
    _use_store(monkeypatch)
    rf = RequestFactory().get("/")
    rf.session = {"store": "yakima", "register_id": "9999"}
    store = V._active_store(rf)
    assert store.register_id == 9999          # session register overrode the store default (700318)


# ── door role blocked from the admin shifts page ────────────────────────────
def test_shifts_requires_superuser(auth):
    _sess(auth, role="budtender", store="yakima")
    r = auth.get(reverse("shifts"), SERVER_NAME="localhost")
    assert r.status_code == 403               # non-superuser


# ── templates render (catch template errors the 403 paths miss) ─────────────
def test_door_page_renders(auth, monkeypatch):
    _use_store(monkeypatch)
    _sess(auth, role="door", store="yakima")
    r = auth.get(reverse("door"), SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Check in a customer" in r.content


def test_shifts_pages_render_for_superuser(client, monkeypatch):
    _use_store(monkeypatch)
    boss = User.objects.create_superuser("boss", password="pw12345!", email="")
    client.force_login(boss)
    _sess(client, role="admin", store="yakima")
    ss = StaffSession.objects.create(user=boss, username="boss", role="admin", store="yakima",
                                     register_id="8318", register_name="Register Test")
    ShopVisit.objects.create(store="yakima", staff_session=ss, outcome="checked_out", cart_total=42)
    r = client.get(reverse("shifts"), SERVER_NAME="localhost")
    assert r.status_code == 200 and b"boss" in r.content
    r2 = client.get(reverse("shift_detail", args=[ss.id]), SERVER_NAME="localhost")
    assert r2.status_code == 200 and b"Register Test" in r2.content


def test_screen_renders_queue_for_budtender(auth, monkeypatch):
    _use_store(monkeypatch)
    _sess(auth, role="budtender", store="yakima")
    ShopVisit.objects.create(store="yakima", status="queued", acct_name="InLine")
    r = auth.get(reverse("screen"), SERVER_NAME="localhost")
    assert r.status_code == 200 and b"Queue" in r.content and b"InLine" in r.content


class TestDutchieOnlySignIn:
    """Dutchie is the gate. A Django password must never be a way in."""

    def test_a_rejected_credential_is_refused(self, client, user, monkeypatch):
        _use_store(monkeypatch)
        monkeypatch.setattr(V, "_all_registers", lambda: [])

        def reject(store, u, p):
            raise V.dutchie_auth.LoginRejected("nope")
        monkeypatch.setattr(V.dutchie_auth, "verify", reject)
        r = client.post(reverse("login"),
                        {"username": "bud", "password": "pw12345!", "role": "budtender",
                         "location": "yakima", "register": ""}, SERVER_NAME="localhost")
        assert r.status_code == 401
        assert "_auth_user_id" not in client.session

    def test_a_correct_django_password_alone_does_not_get_in(self, client, user, monkeypatch):
        # THE POINT. `user` has a real, valid Django password. Dutchie says no, so
        # the answer is no — otherwise the old local password is still a live door.
        _use_store(monkeypatch)
        monkeypatch.setattr(V, "_all_registers", lambda: [])

        def reject(store, u, p):
            raise V.dutchie_auth.LoginRejected("nope")
        monkeypatch.setattr(V.dutchie_auth, "verify", reject)
        r = client.post(reverse("login"),
                        {"username": "bud", "password": "pw12345!", "role": "budtender",
                         "location": "yakima", "register": ""}, SERVER_NAME="localhost")
        assert r.status_code == 401
        assert "_auth_user_id" not in client.session

    def test_dutchie_unreachable_fails_closed(self, client, user, monkeypatch):
        # A network blip must not become an authorisation decision, and must NOT
        # silently fall back to the local password.
        _use_store(monkeypatch)
        monkeypatch.setattr(V, "_all_registers", lambda: [])

        def down(store, u, p):
            raise V.dutchie_auth.LoginUnavailable("dutchie down")
        monkeypatch.setattr(V.dutchie_auth, "verify", down)
        r = client.post(reverse("login"),
                        {"username": "bud", "password": "pw12345!", "role": "budtender",
                         "location": "yakima", "register": ""}, SERVER_NAME="localhost")
        assert r.status_code == 503
        assert "_auth_user_id" not in client.session

    def test_the_dutchie_user_id_lands_on_the_session(self, client, user, monkeypatch):
        _use_store(monkeypatch)
        monkeypatch.setattr(V, "_all_registers", lambda: [])
        _dutchie_ok(monkeypatch, user_id=99001)
        client.post(reverse("login"),
                    {"username": "bud", "password": "x", "role": "budtender",
                     "location": "yakima", "register": ""}, SERVER_NAME="localhost")
        assert client.session["dutchie_user_id"] == 99001

    def test_the_local_row_never_has_a_usable_password(self, client, monkeypatch):
        _use_store(monkeypatch)
        monkeypatch.setattr(V, "_all_registers", lambda: [])
        _dutchie_ok(monkeypatch)
        client.post(reverse("login"),
                    {"username": "NewPerson", "password": "x", "role": "budtender",
                     "location": "yakima", "register": ""}, SERVER_NAME="localhost")
        u = User.objects.get(username="newperson")     # casefolded, so one row per person
        assert not u.has_usable_password()
