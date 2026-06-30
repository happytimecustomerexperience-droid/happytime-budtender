"""
/api/v1 endpoints — called ONLY by the website's server-side proxy.
No response ever includes cost/margin (see serializers.public_product).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (STORES, AnalyticsEvent, ChatMessage, ChatSession,
                     CustomerProfile, Feedback, Product, SuggestedProduct)
from .pairing import pair_for
from . import facets
from .gemini_chat import GeminiChatUnavailable, generate_chat_reply
from .ranking import MIN_STOCK, W_ANON, W_KNOWN, rank_products
from .serializers import (customer_detail, customer_row, profile_summary,
                          public_message, public_product)
from .tasks import (_normalize_phone, ensure_inventory_fresh,
                    inventory_is_stale, recompute_affinity)


def _hash_phone(raw: str) -> str:
    p = _normalize_phone(raw or "")
    return hashlib.sha256(p.encode()).hexdigest() if p else ""


def _slug_from_name(name: str) -> str:
    """Mirror the website catalog slug algorithm (scripts/process-catalog.js)
    EXACTLY so these slugs join against the site's product slugs:
    lower -> trim -> spaces to '-' -> strip non [a-z0-9-] -> collapse '-' -> trim.
    NOTE: name-only (no SKU suffix), unlike the stored Product.slug."""
    s = (name or "").lower().strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    s = re.sub(r"^-|-$", "", s)
    return s


def _safe_chat_text(text: str) -> str:
    s = " ".join(str(text or "").split())[:1200]
    return re.sub(r"\b(cost|margin|profit|wholesale)\b", "[redacted]", s, flags=re.IGNORECASE)


_CHANNELS = {"chat", "web", "menu", "questionnaire", "voice"}
_STORE_ALIASES = {"mt-vernon": "mount-vernon", "mt vernon": "mount-vernon", "mount vernon": "mount-vernon"}
_PHONEISH_RE = re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})(?!\w)")
_PII_PROP_KEYS = {"phone", "phone_number", "email", "contact_email"}
_RANKING_WEIGHTS_CACHE_KEY = "budtender:ranking_weights:v1"


def _safe_location(value, default: str = "yakima") -> str:
    loc = str(value or "").strip().lower()
    loc = _STORE_ALIASES.get(loc, loc)
    return loc if loc in {s[0] for s in STORES} else default


def _safe_channel(value, default: str = "chat") -> str:
    channel = str(value or "").strip().lower()
    return channel if channel in _CHANNELS else default


def _safe_message_role(value) -> str:
    return "assistant" if str(value or "").strip().lower() == "assistant" else "user"


def _bounded_int(value, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return min(max(n, lo), hi)


def _safe_props(value) -> dict:
    if not isinstance(value, dict):
        return {}
    value = {str(k): _safe_prop_value(v) for k, v in value.items() if str(k).lower() not in _PII_PROP_KEYS}
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {}
    return value if len(encoded) <= 12000 else {"_truncated": True}


def _safe_list(value, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "")[:item_limit] for item in value[:limit]]


def _safe_prop_value(value):
    if isinstance(value, str):
        return _redact_phoneish(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_prop_value(v) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k): _safe_prop_value(v) for k, v in value.items() if str(k).lower() not in _PII_PROP_KEYS}
    return _redact_phoneish(str(value))


def _redact_phoneish(text: object) -> str:
    # ponytail: US phone-shape redaction; add a PII service only if international capture matters.
    return _PHONEISH_RE.sub("[phone redacted]", str(text or ""))


def _clean_weight_set(raw: object, base: dict[str, float]) -> dict[str, float]:
    weights = dict(base)
    if isinstance(raw, dict):
        for key in base:
            try:
                value = float(raw[key])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                weights[key] = value
    total = sum(weights.values())
    if total <= 0 or not math.isfinite(total):
        weights = dict(base)
        total = sum(weights.values()) or 1.0
    return {key: value / total for key, value in weights.items()}


def _clean_ranking_weights(raw: object) -> dict:
    data = raw if isinstance(raw, dict) else {}
    try:
        emphasis = float(data.get("margin_emphasis", 1.0))
    except (TypeError, ValueError):
        emphasis = 1.0
    if not math.isfinite(emphasis) or emphasis < 0:
        emphasis = 1.0
    return {
        "w_anon": _clean_weight_set(data.get("w_anon"), W_ANON),
        "w_known": _clean_weight_set(data.get("w_known"), W_KNOWN),
        "margin_emphasis": emphasis,
    }


class InStockProductsView(APIView):
    """GET /api/v1/products/in-stock/?store=<yakima|mount-vernon|pullman>

    Returns the current SALES-FLOOR inventory for one store using the SAME gate
    the recommender uses (availability=True AND quantity_on_hand >= MIN_STOCK),
    as name-derived slugs that match the website catalog, plus per-slug on-hand
    counts. Powers the website 'find similar' feature's in-stock guarantee.
    Auth: global ServiceTokenPermission (Bearer HHT_BACKEND_TOKEN). No cost/margin.
    """

    def get(self, request):
        location = (request.query_params.get("store") or "yakima").strip()
        valid = {s[0] for s in STORES}
        if location not in valid:
            return Response({"error": f"unknown store: {location}"}, status=400)
        rows = (
            Product.objects
            .filter(location_slug=location, availability=True,
                    quantity_on_hand__gte=MIN_STOCK)
            .values_list("name", "quantity_on_hand")
        )
        stock: dict[str, int] = {}
        for name, qty in rows:
            slug = _slug_from_name(name)
            if not slug:
                continue
            stock[slug] = max(stock.get(slug, 0), int(qty or 0))
        slugs = sorted(stock.keys())
        return Response({
            "store": location,
            "count": len(slugs),
            "slugs": slugs,
            "stock": stock,
            "generated_at": timezone.now().isoformat(),
        })


class ProductBySkuView(APIView):
    """GET /api/v1/products/by-sku/?store=<yakima|mount-vernon|pullman>&sku=<sku>

    One purchasable product by SKU, leak-safe (no cost/margin) — the reliable single-SKU lookup
    the voice agent's check_inventory needs (was TODO-B3; previously approximated by a capped ranked
    search that missed specific SKUs). Uses the SAME in-stock gate the recommender does
    (availability=True AND quantity_on_hand >= MIN_STOCK), so a returned row IS buyable. Returns
    {"product": {...}} or {} when not found / not in stock. Auth: global ServiceTokenPermission."""

    def get(self, request):
        location = _safe_location(request.query_params.get("store"))
        sku = (request.query_params.get("sku") or "").strip()
        if not sku:
            return Response({"error": "sku required"}, status=400)
        p = (
            Product.objects
            .filter(location_slug=location, sku=sku, availability=True,
                    quantity_on_hand__gte=MIN_STOCK)
            .first()
        )
        return Response({"product": public_product(p)} if p else {})


RESUME_WINDOW = timedelta(days=30)


def _profile_for_phone(phone: str) -> CustomerProfile | None:
    if not phone:
        return None
    return CustomerProfile.objects.filter(phone=_normalize_phone(phone)).first()


class HealthView(APIView):
    is_public = True

    def get(self, request):
        return Response({"status": "ok"})


class SessionStartView(APIView):
    def post(self, request):
        token = "s-" + secrets.token_urlsafe(24)
        ChatSession.objects.create(
            session_token=token,
            location_slug=_safe_location(request.data.get("location"), default=""),
            channel=_safe_channel(request.data.get("channel"), default="chat"),
        )
        return Response({"session_token": token, "stage": "WELCOME"})


class ChatReplyView(APIView):
    """Persist one website chat turn and answer with bounded Gemini context.

    Auth is still the global service-token gate. The browser should call this
    only through the website's server-side proxy, never directly.
    """

    def post(self, request):
        data = request.data or {}
        raw_message = str(data.get("message") or "").strip()
        if not raw_message:
            return Response({"ok": False, "error": "message required"}, status=400)

        token = str(data.get("session_token") or data.get("session_id") or "").strip()
        location = _safe_location(data.get("location") or data.get("store"))
        channel = _safe_channel(data.get("channel"), default="chat")
        if token:
            session, _ = ChatSession.objects.get_or_create(
                session_token=token,
                defaults={"location_slug": location, "channel": channel},
            )
        else:
            token = "s-" + secrets.token_urlsafe(24)
            session = ChatSession.objects.create(session_token=token, location_slug=location, channel=channel)

        phone = _normalize_phone(data.get("phone", "")) if data.get("phone") else ""
        if phone:
            profile = CustomerProfile.objects.filter(phone=phone).first()
            session.phone = phone
            session.customer = profile
        if location and not session.location_slug:
            session.location_slug = location
        session.channel = channel
        session.save()

        user_msg = ChatMessage.objects.create(
            session=session, role="user", content=_redact_phoneish(raw_message)[:4000]
        )
        AnalyticsEvent.objects.create(
            session_token=session.session_token,
            phone_hash=_hash_phone(phone),
            location_slug=session.location_slug,
            channel=session.channel,
            event_type="chat_message",
            props={"role": "user", "message_id": user_msg.id},
        )

        history = list(session.messages.order_by("ts", "id"))
        try:
            reply = _safe_chat_text(generate_chat_reply(history, store=session.location_slug))
            source = "gemini"
        except GeminiChatUnavailable:
            # ponytail: generic fallback until Gemini auth is configured; upgrade path is env config.
            reply = "I can help with product questions, store info, or finding something on the menu. What are you shopping for today?"
            source = "fallback"

        assistant_msg = ChatMessage.objects.create(session=session, role="assistant", content=reply)
        AnalyticsEvent.objects.create(
            session_token=session.session_token,
            phone_hash=_hash_phone(phone),
            location_slug=session.location_slug,
            channel=session.channel,
            event_type="chat_message",
            props={"role": "assistant", "message_id": assistant_msg.id, "source": source},
        )
        return Response({
            "ok": True,
            "session_token": session.session_token,
            "source": source,
            "message": public_message(assistant_msg),
        })


class ChatHistoryView(APIView):
    """Recent website/chatbot sessions for the voice dashboard.

    Service-token only. Raw phone is never returned; staff get channel/store/session/message context.
    """

    def post(self, request):
        data = request.data or {}
        limit = _bounded_int(data.get("limit"), default=25, lo=1, hi=100)
        message_limit = _bounded_int(data.get("message_limit"), default=200, lo=1, hi=500)
        session_token = str(data.get("session_token") or "").strip()[:128]
        sessions = (
            ChatSession.objects
            .prefetch_related("messages")
            .order_by("-last_active_at")
        )
        if session_token:
            sessions = sessions.filter(session_token=session_token)[:1]
        else:
            sessions = sessions[:limit]
        rows = []
        for session in sessions:
            messages = list(session.messages.all())
            if not messages:
                continue
            rows.append({
                "session_token": session.session_token,
                "channel": session.channel,
                "location_slug": session.location_slug,
                "stage": session.stage,
                "message_count": len(messages),
                "started_at": session.started_at.isoformat(),
                "last_active_at": session.last_active_at.isoformat(),
                "messages": [public_message(m) for m in messages[-message_limit:]],
            })
        return Response({"ok": True, "sessions": rows})


class CustomerListView(APIView):
    """Staff customer roster for the voice dashboard (service-token only, P7). Search by name or
    phone; paginated. Returns leak-safe customer rows (no cost/margin). The live, auto-recomputed
    profiles the bot uses — so the dashboard browse is always fresh."""

    def post(self, request):
        data = request.data or {}
        q = str(data.get("q") or "").strip()[:80]
        limit = _bounded_int(data.get("limit"), default=25, lo=1, hi=100)
        offset = _bounded_int(data.get("offset"), default=0, lo=0, hi=1_000_000)
        qs = CustomerProfile.objects.all()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
        total = qs.count()
        page = qs.order_by("-last_purchase_at", "-id")[offset:offset + limit]
        rows = [customer_row(p) for p in page]
        return Response({
            "ok": True, "total": total, "count": len(rows),
            "offset": offset, "limit": limit, "customers": rows,
        })


class CustomerDetailView(APIView):
    """One customer's full staff profile, by opaque ``id`` (preferred — so a phone never travels in
    a dashboard URL) or by ``phone`` (service-token only, P7). Leak-safe."""

    def post(self, request):
        data = request.data or {}
        profile = None
        cid = data.get("id")
        name = str(data.get("name") or "").strip()
        if cid not in (None, ""):
            profile = CustomerProfile.objects.filter(pk=_bounded_int(cid, default=0, lo=0, hi=2**31)).first()
        elif name:
            # Name match (case-insensitive) — used by the dashboard to enrich an analytics profile
            # with this customer's live affinities. Most-recent buyer wins on a duplicate name.
            profile = (CustomerProfile.objects.filter(name__iexact=name)
                       .order_by("-last_purchase_at", "-id").first())
        else:
            phone = _normalize_phone(str(data.get("phone") or ""))
            if not phone:
                return Response({"ok": False, "reason": "missing id/name/phone"}, status=400)
            profile = CustomerProfile.objects.filter(phone=phone).first()
        if not profile:
            return Response({"ok": False, "reason": "not found"}, status=404)
        return Response({"ok": True, "customer": customer_detail(profile)})


class ProductSearchView(APIView):
    def post(self, request):
        slots = request.data.get("slots") or {}
        limit = _bounded_int(request.data.get("limit"), default=5, lo=1, hi=20)
        location = _safe_location(slots.get("store") or request.data.get("location"))
        exclude = {str(s) for s in (request.data.get("exclude_skus") or [])}
        token = request.data.get("session_token") or ""
        # Get-or-create the session so EVERY session (incl. anonymous
        # questionnaire guests) has its suggested products recorded.
        session = None
        if token:
            session, _ = ChatSession.objects.get_or_create(
                session_token=token,
                defaults={"location_slug": location, "channel": "questionnaire"},
            )
        # Profile drives personalization: prefer the session's linked customer,
        # else resolve by a phone passed with the request (logged-in chat).
        profile = session.customer if session and session.customer else None
        if profile is None:
            profile = _profile_for_phone(request.data.get("phone") or "")
            if profile and session and not session.customer:
                session.customer = profile
                session.phone = profile.phone
                session.save(update_fields=["customer", "phone"])

        # Freshness guard: if this store's inventory is ≥24h stale, kick off an
        # async refresh so suggestions self-heal to live stock. Never blocks the
        # response (and the ranking below already filters to in-stock SKUs).
        if inventory_is_stale(location):
            try:
                ensure_inventory_fresh.delay()
            except Exception:
                pass

        ranking_weights = request.data.get("ranking_weights")
        if ranking_weights is None:
            ranking_weights = cache.get(_RANKING_WEIGHTS_CACHE_KEY)

        ranked = rank_products(
            location,
            slots,
            profile,
            limit=limit,
            exclude_skus=exclude,
            ranking_weights=ranking_weights,
        )
        results = [public_product(p, rank=i + 1, why_this=why) for i, (p, why) in enumerate(ranked)]

        if session:
            for r in results:
                SuggestedProduct.objects.create(
                    session=session, customer=profile, location_slug=location,
                    sku=r["sku"], kind="primary", source=session.channel,
                )
        return Response({"results": results, "source": "vps"})


class AdminRankingWeightsView(APIView):
    """Dashboard admin hook: accept owner ranking levers from the voice dashboard.

    Bearer auth is still the global service-token gate. Stored in cache only; use
    env/DB config if multiple independent budtender processes need distinct values.
    """

    def post(self, request):
        applied = _clean_ranking_weights(request.data or {})
        # ponytail: cache-backed override; move to a model if multi-process admin edits need audit history.
        cache.set(_RANKING_WEIGHTS_CACHE_KEY, applied, None)
        return Response({"ok": True, "applied": applied})


class PriceBandsView(APIView):
    """Data-driven budget buckets for a store+category+size, so the
    questionnaire's price step is granular and relevant to the subcategory."""

    def post(self, request):
        slots = request.data.get("slots") or request.data or {}
        location = _safe_location(slots.get("store") or request.data.get("location"))
        category = facets.resolve_category(slots.get("category"))
        # Served from the precomputed cache (warmed on every inventory sync); a cold
        # combo is computed once + cached. The request path does NO product scan.
        return Response(facets.bands(location, category, slots.get("size"), slots.get("subcategory")))


