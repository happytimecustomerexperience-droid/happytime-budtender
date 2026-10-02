"""Keep the Specials & hours rows in step with the deals on each store's Dutchie online menu.

Budtender reads Dutchie (``GET /api/v1/deals/``); this turns each current deal into one
``StoreFact(kind="special")`` row labelled ``Dutchie #<id>`` so the phone agent, the website chat and
the dashboard all quote today's deals through the one path they already use (``faq_lookup``).

Contract:
  * Off unless the ``auto.deals_sync`` switch is on (a ``--dry-run`` only reads, so it always runs).
  * A store whose Dutchie feed was unreachable arrives as ``None`` and is skipped entirely: an
    outage never wipes that store's deals.
  * Only rows labelled ``Dutchie #...`` are ever touched. Owner-typed specials are never edited or
    deactivated. A Dutchie row whose deal left the feed (ended, disabled, now suspect) is
    deactivated, not deleted.
  * A title or description that looks like a prompt injection is skipped and logged.
  * Nothing is written, and no downstream nudge is sent, when nothing changed.
"""

from __future__ import annotations

import datetime
import logging

from django.db import transaction

from kb import signals
from kb.models import StoreFact
from voice import capabilities, guardrails
from voice.budtender_client import budtender
from voice.tools.faq import _looks_poisoned

logger = logging.getLogger(__name__)

STORES = ("yakima", "mount-vernon", "pullman")
PREFIX = "Dutchie #"


def _clock(t: str) -> tuple[str, str]:
    """'14:05' -> ('2:05', 'PM'); a whole hour drops its minutes ('14:00' -> ('2', 'PM'))."""
    h, m = int(t[:2]), int(t[3:5])
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''}", "AM" if h < 12 else "PM"


_DESC_MAX = 160  # one spoken sentence or so; descriptions can carry a paragraph of fine print


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _description(deal: dict) -> str:
    """The menu description when it says more than the title ("Chewee's Caramels $14 First Come
    First Serve!" under the title "Chewee's Special"), trimmed to about a sentence; else ""."""
    desc = " ".join(str(deal.get("description") or "").split())
    title = _norm(deal["title"])
    # A "cost"/"margin" substring would get the WHOLE specials answer redacted by the leak wall
    # (guardrails.scrub_leak), so such a description is left out rather than risk every deal.
    if not desc or _norm(desc) in title or guardrails._has_forbidden_substr(desc):
        return ""
    if len(desc) > _DESC_MAX:
        cut = desc[:_DESC_MAX]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        desc = cut[: end + 1] if end > 0 else cut[: cut.rfind(" ")].rstrip(",;:-— ") + "."
    return desc


def spoken(deal: dict, today: datetime.date) -> str:
    """The sentence a caller hears: title, then the schedule when it has one ('daily 9-10 AM'),
    then 'through Oct 31' when it ends within a year, then the menu description when it adds
    something (``_description``). Built only from the deal's own fields."""
    desc = _description(deal)
    parts = [deal["title"].strip()]
    days = "/".join(d[:3].capitalize() for d in deal.get("days") or [])
    hours = ""
    if deal.get("start_time") and deal.get("end_time"):
        (a, a_ap), (b, b_ap) = _clock(deal["start_time"]), _clock(deal["end_time"])
        hours = f"{a}-{b} {b_ap}" if a_ap == b_ap else f"{a} {a_ap}-{b} {b_ap}"
        days = days or "daily"
    if days or hours:
        parts.append(f"{days} {hours}".strip())
    if deal.get("ends"):
        end = datetime.date.fromisoformat(deal["ends"])
        if end - today < datetime.timedelta(days=365):
            parts.append(f"through {end:%b} {end.day}")
    text = ", ".join(parts)
    text = text if text[-1] in ".!?" else text + "."  # rows are read back to back
    if desc:
        text += " " + (desc if desc[-1] in ".!?" else desc + ".")
    return text


def sync_deals(dry_run: bool = False) -> dict:
    """Returns ``{store: {created, updated, deactivated}}`` (a skipped store is
    ``{"skipped": "unreachable"}``), or ``{"skipped": "capability off"}``. ``dry_run`` writes
    nothing and adds a per-store ``changes`` list of what would happen."""
    if not dry_run and not capabilities.is_enabled("auto.deals_sync"):
        return {"skipped": "capability off"}
    feed = budtender().deals()["stores"]
    today = datetime.date.today()
    counts: dict = {}
    to_save: list[StoreFact] = []
    for slug in STORES:
        deals = feed.get(slug)
        if deals is None:  # unreachable (or budtender down): unknown, so leave this store alone
            counts[slug] = {"skipped": "unreachable"}
            continue
        wanted = {}
        for d in deals:
            if _looks_poisoned(f"{d['title']} {d.get('description') or ''}"):
                logger.warning("deals_sync: skipping suspect deal %s/%s", slug, d["id"])
                continue
            wanted[f"{PREFIX}{d['id']}"] = {
                "value": spoken(d, today),
                "valid_from": datetime.date.fromisoformat(d["starts"]),
                "valid_to": datetime.date.fromisoformat(d["ends"]) if d.get("ends") else None,
                "confirmed": True,
                "is_active": True,
            }
        have = {
            r.label: r
            for r in StoreFact.objects.filter(store=slug, kind="special", label__startswith=PREFIX)
        }
        c = counts[slug] = {"created": 0, "updated": 0, "deactivated": 0}
        changes = []
        for label, fields in wanted.items():
            row = have.pop(label, None)
            if row is None:
                to_save.append(StoreFact(store=slug, kind="special", label=label, **fields))
                c["created"] += 1
                changes.append(f"create {label}: {fields['value']}")
            elif any(getattr(row, k) != v for k, v in fields.items()):
                for k, v in fields.items():
                    setattr(row, k, v)
                to_save.append(row)
                c["updated"] += 1
                changes.append(f"update {label}: {fields['value']}")
        for row in have.values():  # still labelled Dutchie, no longer in the feed
            if row.is_active:
                row.is_active = False
                to_save.append(row)
                c["deactivated"] += 1
                changes.append(f"deactivate {row.label}: {row.value}")
        if dry_run:
            c["changes"] = changes
    if not dry_run:
        if to_save:
            with signals.bulk(), transaction.atomic():
                for row in to_save:
                    row.save()
        logger.info("deals_sync: %s", counts)
    return counts
