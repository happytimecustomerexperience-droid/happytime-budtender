"""A dead Celery broker must not slow or fail a request (task.delay() blocks ~70 s)."""
from django.core.cache import cache
from django.test import SimpleTestCase

from budtender import fire as fire_mod


class FakeTask:
    name = "fake"

    def __init__(self, exc=None):
        self.exc, self.calls = exc, []

    def apply_async(self, args=None, retry=True, **kw):
        self.calls.append((args, retry))
        if self.exc:
            raise self.exc


class FireTests(SimpleTestCase):
    def setUp(self):
        cache.delete(fire_mod.BREAKER_KEY)

    def test_healthy_broker_publishes_without_retry(self):
        t = FakeTask()
        self.assertTrue(fire_mod.fire(t, "555"))
        self.assertEqual(t.calls, [(["555"], False)])

    def test_dead_broker_returns_false_and_trips_breaker(self):
        t = FakeTask(ConnectionError("down"))
        self.assertFalse(fire_mod.fire(t, "a"))
        self.assertFalse(fire_mod.fire(t, "b"))   # breaker open: not even tried
        self.assertEqual(len(t.calls), 1)

    def test_breaker_resets(self):
        t = FakeTask(ConnectionError("down"))
        fire_mod.fire(t)
        cache.delete(fire_mod.BREAKER_KEY)
        fire_mod.fire(t)
        self.assertEqual(len(t.calls), 2)