class SubtypesView(APIView):
    """Granular product subtypes that actually exist in live inventory for a
    store+category — e.g. concentrates → rosin / live resin / RSO / diamonds;
    edibles → gummies / chocolate / peanut butter cups / lollipops. DATA-DRIVEN:
    new forms appear automatically as soon as a matching SKU is synced, so the
    questionnaire's subtype step is never hardcoded."""

    def post(self, request):
        slots = request.data.get("slots") or request.data or {}
        location = _safe_location(slots.get("store") or request.data.get("location"))
        category = facets.resolve_category(slots.get("category"))
        return Response({"subtypes": facets.subtypes(location, category)})


class SizesView(APIView):
    """Distinct SIZES that actually exist in live inventory for a store+category
    (+ optional subtype) — flower's real weights (1/2/3.5/4/7/8/14/28g), a
    pre-roll's pack counts (single, 1pk…28pk). DATA-DRIVEN: a new weight/pack
    appears as soon as a matching SKU syncs, so the questionnaire's size step is
    never hardcoded. Categories with no reliable size axis (e.g. edibles) return
    an empty list and the questionnaire skips the step."""

    def post(self, request):
        slots = request.data.get("slots") or request.data or {}
        location = _safe_location(slots.get("store") or request.data.get("location"))
        category = facets.resolve_category(slots.get("category"))
        return Response({"sizes": facets.sizes(location, category, slots.get("subcategory"))})


