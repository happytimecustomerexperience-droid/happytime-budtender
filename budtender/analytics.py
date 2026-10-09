"""Chatbot analytics: which event names we store, and what they add up to.

Two readers of the same ``AnalyticsEvent`` rows:

* ``funnel`` rolls the chat events of a window up per session, then per store and per day: how many
  opened the chat, how far down the questionnaire they got, what they searched for, which picks they
  looked at and clicked, and where they bounced.
* ``timeline`` replays ONE session: its events, messages and suggestions in the order they happened.

The website ships its events under two generations of names. The contract names (docs/contracts/
search-v2.md, "Events") are canonical; the ``chat_*`` names the live site already sends are folded onto
them by ``canonical`` so the dashboard works the day this lands and keeps working after the site moves.
No phone, no raw session token and no cost/margin ever leaves this module: sessions are addressed by an
opaque ``ref``, and the owner reads text the visitor already typed into the chat.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone

from .models import AnalyticsEvent, ChatMessage, ChatSession, SuggestedProduct

# ── names ────────────────────────────────────────────────────────────────────

# The contract's event types (docs/contracts/search-v2.md).
CONTRACT_EVENTS = frozenset({
    "chat_open", "chat_close", "chat_resume", "chat_restart", "chat_message_sent", "chat_message_received",
    "chip_click", "questionnaire_step_view", "questionnaire_step_answer", "questionnaire_skip",
    "specify_more_open", "search_run", "picks_view", "picks_collapse", "show_more_click",
    "product_card_click", "product_expand", "order_ahead_click", "find_similar_open",
    "find_similar_result", "similar_pick", "pair_upsell_view", "pair_upsell_accept",
    "phone_capture_submit", "phone_capture_skip", "voice_offer_click", "bounce", "nav_away",
})

# Names the live site and the backend already emit. Kept so nothing that is recorded today is dropped.
LEGACY_CHAT_EVENTS = frozenset({
    "chat_message", "chat_search", "chat_recommend_view", "chat_product_click",
    "chat_show_me_something_else", "chat_session_end", "chat_stage_dwell", "chat_route_select",
    "chat_location_select", "chat_login", "chat_pairing_view", "chat_pairing_click",
    "chat_pair_upsell_view", "chat_pair_upsell_click", "chat_pair_upsell_dismiss", "chat_picks_refresh",
    "chat_size_ask", "chat_budget_ask", "chat_budget_hallucinated", "chat_category_info",
    "chat_new_drops_click", "chat_deals_view", "chat_voice_offer_select", "chat_voice_offer_decline",
    "similar_search", "similar_results_view", "similar_select",
    "voice_call_active", "voice_call_ended", "voice_call_error",
})

# Site-wide events (not chat): page and conversion beacons, the Dutchie menu embed, feedback.
SITE_EVENTS = frozenset({
    "page_view", "time_on_page", "scroll", "web_vital", "performance", "user_properties_set",
    "order_online_click", "phone_click", "directions_click", "email_click", "outbound_click",
    "dutchie_product_view", "dutchie_add_to_cart", "dutchie_cart_view", "dutchie_checkout",
    "dutchie_purchase", "feedback",
})

CHAT_EVENT_NAMES = CONTRACT_EVENTS | LEGACY_CHAT_EVENTS
EVENT_WHITELIST = CHAT_EVENT_NAMES | SITE_EVENTS
MAX_EVENT_NAME = 32  # AnalyticsEvent.event_type

_ALIASES = {
    "chat_search": "search_run",
    "chat_recommend_view": "picks_view",
    "chat_product_click": "product_card_click",
    "chat_show_me_something_else": "show_more_click",
    "chat_pair_upsell_view": "pair_upsell_view",
    "chat_pair_upsell_click": "pair_upsell_accept",
    "chat_voice_offer_select": "voice_offer_click",
    "similar_results_view": "find_similar_result",
    "similar_select": "similar_pick",
    "similar_search": "find_similar_open",
}

# Events that are the page talking or the visitor leaving, not the visitor doing something.
_PASSIVE = frozenset({
    "chat_open", "chat_close", "chat_resume", "chat_restart", "chat_message_received", "picks_collapse",
    "pair_upsell_view", "bounce", "nav_away", "chat_session_end", "chat_stage_dwell", "chat_pairing_view",
    "chat_size_ask", "chat_budget_ask", "chat_budget_hallucinated", "questionnaire_step_view",
    "find_similar_result", "chat_pair_upsell_dismiss", "chat_voice_offer_decline", "voice_call_active",
    "voice_call_ended", "voice_call_error",
})
# What a visitor does with picks once they are on screen.
_ENGAGES_WITH_PICKS = frozenset({
    "product_card_click", "product_expand", "order_ahead_click", "show_more_click", "find_similar_open",
    "similar_pick", "pair_upsell_accept", "chat_message_sent", "chip_click", "chat_pairing_click",
    "chat_new_drops_click",
})


def canonical(event_type: str, props: dict | None = None) -> str:
    """The contract name for a stored event type (``chat_message`` is split by who spoke)."""
    if event_type == "chat_message":
        return "chat_message_received" if (props or {}).get("role") == "assistant" else "chat_message_sent"
    return _ALIASES.get(event_type, event_type)


def is_chat_event(event_type: str) -> bool:
    return event_type in CHAT_EVENT_NAMES


# ── small helpers ────────────────────────────────────────────────────────────

_SLOT_PART = re.compile(r"^\s*([a-z_]+)\s*=\s*(.+?)\s*$")
_SLOT_KEYS = ("category", "effect", "size", "budget", "flavor", "terpene", "aroma", "sort", "thc", "ratio", "tier")


def parse_slots(value: object) -> dict[str, str]:
    """``slots_summary`` is "store=pullman · category=flower · budget=$30-50"; a dict is taken as is."""
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (str, int, float)) and str(v).strip():
                out[str(k)[:24]] = str(v).strip().strip('"')[:60]
        return out
    for part in str(value or "").split("·"):
        m = _SLOT_PART.match(part)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')[:60]
    return out


def session_ref(token: str) -> str:
    """An opaque handle for a session. The token itself is the visitor's write credential and is never shown."""
    return hashlib.sha256(f"hht-session:{token}".encode()).hexdigest()[:16]


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class _Session:
    """Everything the funnel needs to know about one chat session, built from its events in time order."""

    def __init__(self, token: str):
        self.token = token
        self.visitor = ""
        self.stores: Counter = Counter()
        self.first = self.last = None
        self.names: Counter = Counter()
        self.steps: dict[str, dict[str, bool]] = {}
        self.order: list[str] = []                                # questionnaire steps in the order first seen
        self.searches: list[tuple[dict, float | None]] = []      # (slots, result count)
        self.last_step = ""
        self.bounce_event: dict | None = None
        self.n_events = 0
        self.picks_at = None
        self.engaged_after_picks = False

    def add(self, name: str, props: dict, store: str, visitor: str, ts) -> None:
        self.n_events += 1
        self.names[name] += 1
        if visitor and not self.visitor:
            self.visitor = visitor
        store = store or str(props.get("store") or "")
        if store:
            self.stores[store] += 1
        self.first = ts if self.first is None else min(self.first, ts)
        self.last = ts if self.last is None else max(self.last, ts)
        step = str(props.get("step") or "")[:40]
        if name in ("questionnaire_step_view", "questionnaire_step_answer", "questionnaire_skip") and step:
            if step not in self.steps:
                self.steps[step] = {"viewed": False, "answered": False, "skipped": False}
                self.order.append(step)
            flag = {"questionnaire_step_view": "viewed", "questionnaire_step_answer": "answered",
                    "questionnaire_skip": "skipped"}[name]
            self.steps[step][flag] = True
            self.last_step = step
        elif name == "chip_click" and step:
            self.last_step = step
        if name == "search_run":
            slots = parse_slots(props.get("slots_summary"))
            for k in ("category", "effect", "size", "budget"):  # the legacy beacon carries these flat
                if props.get(k) and k not in slots:
                    slots[k] = str(props[k])[:60]
            self.searches.append((slots, _num(props.get("count"))))
        if name == "bounce":
            self.bounce_event = props
            if props.get("last_step"):
                self.last_step = str(props["last_step"])[:40]
        if name == "picks_view" and self.picks_at is None:
            self.picks_at = ts
        elif self.picks_at is not None and name in _ENGAGES_WITH_PICKS:
            self.engaged_after_picks = True

    @property
    def store(self) -> str:
        return self.stores.most_common(1)[0][0] if self.stores else ""

    def count(self, *names: str) -> int:
        return sum(self.names[n] for n in names)

    @property
    def interacted(self) -> bool:
        return any(c for n, c in self.names.items() if n not in _PASSIVE)

    @property
    def zero_result_searches(self) -> int:
        return sum(1 for _, n in self.searches if n == 0)

    @property
    def outcome(self) -> str:
        """One label per session. ``bounce_*`` and ``left_*`` are the drop-offs the owner wants to see."""
        if self.count("order_ahead_click"):
            return "order_ahead"
        if self.count("product_card_click"):
            return "clicked_product"
        if self.picks_at is not None and not self.engaged_after_picks:
            return "left_after_picks"
        if self.names["search_run"] and self.zero_result_searches == self.names["search_run"]:
            return "zero_results"
        if not self.interacted:
            return "bounce_no_interaction"
        if self.picks_at is None and not self.names["search_run"]:
            return "left_before_search"
        return "engaged"

    @property
    def bounced(self) -> bool:
        return self.outcome in ("bounce_no_interaction", "left_after_picks") or bool(self.bounce_event)


