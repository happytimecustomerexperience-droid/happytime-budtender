"""Read-only: why would a visitor be greeted by name? Lists the rows that can supply a name wrongly.

    python manage.py diagnose_greeting                # shared / junk-number rows, whoever they are named
    python manage.py diagnose_greeting --name Jaime   # also every row (and linked chat) with that first name

It writes nothing and prints phone numbers as their last four digits only. A row is flagged when
``identity`` would no longer resolve it (a store's own line, a junk number, a blank phone, or one phone
folding several Dutchie customers): those are the rows that greeted strangers before the fix.
"""
from collections import Counter

from django.core.management.base import BaseCommand

from budtender import identity
from budtender.models import ChatSession, CustomerProfile


def _last4(phone: str) -> str:
    return f"...{phone[-4:]}" if phone else "(blank)"


class Command(BaseCommand):
    help = "List customer rows that could greet a visitor by name when they should not (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="", help="Also list every row whose first name is this.")

    def handle(self, *args, **opts):
        want = identity.first_name(opts["name"]).lower()
        flagged = []
        for p in CustomerProfile.objects.all().iterator():
            why = []
            if not p.phone:
                why.append("blank phone")
            elif identity.non_identifying_phone(p.phone):
                why.append("non-identifying number (store line / junk / placeholder)")
            if identity.shared_profile(p):
                why.append(f"shared: {len(p.dutchie_ids)} Dutchie customer ids on one phone")
            named = bool(want) and identity.first_name(p.name).lower() == want
            if why or named:
                flagged.append((p, why, named))
        self.stdout.write(f"{len(flagged)} row(s) flagged.")
        for p, why, named in sorted(flagged, key=lambda t: -t[0].total_orders):
            sessions = ChatSession.objects.filter(customer=p)
            by_store = Counter(sessions.values_list("location_slug", flat=True))
            self.stdout.write(
                f"  #{p.pk} {_last4(p.phone)} name={identity.first_name(p.name) or '-'!r} source={p.source} "
                f"orders={p.total_orders} dutchie_ids={len(p.dutchie_ids or [])} "
                f"chats_linked={sum(by_store.values())} by_store={dict(by_store)} "
                f"{'| ' + '; '.join(why) if why else '| named match, looks like one person'}"
                f"{' | NAME MATCH' if named and why else ''}"
            )