class DohOptionsView(APIView):
    """Whether the 'DOH-certified only?' question is a REAL choice for the current
    filters — i.e. the matching in-stock set has BOTH DOH and non-DOH products. The
    questionnaire SKIPS the DOH step when it isn't (all-DOH = redundant; none-DOH =
    a dead end), so we never offer a cert filter that can't be fulfilled."""

    def post(self, request):
        slots = request.data.get("slots") or request.data or {}
        location = _safe_location(slots.get("store") or request.data.get("location"))
        category = facets.resolve_category(slots.get("category"))
        return Response(facets.doh(location, category, slots.get("size"), slots.get("subcategory"),
                                   slots.get("price_min"), slots.get("price_max")))


class AnalyticsSummaryView(APIView):
    """Chatbot/menu funnel counts — unique visitors, chat usage, messages, clicks,
    'show me something else', and timing. Read-only; powers the owner dashboard.
    Counts come from AnalyticsEvent; unique visitors dedupe on props.visitor_id."""

    def post(self, request):
        from datetime import timedelta

        days = _bounded_int((request.data or {}).get("days"), default=30, lo=1, hi=365)
        since = timezone.now() - timedelta(days=days)
        qs = AnalyticsEvent.objects.filter(ts__gte=since)

        def ev(name):
            return qs.filter(event_type=name)

        def uniq_visitors(q):
            seen = set()
            for props in q.values_list("props", flat=True):
                vid = (props or {}).get("visitor_id")
                if vid:
                    seen.add(vid)
            return len(seen)

        opens = ev("chat_open").count()
        searches = ev("chat_search").count()
        rec_views = ev("chat_recommend_view").count()
        shop_clicks = ev("chat_product_click").count()
        sme = ev("chat_show_me_something_else")
        sme_count = sme.count()
        # Avg seconds a visitor reviewed picks before asking for fresh ones.
        durations = [
            float(p.get("ms_since_results"))
            for p in sme.values_list("props", flat=True)
            if isinstance(p, dict) and isinstance(p.get("ms_since_results"), (int, float))
        ]
        avg_sme_s = round(sum(durations) / len(durations) / 1000, 1) if durations else None
        user_msgs = ev("chat_message").filter(props__role="user").count()

        # ── Single pass over the window for the JSON-prop breakdowns ──
        from collections import Counter
        from django.db.models import Avg, Count

        daily, device, loc, cat_ctr, click_ctr = Counter(), Counter(), Counter(), Counter(), Counter()
        # Engagement accumulators — all schema-free, read straight from props JSON:
        dwell_ms: list[float] = []          # chat_session_end.duration_ms = "how long they stay"
        results_dwell_ms: list[float] = []  # chat_stage_dwell.ms for the RESULTS stage
        total_opens = reopens = 0           # chat_open.open_index (a reopen is index > 1)
        post_reopen_clicks = 0              # suggestion/pairing/upsell clicks with is_reopen=true
        upsell_views = upsell_clicks = 0    # post-reopen pairing-modal funnel (view → click)
        CLICK_EVENTS = {"chat_product_click", "chat_pairing_click", "chat_pair_upsell_click"}
        for etype, props, lslug, ts in qs.values_list("event_type", "props", "location_slug", "ts"):
            p = props or {}
            daily[ts.date().isoformat()] += 1
            if p.get("device_type"):
                device[str(p["device_type"])] += 1
            if lslug:
                loc[str(lslug)] += 1
            if etype == "chat_search" and p.get("category"):
                cat_ctr[str(p["category"])] += 1
            elif etype == "chat_product_click" and p.get("sku"):
                click_ctr[str(p["sku"])] += 1
            elif etype == "chat_open":
                total_opens += 1
                idx = p.get("open_index")
                if (isinstance(idx, (int, float)) and idx > 1) or p.get("is_reopen") is True:
                    reopens += 1
            elif etype == "chat_session_end":
                d = p.get("duration_ms")
                if isinstance(d, (int, float)) and d >= 0:
                    dwell_ms.append(float(d))
            elif etype == "chat_stage_dwell":
                if p.get("stage") == "RESULTS" and isinstance(p.get("ms"), (int, float)):
                    results_dwell_ms.append(float(p["ms"]))
            elif etype == "chat_pair_upsell_view":
                upsell_views += 1
            elif etype == "chat_pair_upsell_click":
                upsell_clicks += 1
            # Separate (not elif): a click event already matched above still counts here.
            if etype in CLICK_EVENTS and p.get("is_reopen") is True:
                post_reopen_clicks += 1

        # ── Engagement summaries from the accumulators ──
        def _dur_stats(ms_list):
            if not ms_list:
                return (None, None, 0)
            s = sorted(ms_list)
            n = len(s)
            med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
            return (round(sum(s) / n / 1000, 1), round(med / 1000, 1), n)

        dwell_avg_s, dwell_med_s, dwell_n = _dur_stats(dwell_ms)
        results_avg_s, _, _ = _dur_stats(results_dwell_ms)
        prim_impressions = SuggestedProduct.objects.filter(shown_at__gte=since, kind="primary").count()
        suggestion_ctr = round(shop_clicks / prim_impressions, 3) if prim_impressions else None
        pair_upsell_ctr = round(upsell_clicks / upsell_views, 3) if upsell_views else None

        # ── Which products get suggested / clicked (the merchandising view) ──
        sugg = list(SuggestedProduct.objects.filter(shown_at__gte=since, kind="primary")
                    .values("sku").annotate(n=Count("id")).order_by("-n")[:12])
        pairs = list(SuggestedProduct.objects.filter(shown_at__gte=since, kind="pairing")
                     .values("sku").annotate(n=Count("id")).order_by("-n")[:12])
        clicked = click_ctr.most_common(12)
        want = {s["sku"] for s in sugg} | {s["sku"] for s in pairs} | {s for s, _ in clicked}
        name_by_sku: dict[str, str] = {}
        for sku_, name_ in Product.objects.filter(sku__in=list(want)).values_list("sku", "name"):
            name_by_sku.setdefault(sku_, name_)
        def nm(s):
            return name_by_sku.get(s, s)
        click_n = dict(clicked)

        fb_qs = Feedback.objects.filter(ts__gte=since)
        avg_rating = fb_qs.filter(rating__isnull=False).aggregate(a=Avg("rating"))["a"]

        return Response({
            "window_days": days,
            "unique_visitors": uniq_visitors(qs),
            "total_events": qs.count(),
            "chat": {
                "opens": opens,
                "unique_chat_users": uniq_visitors(ev("chat_open")),
                "user_messages_sent": user_msgs,
                "questionnaire_searches": searches,
                "recommendation_views": rec_views,
                "show_me_something_else_clicks": sme_count,
                "avg_seconds_reviewing_before_refresh": avg_sme_s,
            },
            "conversions": {
                "shop_now_clicks": shop_clicks,
                "pairing_addon_clicks": ev("chat_pairing_click").count(),
                "pair_upsell_clicks": upsell_clicks,
                "open_to_shopnow_rate": round(shop_clicks / opens, 3) if opens else None,
                "view_to_shopnow_rate": round(shop_clicks / rec_views, 3) if rec_views else None,
                # Of the suggestions the budtender showed, what share got a Shop-Now click.
                "suggestion_impressions": prim_impressions,
                "suggestion_ctr": suggestion_ctr,
                # Clicks on suggestions shown AFTER the visitor reopened the chatbot.
                "post_reopen_clicks": post_reopen_clicks,
                "pair_upsell_views": upsell_views,
                "pair_upsell_ctr": pair_upsell_ctr,
            },
            # How long visitors stay + how often they come back into the panel.
            "engagement": {
                "avg_dwell_seconds": dwell_avg_s,
                "median_dwell_seconds": dwell_med_s,
                "sessions_ended": dwell_n,
                "total_opens": total_opens,
                "reopens": reopens,
                "reopen_rate": round(reopens / total_opens, 3) if total_opens else None,
                "avg_results_view_seconds": results_avg_s,
            },
            "menu_embed": {
                "product_views": ev("dutchie_product_view").count(),
                "add_to_cart": ev("dutchie_add_to_cart").count(),
                "checkout_started": ev("dutchie_checkout").count(),
            },
            "top_suggested_products": [{"sku": s["sku"], "name": nm(s["sku"]), "suggested": s["n"],
                                        "clicked": click_n.get(s["sku"], 0)} for s in sugg],
            "top_clicked_products": [{"sku": s, "name": nm(s), "clicks": n} for s, n in clicked],
            "top_pairings_suggested": [{"sku": s["sku"], "name": nm(s["sku"]), "count": s["n"]} for s in pairs],
            "category_interest": [{"category": c, "searches": n} for c, n in cat_ctr.most_common(10)],
            "by_location": dict(loc),
            "by_device": dict(device),
            "daily_activity": [{"date": d, "events": daily[d]} for d in sorted(daily)],
            "feedback": {"count": fb_qs.count(), "avg_rating": round(avg_rating, 2) if avg_rating else None},
        })


