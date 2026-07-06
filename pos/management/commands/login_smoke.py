"""Smoke #1 via manage.py: python manage.py login_smoke yakima"""

from django.core.management.base import BaseCommand

from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import get_store


class Command(BaseCommand):
    help = "Prove employee login + cookie (and cross-subdomain POS reachability)."

    def add_arguments(self, parser):
        parser.add_argument("store")

    def handle(self, *args, **opts):
        client = PosRegisterClient(get_store(opts["store"]))
        sess = client._session()
        self.stdout.write(f"cookie: {(sess.cookie_header or '')[:40]}...")
        self.stdout.write(f"session_gid: {sess.session_gid}")
        self.stdout.write(f"user_id: {sess.user_id}")
        if not sess.session_gid:
            self.stderr.write("FAIL: empty session_gid")
            return
        probe = client.get("/")
        self.stdout.write(self.style.SUCCESS(f"pos GET / -> {probe.get('status')}"))