def collect(days: int, store: str = "") -> dict[str, _Session]:
    """Every chat session with at least one stored chat event in the last ``days`` days."""
    since = timezone.now() - timedelta(days=days)
    qs = AnalyticsEvent.objects.filter(ts__gte=since, event_type__in=CHAT_EVENT_NAMES).exclude(session_token="")
    if store:
        qs = qs.filter(location_slug=store)
    rows = qs.order_by("ts", "id").values_list("session_token", "visitor_id", "event_type", "props",
                                               "location_slug", "ts").iterator(chunk_size=5000)
    sessions: dict[str, _Session] = {}
    for token, visitor, etype, props, lslug, ts in rows:
        props = props if isinstance(props, dict) else {}
        s = sessions.get(token)
        if s is None:
            s = sessions[token] = _Session(token)
        s.add(canonical(etype, props), props, lslug, visitor or str(props.get("visitor_id") or ""), ts)
    return sessions


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def _bucket() -> dict:
    return {"sessions": 0, "opens": 0, "searches": 0, "zero_result_searches": 0, "picks_viewed": 0,
            "product_clicks": 0, "order_ahead_clicks": 0, "bounces": 0}


def _fold(b: dict, s: _Session) -> None:
    b["sessions"] += 1
    b["opens"] += s.count("chat_open")
    b["searches"] += s.names["search_run"]
    b["zero_result_searches"] += s.zero_result_searches
    b["picks_viewed"] += 1 if s.picks_at is not None else 0
    b["product_clicks"] += s.count("product_card_click")
    b["order_ahead_clicks"] += s.count("order_ahead_click")
    b["bounces"] += 1 if s.bounced else 0