class PairingView(APIView):
    def post(self, request):
        location = _safe_location(request.data.get("location"))
        sku = request.data.get("sku")
        slug = request.data.get("slug")
        phone = request.data.get("phone") or ""
        profile = _profile_for_phone(phone)

        anchor = None
        if sku:
            anchor = Product.objects.filter(location_slug=location, sku=str(sku)).first()
        if anchor is None and slug:
            anchor = Product.objects.filter(location_slug=location, slug=str(slug)).first()

        pair, reason, reason_text, strength = pair_for(location, anchor, profile)
        if not pair:
            return Response({"pairing": None, "reason_code": "none", "reason_text": "", "strength": 0.0})

        session = ChatSession.objects.filter(session_token=request.data.get("session_token", "")).first()
        SuggestedProduct.objects.create(
            session=session, customer=profile, location_slug=location, sku=pair.sku,
            kind="pairing", source=(session.channel if session else "menu"),
            paired_with_sku=(anchor.sku if anchor else ""), reason_code=reason,
        )
        return Response({
            "pairing": public_product(pair), "reason_code": reason,
            "reason_text": reason_text, "strength": strength,
        })


class ResumeByPhoneView(APIView):
    def post(self, request):
        phone = _normalize_phone(request.data.get("phone", ""))
        current = request.data.get("current_session_token")
        profile = CustomerProfile.objects.filter(phone=phone).first() if phone else None

        # Link the in-flight session to the customer.
        if current:
            ChatSession.objects.filter(session_token=current).update(
                phone=phone, customer=profile, last_active_at=timezone.now()
            )
        if profile:
            recompute_affinity.delay(phone)

        prior = (
            ChatSession.objects.filter(phone=phone, started_at__gte=timezone.now() - RESUME_WINDOW)
            .exclude(session_token=current or "")
            .order_by("-last_active_at")
            .first()
            if phone
            else None
        )
        if not prior:
            return Response({"resumed": False, "session_token": current, "stage": "WELCOME",
                             "slots": {}, "messages": [], "prior_suggestions": [],
                             "profile_summary": profile_summary(profile)})

        messages = [public_message(m) for m in prior.messages.all()]
        sugg_skus = list(
            SuggestedProduct.objects.filter(session=prior).order_by("-shown_at").values_list("sku", flat=True)[:10]
        )
        return Response({
            "resumed": True,
            "session_token": prior.session_token,
            "stage": prior.stage,
            "slots": prior.slots,
            "messages": messages,
            "prior_suggestions": sugg_skus,
            "profile_summary": profile_summary(profile),
        })


