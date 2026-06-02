"""
/api/v1 endpoints — called ONLY by the website's server-side proxy.
No response ever includes cost/margin (see serializers.public_product).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (AnalyticsEvent, ChatMessage, ChatSession, CustomerProfile,
                     Feedback, Product, SuggestedProduct)
from .pairing import pair_for
from .ranking import rank_products
from .serializers import profile_summary, public_message, public_product
from .tasks import _normalize_phone, recompute_affinity


def _hash_phone(raw: str) -> str:
    p = _normalize_phone(raw or "")
    return hashlib.sha256(p.encode()).hexdigest() if p else ""

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
            location_slug=request.data.get("location", ""),
            channel=request.data.get("channel", "chat"),
        )
        return Response({"session_token": token, "stage": "WELCOME"})


class ProductSearchView(APIView):
    def post(self, request):
        slots = request.data.get("slots") or {}
        limit = int(request.data.get("limit", 5))
        location = slots.get("store") or request.data.get("location") or "yakima"
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

        ranked = rank_products(location, slots, profile, limit=limit, exclude_skus=exclude)
        results = [public_product(p, rank=i + 1, why_this=why) for i, (p, why) in enumerate(ranked)]

        if session:
            for r in results:
                SuggestedProduct.objects.create(
                    session=session, customer=profile, location_slug=location,
                    sku=r["sku"], kind="primary", source=session.channel,
                )
        return Response({"results": results, "source": "vps"})


class PairingView(APIView):
    def post(self, request):
        location = request.data.get("location") or "yakima"
        sku = request.data.get("sku")
        slug = request.data.get("slug")
        phone = request.data.get("phone") or ""
        profile = _profile_for_phone(phone)

        anchor = None
        if sku:
            anchor = Product.objects.filter(location_slug=location, sku=str(sku)).first()
        if anchor is None and slug:
            anchor = Product.objects.filter(location_slug=location, slug=str(slug)).first()

        pair, reason = pair_for(location, anchor, profile)
        if not pair:
            return Response({"pairing": None, "reason_code": "none"})

        session = ChatSession.objects.filter(session_token=request.data.get("session_token", "")).first()
        SuggestedProduct.objects.create(
            session=session, customer=profile, location_slug=location, sku=pair.sku,
            kind="pairing", source=(session.channel if session else "menu"),
            paired_with_sku=(anchor.sku if anchor else ""), reason_code=reason,
        )
        return Response({"pairing": public_product(pair), "reason_code": reason})


class ResumeByPhoneView(APIView):
    def post(self, request):
        phone = _normalize_phone(request.data.get("phone", ""))
        location = request.data.get("location", "")
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
        session.location_slug = (data.get("slots") or {}).get("store") or session.location_slug
        session.slots = data.get("slots") or session.slots
        session.stage = data.get("stage") or session.stage
        if phone:
            session.phone = phone
            session.customer = profile
        session.save()
        # Replace message log with the latest snapshot.
        msgs = data.get("messages") or []
        if msgs:
            session.messages.all().delete()
            for m in msgs:
                ChatMessage.objects.create(
                    session=session, role=m.get("role", "user"),
                    content=m.get("content", ""), chips=m.get("chips", []),
                    result_skus=[r.get("sku") for r in (m.get("search_results") or []) if r.get("sku")],
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
            location_slug=str(location_slug or "")[:32],
            channel=str(channel or "web")[:16],
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
                    props={k: v for k, v in merged.items() if v is not None},
                    session_token=props.get("session_id") or e.get("session_id"),
                    phone=props.get("phone"),
                    location_slug=props.get("store") or props.get("location_slug"),
                    channel=props.get("channel") or "web",
                ))
        else:
            rows.append(dict(
                event_type=d.get("event_type"), props=d.get("props"),
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
            message=msg[:4000],
            session_token=str(d.get("session_token") or "")[:64],
            phone_hash=_hash_phone(d.get("phone") or ""),
            location_slug=str(d.get("location_slug") or "")[:32],
            channel=str(d.get("channel") or "chat")[:16],
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