def funnel(days: int = 30, store: str = "", recent: int = 50) -> dict:
    """The owner's picture of the chat: counts per stage, drop-offs, by store and by day."""
    sessions = collect(days, store)
    ss = list(sessions.values())
    total, by_store, by_day = _bucket(), defaultdict(_bucket), defaultdict(_bucket)
    outcomes: Counter = Counter()
    last_steps: Counter = Counter()
    step_stats: dict[str, Counter] = defaultdict(Counter)
    step_pos: dict[str, list[int]] = defaultdict(list)
    cats, slot_vals, zero, ev = Counter(), Counter(), Counter(), Counter()
    for s in ss:
        _fold(total, s)
        _fold(by_store[s.store or "unknown"], s)
        _fold(by_day[s.first.date().isoformat()], s)
        outcomes[s.outcome] += 1
        ev.update(s.names)
        if s.bounced and s.last_step:
            last_steps[s.last_step] += 1
        for i, step in enumerate(s.order):
            flags = s.steps[step]
            step_pos[step].append(i)
            step_stats[step]["sessions"] += 1
            for k in ("viewed", "answered", "skipped"):
                step_stats[step][k] += 1 if flags[k] else 0
        for slots, count in s.searches:
            if slots.get("category"):
                cats[slots["category"]] += 1
            for k in _SLOT_KEYS:
                if slots.get(k):
                    slot_vals[f"{k}={slots[k]}"] += 1
            if count == 0:
                zero[" · ".join(f"{k}={v}" for k, v in sorted(slots.items())) or "(no slots)"] += 1

    steps = []
    for step, c in sorted(step_stats.items(), key=lambda kv: sum(step_pos[kv[0]]) / len(step_pos[kv[0]])):
        gone = max(c["viewed"] - c["answered"] - c["skipped"], 0)
        steps.append({"step": step, "viewed": c["viewed"], "answered": c["answered"], "skipped": c["skipped"],
                      "dropped": gone, "drop_rate": _rate(gone, c["viewed"])})

    n = len(ss)
    return {
        "window_days": days,
        "store": store or "all",
        "sessions": n,
        "unique_visitors": len({s.visitor for s in ss if s.visitor}),
        "opens": ev["chat_open"],
        "resumes": ev["chat_resume"],
        "restarts": ev["chat_restart"],
        "funnel": [
            {"stage": "opened_chat", "sessions": n},
            {"stage": "interacted", "sessions": sum(1 for s in ss if s.interacted)},
            {"stage": "searched", "sessions": sum(1 for s in ss if s.names["search_run"])},
            {"stage": "saw_picks", "sessions": sum(1 for s in ss if s.picks_at is not None)},
            {"stage": "clicked_a_product", "sessions": sum(1 for s in ss if s.count("product_card_click"))},
            {"stage": "order_ahead", "sessions": sum(1 for s in ss if s.count("order_ahead_click"))},
        ],
        "actions": {
            "searches": ev["search_run"], "zero_result_searches": total["zero_result_searches"],
            "picks_views": ev["picks_view"], "picks_collapses": ev["picks_collapse"],
            "show_more_clicks": ev["show_more_click"], "product_card_clicks": ev["product_card_click"],
            "product_expands": ev["product_expand"], "order_ahead_clicks": ev["order_ahead_click"],
            "find_similar_opens": ev["find_similar_open"], "find_similar_results": ev["find_similar_result"],
            "similar_picks": ev["similar_pick"], "specify_more_opens": ev["specify_more_open"],
            "chip_clicks": ev["chip_click"], "messages_sent": ev["chat_message_sent"],
            "pair_upsell_views": ev["pair_upsell_view"], "pair_upsell_accepts": ev["pair_upsell_accept"],
            "phone_capture_submits": ev["phone_capture_submit"], "phone_capture_skips": ev["phone_capture_skip"],
            "voice_offer_clicks": ev["voice_offer_click"],
        },
        "bounces": {
            "total": total["bounces"], "rate": _rate(total["bounces"], n),
            "no_interaction": outcomes["bounce_no_interaction"],
            "left_after_picks": outcomes["left_after_picks"],
            "left_before_search": outcomes["left_before_search"],
            "zero_results": outcomes["zero_results"],
            "last_step": [{"step": k, "sessions": v} for k, v in last_steps.most_common(10)],
        },
        "outcomes": dict(outcomes),
        "questionnaire_steps": steps,
        "top_categories": [{"category": k, "searches": v} for k, v in cats.most_common(10)],
        "top_slots": [{"slot": k, "searches": v} for k, v in slot_vals.most_common(20)],
        "zero_result_searches": [{"slots": k, "searches": v} for k, v in zero.most_common(10)],
        "by_store": {k: {**v, "bounce_rate": _rate(v["bounces"], v["sessions"])} for k, v in sorted(by_store.items())},
        "by_day": [{"date": d, **v, "bounce_rate": _rate(v["bounces"], v["sessions"])}
                   for d, v in sorted(by_day.items())],
        "recent_sessions": _recent(sorted(ss, key=lambda s: s.last, reverse=True)[:max(recent, 0)]),
    }


