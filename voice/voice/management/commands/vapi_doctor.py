"""``python manage.py vapi_doctor [--json] [--fix-hints]``: read-only check of the whole phone line.

GETs only (Vapi + our own health URLs); never POST/PATCH/DELETE, never places a call, never prints a
secret. Exits non-zero when any check FAILs. See ``voice/doctor.py`` and
``docs/VAPI-SETUP-AND-TESTING.md``. On the VPS run it inside the stack:

    docker compose exec voice-web python manage.py vapi_doctor
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from voice import doctor


class Command(BaseCommand):
    help = "Read-only check of config, budtender, our webhook, and the live Vapi squad / phone number."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print a stable JSON report instead of text.")
        parser.add_argument("--fix-hints", action="store_true", help="Also print why each problem matters (and the Vapi doc).")
        parser.add_argument(
            "--expect-squad",
            default=doctor.KNOWN_LIVE_SQUAD_ID,
            help="The squad id the line should run (default: the known live squad).",
        )

    def handle(self, *args, **opts):
        checks = doctor.Doctor(expect_squad=opts["expect_squad"]).run()
        if opts["json"]:
            self.stdout.write(doctor.to_json(checks))
        else:
            self.stdout.write(doctor.to_text(checks, hints=opts["fix_hints"]))
        failed = doctor.summary(checks)["fail"]
        if failed:
            raise CommandError(f"{failed} check(s) FAILED; see the fix lines above.")