class PersistView(APIView):
    def post(self, request):
        data = request.data or {}
        token = data.get("session_id") or data.get("session_token")
        if not token:
            return Response({"ok": False}, status=202)
        phone = _normalize_phone(data.get("phone", "")) if data.get("phone") else ""
        profile = CustomerProfile.objects.filter(phone=phone).first() if phone else None
        session, _ = ChatSession.objects.get_or_create(session_token=token)
        session.location_slug = _safe_location(
            (data.get("slots") or {}).get("store"), default=session.location_slug
        )
        session.slots = data.get("slots") or session.slots
        session.stage = data.get("stage") or session.stage
        if phone:
            session.phone = phone
            session.customer = profile
        session.save()
        # Replace message log with the latest snapshot.
        msgs = data.get("messages") if isinstance(data.get("messages"), list) else []
        if msgs:
            session.messages.all().delete()
            # ponytail: cap browser snapshots; move to paged transcript ingest if sessions exceed 500 turns.
            for m in msgs[-500:]:
                if not isinstance(m, dict):
                    continue
                ChatMessage.objects.create(
                    session=session, role=_safe_message_role(m.get("role")),
                    content=_redact_phoneish(m.get("content"))[:4000],
                    chips=_safe_list(m.get("chips"), limit=20, item_limit=80),
                    result_skus=[
                        str(r.get("sku"))[:64]
                        for r in (m.get("search_results") or [])[:50]
                        if isinstance(r, dict) and r.get("sku")
                    ],
                )
        return Response({"ok": True}, status=202)


