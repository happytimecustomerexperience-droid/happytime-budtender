"""Blank the contact details and ID documents we no longer need.

Not scheduled and not a migration: the owner decides when to run it on prod.

    python manage.py purge_pii --dry-run     # counts only, writes nothing
    python manage.py purge_pii               # do it

Phone carts: the raw phone, email and names are only needed until the order is collected.
A draft that expired, or was claimed, more than --days ago (default 30) has them blanked;
its lines and quote stay for reporting.

Customer cache: a scan's raw payload, ID number, address and date of birth are only needed
until the Dutchie account exists (see customers.services.identity_document_blanks). Rows
untouched for a day that still carry them are blanked: customers resolved before that rule
existed, and scans nobody finished. A scan from the last day may be mid-checkout and is left
alone.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from budtender.models import PhoneCartDraft
from customers.models import Customer
from customers.services import identity_document_blanks

DRAFT_CONTACT_FIELDS = ("contact_phone", "contact_email", "pickup_name", "customer_name")
PENDING_SCAN_GRACE = timedelta(days=1)


def _any_not_blank(blanks: dict) -> Q:
    """Rows where at least one of these columns still holds something."""
    q = Q()
    for field, blank in blanks.items():
        q |= ~Q(**{field: blank})
    return q


class Command(BaseCommand):
    help = "Blank contact details on old phone-cart drafts and ID documents on cached customers."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30,
                            help="Drafts expired/claimed more than this many days ago (default 30).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Print the counts without changing anything.")

    def handle(self, *args, **opts):
        now = timezone.now()
        cutoff = now - timedelta(days=opts["days"])

        draft_blanks = {f: "" for f in DRAFT_CONTACT_FIELDS}
        drafts = PhoneCartDraft.objects.filter(_any_not_blank(draft_blanks)).filter(
            Q(status=PhoneCartDraft.Status.EXPIRED)
            | Q(expires_at__lt=cutoff)
            | Q(status=PhoneCartDraft.Status.CLAIMED, claimed_at__lt=cutoff)
        )

        customer_blanks = identity_document_blanks()
        customers = Customer.objects.filter(
            _any_not_blank(customer_blanks), updated_at__lt=now - PENDING_SCAN_GRACE
        )

        n_drafts, n_customers = drafts.count(), customers.count()
        if opts["dry_run"]:
            self.stdout.write(f"Would blank contact details on {n_drafts} phone-cart draft(s) "
                              f"expired or claimed more than {opts['days']}d ago.")
            self.stdout.write(f"Would blank ID documents on {n_customers} cached customer(s).")
            return
        drafts.update(**draft_blanks)
        customers.update(**customer_blanks)
        self.stdout.write(self.style.SUCCESS(
            f"Blanked contact details on {n_drafts} phone-cart draft(s) and ID documents on "
            f"{n_customers} cached customer(s)."))
