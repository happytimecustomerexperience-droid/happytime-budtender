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
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from pos.dutchie_auth import DOOR_ONLY_GROUP, role_for

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
IDENTITY = {"user_id": 95602, "session_gid": "gid", "cookie_header": "LLSession=x"}


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


class TheGroupExistsForManagersToUseTests(TestCase):
    def test_the_migration_left_it_ready_and_empty(self):
        group = Group.objects.filter(name=DOOR_ONLY_GROUP).first()
        self.assertIsNotNone(group, "managers have no group to put anyone in")
        self.assertFalse(group.user_set.exists(),
                         "the migration pinned somebody, which it must never do")
