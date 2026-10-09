"""Customer page: every conversation (website chats + phone calls), AI summaries, clear memory.

Staff only; nothing here is ever shown or said to the customer.

Linking the imported ``crm.CustomerProfile`` to budtender's live customer (``resolve_link``):
  * a stored ``phone`` / ``manual`` link is used as it is. (Nothing in this repo can CREATE a
    ``phone`` link: the imported row keeps only a peppered hash, and budtender cannot resolve a
    hash made with this service's pepper.)
  * otherwise a ``name_unique`` link, only when the full name (two or more words), compared
    case- and whitespace-insensitively, matches EXACTLY ONE imported row here AND exactly one live
    customer in budtender (``customer/name-match``). It is re-checked on every load and dropped the
    moment either side stops being unique. A substring, a first name, or "the most recent buyer"
    never links.
  * anything else stays unlinked and the page says so.
A customer that exists only in budtender (no imported row) is keyed by its budtender id, which is the
page's pk.

Conversations: chat sessions come from ``chat/history {customer_id}`` (voice-channel sessions are
left out: they are the calls), calls from ``customer/call-ids`` joined to ``voice.VoiceCall``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from crm.models import ConversationSummary, CustomerProfile, CustomerSummary
from voice import budtender_client

from . import conversation_summaries as summaries
from .views import _toast

logger = logging.getLogger(__name__)

UNLINKED = "Not linked to a live customer record — conversations unavailable."
_EPOCH = datetime.fromtimestamp(0, UTC)


# ── linking ───────────────────────────────────────────────────────────────────
def _norm(name) -> str:
    return " ".join(str(name or "").split()).casefold()


@dataclass
class Link:
    bt_id: int | None
    kind: str = ""  # "" | phone | name_unique | manual | live (budtender-only customer)
    reason: str = ""  # why it is unlinked (staff-facing, never a name or number)


def _same_name_rows(name: str) -> int:
    words = str(name).split()
    rows = CustomerProfile.objects.filter(name__icontains=words[0]).filter(name__icontains=words[-1])
    # No cap: a capped scan could miss the second row and call an ambiguous name unique.
    return sum(1 for n in rows.values_list("name", flat=True) if _norm(n) == _norm(name))


def _store_link(local: CustomerProfile, bt_id: int | None, kind: str) -> None:
    if (local.budtender_customer_id, local.budtender_link) != (bt_id, kind):
        CustomerProfile.objects.filter(pk=local.pk).update(budtender_customer_id=bt_id, budtender_link=kind)
        local.budtender_customer_id, local.budtender_link = bt_id, kind


def resolve_link(pk: int, local: CustomerProfile | None) -> Link:
    """Which budtender customer this page is, or why we cannot tell. See the module docstring."""
    if local is None:
        return Link(pk, "live")
    if local.budtender_customer_id and local.budtender_link in ("phone", "manual"):
        return Link(local.budtender_customer_id, local.budtender_link)
    stored = local.budtender_customer_id if local.budtender_link == "name_unique" else None
    name = " ".join(str(local.name or "").split())
    if len(name.split()) < 2:
        _store_link(local, None, "")
        return Link(None, reason="no full first and last name to match on")
    if _same_name_rows(name) != 1:
        _store_link(local, None, "")
        return Link(None, reason="more than one imported customer has this exact name")
    match = budtender_client.budtender().customer_name_match(name)
    if match is None:  # budtender did not answer: unknown, so neither link nor unlink
        if stored:
            return Link(stored, "name_unique")
        return Link(None, reason="budtender did not answer")
    if match["count"] == 1 and match["id"]:
        _store_link(local, int(match["id"]), "name_unique")
        return Link(int(match["id"]), "name_unique")
    _store_link(local, None, "")
    return Link(None, reason=(
        "more than one live customer has this exact name" if match["count"] > 1
        else "no live customer has this exact name"))


# ── the conversation list ─────────────────────────────────────────────────────
@dataclass
class Conv:
    kind: str  # chat | call
    ref: str  # chat: budtender session id; call: Vapi call id
    when: datetime
    store: str
    size: str  # "12 messages" / "3:07"
    outcome: str
    href: str
    message_count: int
    load_text: Callable[[], str] = field(repr=False)
    summary: ConversationSummary | None = None
    stale: bool = False

    @property
    def dom_id(self) -> str:
        return f"{self.kind}-{self.ref}"

    @property
    def kind_label(self) -> str:
        return "Call" if self.kind == "call" else "Chat"


@dataclass
class Listing:
    items: list[Conv]
    chats_ok: bool = True
    calls_ok: bool = True
    chats_truncated: bool = False  # budtender holds more website chats than the page asked for
    missing_calls: int = 0  # call ids budtender knows that have no stored call record here


def _when(value) -> datetime:
    dt = value if isinstance(value, datetime) else parse_datetime(str(value or ""))
    if dt is None:
        return _EPOCH
    return timezone.make_aware(dt, UTC) if timezone.is_naive(dt) else dt


def _chat_text(bt_id: int, ref: str) -> str:
    session = budtender_client.budtender().customer_chat_session(bt_id, int(ref))
    if not session:
        raise summaries.SummaryError("Could not load this chat from budtender.")
    lines = [
        f"{'Customer' if m.get('role') == 'user' else 'Assistant'}: {m.get('content') or ''}"
        for m in session.get("messages") or []
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return "\n".join(lines)


def _call_text(call) -> str:
    if (call.transcript or "").strip():
        return call.transcript
    return "\n".join(f"{t.role}: {t.text}" for t in call.turns.order_by("seq") if t.text)


def _call_count(call, n_turns: int) -> int:
    lines = [ln for ln in (call.transcript or "").splitlines() if ln.strip()]
    return len(lines) or n_turns


def build_listing(bt_id: int) -> Listing:
    from voice.models import VoiceCall

    client = budtender_client.budtender()
    items: list[Conv] = []

    chats = client.customer_chat_sessions(bt_id)
    for s in chats["sessions"]:
        if not isinstance(s, dict) or s.get("channel") == "voice" or not s.get("message_count") or not s.get("id"):
            continue
        n, sid = int(s["message_count"]), str(s["id"])
        items.append(Conv(
            kind="chat", ref=sid, when=_when(s.get("last_active_at")), store=s.get("location_slug") or "",
            size=f"{n} message{'s' if n != 1 else ''}", outcome=s.get("primary_intent") or "",
            href=f"{reverse('dash-chat-detail')}?id={sid}", message_count=n,
            load_text=lambda sid=sid: _chat_text(bt_id, sid),
        ))

    call_ids = client.customer_call_ids(bt_id)
    found = 0
    if call_ids["call_ids"]:
        calls = VoiceCall.objects.filter(call_id__in=call_ids["call_ids"]).annotate(n_turns=Count("turns"))
        for c in calls:
            found += 1
            dur = f"{c.duration_s // 60}:{c.duration_s % 60:02d}" if c.duration_s is not None else "—"
            items.append(Conv(
                kind="call", ref=c.call_id, when=c.created_at, store=c.store, size=dur,
                outcome=c.get_outcome_display() if c.outcome else "",
                href=reverse("dash-call-detail", kwargs={"pk": c.pk}),
                message_count=_call_count(c, c.n_turns), load_text=lambda c=c: _call_text(c),
            ))
    items.sort(key=lambda i: i.when, reverse=True)
    return Listing(
        items=items, chats_ok=chats["ok"], calls_ok=call_ids["ok"],
        chats_truncated=chats["total"] > len(chats["sessions"]),
        missing_calls=len(set(call_ids["call_ids"])) - found,
    )


def _attach_summaries(items: list[Conv]) -> None:
    rows = {
        (s.kind, s.ref): s
        for s in ConversationSummary.objects.filter(ref__in=[i.ref for i in items])
    }
    for i in items:
        i.summary = rows.get((i.kind, i.ref))
        i.stale = bool(i.summary and i.summary.message_count != i.message_count)


def build_panel(pk: int, local: CustomerProfile | None, link: Link | None = None,
                listing: Listing | None = None) -> dict:
    """Everything ``_customer_conversations.html`` renders."""
    link = link or resolve_link(pk, local)
    if link.bt_id is None:
        return {"pk": pk, "linked": False, "message": UNLINKED, "reason": link.reason}
    listing = listing or build_listing(link.bt_id)
    _attach_summaries(listing.items)
    overall = CustomerSummary.objects.filter(budtender_customer_id=link.bt_id).first()
    return {
        "pk": pk, "linked": True, "link_kind": link.kind, "listing": listing, "items": listing.items,
        "overall": overall,
        "overall_new": bool(overall and overall.covers_count != min(len(listing.items), summaries.MAX_CONVERSATIONS)),
        "max_conversations": summaries.MAX_CONVERSATIONS,
    }


# ── views (staff only; writes are POST + CSRF) ────────────────────────────────
def _no_swap(level: str, message: str) -> HttpResponse:
    """htmx leaves the page alone on a 204; the toast tells the story."""
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = _toast(level, message)
    return resp


def _page(pk: int):
    local = CustomerProfile.objects.filter(pk=pk).first()
    return local, resolve_link(pk, local)


@staff_member_required
@require_POST
def conversation_summary(request, pk: int, kind: str, ref: str):
    """Summarize (or regenerate) ONE conversation of this customer; swaps just that row."""
    if kind not in ("chat", "call"):
        raise Http404("unknown conversation kind")
    local, link = _page(pk)
    if link.bt_id is None:
        return _no_swap("error", UNLINKED)
    listing = build_listing(link.bt_id)
    item = next((i for i in listing.items if i.kind == kind and i.ref == ref), None)
    if item is None:  # only conversations that belong to THIS customer can be summarized
        return _no_swap("error", "That conversation is not in this customer's list.")
    try:
        _, generated = summaries.conversation_summary(
            kind, ref, item.message_count, item.load_text, force=request.POST.get("regenerate") == "1")
    except summaries.SummaryError as exc:
        return _no_swap("error", str(exc))
    _attach_summaries([item])
    resp = render(request, "dashboard/_conversation_row.html", {"item": item, "pk": pk})
    resp["HX-Trigger"] = _toast("success" if generated else "info",
                                "Summary generated." if generated else "Summary is already up to date.")
    return resp


@staff_member_required
@require_POST
def summarize_all(request, pk: int):
    """Condense all of this customer's conversations (latest 30) into one paragraph; swaps the panel."""
    local, link = _page(pk)
    if link.bt_id is None:
        return _no_swap("error", UNLINKED)
    listing = build_listing(link.bt_id)
    if not listing.items:
        return _no_swap("info", "No conversations to summarize.")
    try:
        _, covered, failed = summaries.summarize_all(link.bt_id, listing.items)
    except summaries.SummaryError as exc:
        return _no_swap("error", str(exc))
    resp = render(request, "dashboard/_customer_conversations.html",
                  {"conv": build_panel(pk, local, link, listing)})
    note = f"Summarized {covered} conversation{'s' if covered != 1 else ''}."
    if failed:
        note += f" {failed} could not be summarized and were left out."
    resp["HX-Trigger"] = _toast("success" if not failed else "info", note)
    return resp


@staff_member_required
@require_POST
def memory_clear(request, pk: int):
    """Wipe this customer's stored memory in budtender (what the bots remember about them)."""
    _, link = _page(pk)
    if link.bt_id is None:
        return _no_swap("error", UNLINKED + " Memory was NOT cleared.")
    res = budtender_client.budtender().memory_clear(link.bt_id, request.user.get_username())
    status = res["status"]
    if status == "cleared":
        n = res["sessions_cleared"]
        return _no_swap("success", "Memory cleared for this customer."
                        + (f" Also cleared what {n} linked chat/call session{'s' if n != 1 else ''} had learned." if n else ""))
    if status == "not_found":
        return _no_swap("error", "Budtender has no such customer — nothing was cleared.")
    if status == "error":
        return _no_swap("error", "Budtender refused the request — memory was NOT cleared.")
    return _no_swap("error", "Budtender is unreachable — memory was NOT cleared.")