def _recent(rows: list[_Session]) -> list[dict]:
    pk_by_token = dict(ChatSession.objects.filter(session_token__in=[s.token for s in rows])
                       .values_list("session_token", "pk"))
    return [{
        "ref": session_ref(s.token),
        "id": pk_by_token.get(s.token),
        "store": s.store,
        "started_at": s.first.isoformat(),
        "last_event_at": s.last.isoformat(),
        "seconds": int((s.last - s.first).total_seconds()),
        "events": s.n_events,
        "searches": s.names["search_run"],
        "outcome": s.outcome,
        "last_step": s.last_step,
    } for s in rows]


# ── one session, replayed ────────────────────────────────────────────────────

def resolve_token(*, ref: str = "", chat_id: int | None = None, days: int = 90) -> str:
    """Session token for an opaque ``ref`` or a ChatSession ``id``; "" when nothing matches."""
    if chat_id:
        return ChatSession.objects.filter(pk=chat_id).values_list("session_token", flat=True).first() or ""
    ref = re.sub(r"[^0-9a-f]", "", str(ref or "").lower())
    if len(ref) != 16:
        return ""
    since = timezone.now() - timedelta(days=days)
    seen = AnalyticsEvent.objects.filter(ts__gte=since).exclude(session_token="").values_list(
        "session_token", flat=True).distinct()
    for t in seen.iterator():
        if session_ref(t) == ref:
            return t
    for t in ChatSession.objects.values_list("session_token", flat=True).iterator():
        if session_ref(t) == ref:
            return t
    return ""


