"""Throwaway: revert the two fixes AT RUNTIME and confirm the guards fail."""
import re
from unittest.mock import patch
from django.test import override_settings
from bundles.tests.test_security import (InputAbuseTests, SECRET, CACHES_LOCMEM)

NEVER = re.compile(r"(?!x)x")   # matches nothing == the old, unstripped behaviour
OLD_PHONE = re.compile(r"\D+")  # the old Unicode-aware strip


@override_settings(BUNDLE_URL_SECRET=SECRET, CACHES=CACHES_LOCMEM, BUNDLE_MIN_STOCK=2,
                   BUNDLE_MAX_ORDER_TOTAL=300)
class MutationCheck(InputAbuseTests):
    def test_nul_guard_bites(self):
        with patch("bundles.views._CONTROL_RE", NEVER):
            try:
                super().test_a_null_byte_in_the_name_never_reaches_the_insert()
            except AssertionError as exc:
                print("NUL GUARD BITES:", str(exc)[:160]); return
            except Exception as exc:
                print("NUL GUARD BITES (raised):", type(exc).__name__, str(exc)[:120]); return
        raise AssertionError("NUL guard did NOT bite — the test is vacuous")

    def test_phone_guard_bites(self):
        with patch("bundles.views._PHONE_RE", OLD_PHONE):
            try:
                super().test_a_phone_must_be_ten_ascii_digits()
            except AssertionError as exc:
                print("PHONE GUARD BITES:", str(exc)[:200]); return
        raise AssertionError("phone guard did NOT bite — the test is vacuous")

    def test_control_guard_bites(self):
        with patch("bundles.views._CONTROL_RE", NEVER):
            try:
                super().test_every_c0_control_character_is_stripped_from_the_contact_fields()
            except (AssertionError, Exception) as exc:
                print("C0 GUARD BITES:", type(exc).__name__, str(exc)[:120]); return
        raise AssertionError("C0 guard did NOT bite")
