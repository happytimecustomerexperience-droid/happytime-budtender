"""A 500 in production must reach `docker compose logs`, not vanish.

This is the one thing standing between a broken checkout and silence. gunicorn runs
with no `--access-logfile` and Traefik with no `--accesslog`, so Django's own logging
is the only record that a request failed — there is no second copy anywhere, and no
Sentry.

Before `core.settings.LOGGING` existed, there was none at all. Django's DEFAULT_LOGGING
gives the `django` logger a console handler behind `require_debug_true` (dead when
DEBUG=0) and `mail_admins`, which returns immediately when `ADMINS` is empty — and it
is. Python's `lastResort` handler does not cover for it either: that only fires when a
record finds NO handler anywhere in its chain, and `django.request` propagates to
`django`, which HAS handlers, so the record counts as handled and disappears.

Measured on the VPS at the time: `web` returned two 400s and logged nothing, while
`voice-web` — the one project that already configured logging — printed `Not Found: /`
for the same class of event.

These tests assert the BEHAVIOUR (a record is emitted, on the right logger, at the right
level) rather than the shape of the settings dict, because a dict that looks right and
emits nothing is exactly the failure that shipped.
"""
import logging

from django.conf import settings
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path

log = logging.getLogger(__name__)


def boom(request):
    raise RuntimeError("canary-500")


def fine(request):
    return HttpResponse("ok")


urlpatterns = [
    path("boom", boom),
    path("fine", fine),
]


@override_settings(ROOT_URLCONF=__name__, DEBUG=False)
class UnhandledExceptionIsLoggedTests(SimpleTestCase):
    def test_a_500_emits_an_error_record_with_its_traceback(self):
        # raise_request_exception=False makes the client behave like a real server:
        # Django catches the exception, logs it, and returns 500 — rather than
        # re-raising into the test and never exercising the logging path at all.
        with self.assertLogs("django.request", level="ERROR") as caught:
            r = self.client.get("/boom", raise_request_exception=False)

        self.assertEqual(r.status_code, 500)
        self.assertEqual(len(caught.records), 1)
        record = caught.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        # The traceback is the whole point — an ERROR line saying "Internal Server
        # Error: /boom" with no exc_info tells you nothing you did not already know
        # from the status code.
        self.assertIsNotNone(record.exc_info, "the traceback was not attached")
        self.assertIn("canary-500", "".join(caught.output))

    def test_the_record_actually_reaches_a_stream_handler(self):
        """assertLogs would pass even with zero handlers — it attaches its own.

        So assert separately that `django.request` resolves to a real StreamHandler.
        That is what puts the line in `docker compose logs`; without it the record is
        created and then dropped, which is precisely the bug this file exists for.
        """
        logger = logging.getLogger("django.request")
        handlers, node = [], logger
        while node:
            handlers.extend(node.handlers)
            node = node.parent if node.propagate else None
        streams = [h for h in handlers if isinstance(h, logging.StreamHandler)]
        self.assertTrue(streams, "django.request reaches no StreamHandler — a 500 is invisible")
        for h in streams:
            # A require_debug_true filter is what silently disabled the default
            # console handler in production. Any filter here deserves the same
            # suspicion, so name it in the failure.
            names = [type(f).__name__ for f in h.filters]
            self.assertNotIn("RequireDebugTrue", names,
                             f"handler {h} is filtered out whenever DEBUG=0")

    def test_a_healthy_request_stays_quiet(self):
        # Logging every 200 would bury the 500s. `django.request` at ERROR is what
        # keeps 404 and success noise out of the stream.
        with self.assertNoLogs("django.request", level="INFO"):
            self.assertEqual(self.client.get("/fine").status_code, 200)


class LoggingIsConfiguredAtAllTests(SimpleTestCase):
    def test_the_project_ships_a_logging_config(self):
        # An empty LOGGING is not a neutral default — it is the broken state.
        self.assertTrue(settings.LOGGING, "core.settings defines no LOGGING")
        self.assertIn("django.request", settings.LOGGING.get("loggers", {}))

    def test_security_events_are_not_swallowed(self):
        # DisallowedHost arrives here. It is how you find out someone is probing the
        # host header, and it was equally invisible before.
        logger = logging.getLogger("django.security")
        with self.assertLogs("django.security", level="WARNING"):
            logger.warning("canary-security")
        self.assertFalse(logger.propagate, "django.security would double-log via root")