class TrackView(APIView):
    """Analytics ingest — records EVERYTHING. Accepts two shapes:
      • batch from the site-wide tracker: {v, events:[{event, props, session_id, ...}]}
      • single event from the menu widget:  {event_type, channel, session_token, ...}
    Phone is always HASHED. Best-effort: never errors the caller."""

    def _store_one(self, *, event_type, props, session_token, phone, location_slug, channel):
        if not event_type:
            return
        AnalyticsEvent.objects.create(
            session_token=str(session_token or "")[:64],
            phone_hash=_hash_phone(phone or ""),
            location_slug=_safe_location(location_slug, default=""),
            channel=_safe_channel(channel, default="web"),
            event_type=str(event_type)[:32],
            props=props if isinstance(props, dict) else {},
        )

    def post(self, request):
        d = request.data or {}
        rows = []
        if isinstance(d.get("events"), list):
            for e in d["events"]:
                if not isinstance(e, dict):
                    continue
                props = e.get("props") if isinstance(e.get("props"), dict) else {}
                # carry the tracker's context into props so nothing is lost
                merged = {**props, "path": e.get("path"), "device_type": e.get("device_type"),
                          "visitor_id": e.get("visitor_id"), "ts": e.get("ts")}
                rows.append(dict(
                    event_type=e.get("event"),
                    props=_safe_props({k: v for k, v in merged.items() if v is not None}),
                    session_token=props.get("session_id") or e.get("session_id"),
                    phone=props.get("phone"),
                    location_slug=props.get("store") or props.get("location_slug"),
                    channel=props.get("channel") or "web",
                ))
        else:
            rows.append(dict(
                event_type=d.get("event_type"), props=_safe_props(d.get("props")),
                session_token=d.get("session_token"), phone=d.get("phone"),
                location_slug=d.get("location_slug"), channel=d.get("channel") or "chat",
            ))
        for r in rows:
            try:
                self._store_one(**r)
            except Exception:  # noqa: BLE001 — never fail a tracking beacon
                pass
        return Response({"ok": True, "stored": len(rows)}, status=202)