def timeline(token: str) -> dict | None:
    """Events, messages and suggestions of one session in the order they happened ("what happened where")."""
    if not token:
        return None
    chat = ChatSession.objects.filter(session_token=token).first()
    events = list(AnalyticsEvent.objects.filter(session_token=token).order_by("ts", "id"))
    if chat is None and not events:
        return None
    s = _Session(token)
    items: list[dict] = []
    for e in events:
        props = e.props if isinstance(e.props, dict) else {}
        name = canonical(e.event_type, props)
        if is_chat_event(e.event_type):
            s.add(name, props, e.location_slug, e.visitor_id, e.ts)
        items.append({"at": e.ts, "kind": "event", "event": e.event_type, "name": name,
                      "store": e.location_slug, "props": {k: v for k, v in props.items() if k != "visitor_id"}})
    if chat is not None:
        for m in ChatMessage.objects.filter(session=chat).order_by("ts", "id"):
            items.append({"at": m.ts, "kind": "message", "role": m.role, "text": m.content,
                          "chips": m.chips, "result_skus": m.result_skus})
        for g in SuggestedProduct.objects.filter(session=chat).order_by("shown_at", "id"):
            items.append({"at": g.shown_at, "kind": "suggestion", "sku": g.sku, "suggestion": g.kind,
                          "source": g.source, "reason": g.reason_code, "paired_with": g.paired_with_sku})
    items.sort(key=lambda i: i["at"])
    t0 = items[0]["at"] if items else None
    for i in items:
        i["t"] = int((i["at"] - t0).total_seconds()) if t0 else 0
        i["at"] = i["at"].isoformat()
    started = chat.started_at if chat else s.first
    active = chat.last_active_at if chat else s.last
    return {
        "ref": session_ref(token),
        "id": chat.pk if chat else None,
        "store": (chat.location_slug if chat and chat.location_slug else s.store),
        "channel": chat.channel if chat else "",
        "stage": chat.stage if chat else "",
        "primary_intent": chat.primary_intent if chat else "",
        "identified": bool(chat and chat.customer_id),
        "started_at": started.isoformat() if started else "",
        "last_active_at": active.isoformat() if active else "",
        "outcome": s.outcome if s.n_events else "",
        "last_step": s.last_step,
        "counts": {"events": len(events), "messages": sum(1 for i in items if i["kind"] == "message"),
                   "suggestions": sum(1 for i in items if i["kind"] == "suggestion")},
        "timeline": items,
    }
