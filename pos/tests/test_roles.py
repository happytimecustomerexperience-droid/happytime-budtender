"""The role a shift runs at must not be the browser's word.

Measured before the fix, against a real login POST: posting `role=budtender`
produced `session["role"] == "budtender"` and a redirect to /pos/. That was not an
escalation past a lock — `budtender` is the DEFAULT, so door staff reached checkout
by simply not choosing "door". The eight `_require_not_door` guards bound only the
people who volunteered to be bound.

Dutchie cannot arbitrate it: a live `EmployeeLogin` capture carries no permission,
role or access-level field at either origin (dutchie/fixtures/employee_login_*.json).
So the source of truth is the `door-only` group, and these tests pin the one
property that matters — membership beats the POST body.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from pos.dutchie_auth import DOOR_ONLY_GROUP, role_for

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
IDENTITY = {"user_id": 95602, "session_gid": "gid", "permissions": ["LogintoPOS"],
            "permissions_known": True}
STORE = SimpleNamespace(name="yakima", base_url="https://bo", pos_base_url="https://pos",
                        org_id=8002, lsp_id=1745, loc_id=3498)


def door_only():
    """The group the migration already made. Fetched, never created — creating it
    here would pass even if the migration had never shipped, which is the one thing
    these tests are supposed to notice."""
    return Group.objects.get(name=DOOR_ONLY_GROUP)


class RoleForTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ann")

    def test_an_ordinary_employee_still_picks_their_own_mode(self):
        # The picker stays a convenience for everyone not pinned — this change is
        # not allowed to make anyone's day harder.
        self.assertEqual(role_for(self.user, "budtender"), "budtender")
        self.assertEqual(role_for(self.user, "door"), "door")

    def test_a_junk_role_is_not_honoured(self):
        for junk in ("admin", "superuser", "", None, "BUDTENDER", "door "):
            self.assertEqual(role_for(self.user, junk), "budtender")

    def test_admin_is_decided_by_the_server_not_the_form(self):
        boss = User.objects.create_user("boss", is_superuser=True)
        self.assertEqual(role_for(boss, "door"), "admin")

    # ── the actual defect ────────────────────────────────────────────────────
    def test_door_staff_cannot_request_their_way_to_the_till(self):
        self.user.groups.add(door_only())
        self.assertEqual(role_for(self.user, "budtender"), "door")

    def test_the_group_can_only_take_the_till_away_never_grant_it(self):
        """One-directional on purpose: no group GRANTS selling.

        A group that granted it would mean one wrong click in Django admin hands
        somebody the register. Removal-only means the worst a mistake can do is
        stop someone selling, which a manager notices in seconds.
        """
        self.user.groups.add(Group.objects.create(name="budtender-only"))
        self.assertEqual(role_for(self.user, "door"), "door")


@override_settings(CACHES=CACHES_LOCMEM)
class LoginHonoursThePinTests(TestCase):
    """End to end through the real view, with only Dutchie itself mocked."""

    def _login(self, role):
        c = Client()
        with patch("pos.dutchie_auth.verify", return_value=IDENTITY), \
             patch("pos.views._all_registers", return_value=[]), \
             patch("pos.views._stores", return_value={"yakima": object()}):
            r = c.post(reverse("login"), {"location": "yakima", "username": "ann",
                                          "password": "pw", "role": role})
        return r, c.session.get("role")

    def test_the_pin_survives_a_forged_post(self):
        door_only().user_set.add(User.objects.create_user("ann"))
        r, role = self._login("budtender")
        self.assertEqual(role, "door")
        self.assertEqual(r["Location"], reverse("door"))

    def test_an_unpinned_employee_is_unaffected(self):
        _, role = self._login("budtender")
        self.assertEqual(role, "budtender")

    def test_the_group_is_matched_on_the_casefolded_username(self):
        """`local_user_for` casefolds, so pinning "Ann" must still catch "ann"."""
        door_only().user_set.add(User.objects.create_user("ann"))
        c = Client()
        with patch("pos.dutchie_auth.verify", return_value=IDENTITY), \
             patch("pos.views._all_registers", return_value=[]), \
             patch("pos.views._stores", return_value={"yakima": object()}):
            c.post(reverse("login"), {"location": "yakima", "username": "Ann",
                                      "password": "pw", "role": "budtender"})
        self.assertEqual(c.session.get("role"), "door")
        self.assertEqual(User.objects.filter(username__iexact="ann").count(), 1)


class DutchieCanRefusePOSAccessTests(TestCase):
    """Dutchie's own answer to "may this person use a register at all".

    Read once at sign-in on the employee's own session — the only moment it can be
    read, since Dutchie refuses to report permissions for anyone but the
    authenticated user (probed live: any other UserId returns 403).
    """

    def _verify(self, perms):
        from pos import dutchie_auth as da
        with patch.object(da, "get_store", return_value=STORE), \
             patch.object(da, "authenticate_employee",
                          return_value={"user_id": 1, "session_gid": "s",
                                        "cookie_header": "c=1"}), \
             patch.object(da, "employee_permissions", return_value=perms):
            return da.verify("yakima", "ann", "pw")

    def test_dutchie_saying_no_pos_access_stops_the_sign_in(self):
        from pos.dutchie_auth import LoginRejected
        with self.assertRaises(LoginRejected) as caught:
            self._verify({"ViewCustomers"})          # a definite set, without LogintoPOS
        # Not the wrong-password message: the password was right, and only a manager
        # can fix this. Sending them back to retype it would waste their shift.
        self.assertIn("manager", str(caught.exception))

    def test_an_account_holding_nothing_is_refused(self):
        from pos.dutchie_auth import LoginRejected
        with self.assertRaises(LoginRejected):
            self._verify(set())

    def test_no_answer_from_dutchie_does_not_lock_the_store_out(self):
        # None means "no answer", never "no permissions". A Cloudflare blip must not
        # take a store off its own tills — identity was already proved above.
        got = self._verify(None)
        self.assertEqual(got["permissions_known"], False)
        self.assertEqual(got["permissions"], [])

    def test_a_permitted_employee_gets_in_and_the_set_is_recorded(self):
        got = self._verify({"LogintoPOS", "ViewCustomers"})
        self.assertTrue(got["permissions_known"])
        self.assertEqual(got["permissions"], ["LogintoPOS", "ViewCustomers"])

    def test_the_borrowed_session_is_not_kept(self):
        # It was minted to ask one question. Holding it would mean the shift runs on
        # the employee's own Dutchie session, which is a different design decision.
        self.assertNotIn("cookie_header", self._verify({"LogintoPOS"}))


class TheGroupExistsForManagersToUseTests(TestCase):
    def test_the_migration_left_it_ready_and_empty(self):
        group = Group.objects.filter(name=DOOR_ONLY_GROUP).first()
        self.assertIsNotNone(group, "managers have no group to put anyone in")
        self.assertFalse(group.user_set.exists(),
                         "the migration pinned somebody, which it must never do")