class FeedbackView(APIView):
    """Store customer feedback. Phone is hashed; raw email kept only when the
    customer opts into a reply. Also logged as an analytics event."""
    def post(self, request):
        d = request.data or {}
        msg = str(d.get("message") or "").strip()
        rating = d.get("rating")
        try:
            rating = int(rating) if rating is not None else None
        except (TypeError, ValueError):
            rating = None
        if not msg and rating is None:
            return Response({"ok": False, "error": "empty"}, status=400)
        fb = Feedback.objects.create(
            rating=rating,
            category=str(d.get("category") or "")[:32],
            message=_redact_phoneish(msg)[:4000],
            session_token=str(d.get("session_token") or "")[:64],
            phone_hash=_hash_phone(d.get("phone") or ""),
            location_slug=_safe_location(d.get("location_slug"), default=""),
            channel=_safe_channel(d.get("channel"), default="chat"),
            contact_email=str(d.get("contact_email") or "")[:254] if d.get("contact_ok") else "",
        )
        AnalyticsEvent.objects.create(
            session_token=fb.session_token, phone_hash=fb.phone_hash,
            location_slug=fb.location_slug, channel=fb.channel,
            event_type="feedback", props={"rating": rating, "category": fb.category},
        )
        return Response({"ok": True}, status=201)


class ProfileUpsertView(APIView):
    def post(self, request):
        phone = _normalize_phone(request.data.get("phone", ""))
        if not phone:
            return Response({"status": "no-phone"}, status=400)
        profile, _ = CustomerProfile.objects.get_or_create(phone=phone)
        recompute_affinity.delay(phone)
        return Response({"status": "ok", "profile_summary": profile_summary(profile)})
