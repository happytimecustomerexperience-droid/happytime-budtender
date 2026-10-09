"""Wipe one customer's memory (customer-memory-v1 "forget"), the same as POST /customer/memory/clear.

    uv run python manage.py clear_memory --phone +15095551234 --yes
    uv run python manage.py clear_memory --id 42 --yes

Clears CustomerProfile.memory (style, notes, likes/dislikes, context, topics, derived — derived comes
back from purchases on the next recompute) and ChatSession.learned of every session linked to them.
Without --yes it only says what it would clear. Audited in AdminAudit ("memory.clear").
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from budtender import identity
from budtender.models import AdminAudit, ChatSession, CustomerProfile
from budtender.tasks import _normalize_phone


class Command(BaseCommand):
    help = "Wipe one customer's stored memory (and what their linked chat sessions learned)."

    def add_arguments(self, parser):
        parser.add_argument("--phone", default="")
        parser.add_argument("--id", type=int, default=None)
        parser.add_argument("--yes", action="store_true", help="actually clear (otherwise a dry run)")

    def handle(self, *args, **opts):
        if opts["id"] is not None:
            row = CustomerProfile.objects.filter(pk=opts["id"]).first()
        else:
            phone = _normalize_phone(opts["phone"] or "")
            if not phone:
                raise CommandError("give --id or a US --phone")
            row = CustomerProfile.objects.filter(phone=phone).first()
        profile = identity.follow(row)
        if profile is None:
            raise CommandError("no such customer")
        sessions = ChatSession.objects.filter(customer=profile).exclude(learned={})
        keys = sorted((profile.memory or {}).keys())
        if not opts["yes"]:
            self.stdout.write(f"would clear customer {profile.pk}: memory keys {keys}, {sessions.count()} session(s)")
            return
        CustomerProfile.objects.filter(pk=profile.pk).update(memory={}, memory_updated_at=timezone.now())
        n = sessions.update(learned={})
        AdminAudit.objects.create(actor="manage.py clear_memory", action="memory.clear",
                                  target=f"customer:{profile.pk}", before={"had_memory": bool(keys)},
                                  after={"sessions_cleared": n})
        self.stdout.write(self.style.SUCCESS(f"cleared customer {profile.pk}: memory + {n} session(s)"))
