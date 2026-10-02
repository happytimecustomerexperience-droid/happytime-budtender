"""Per-call staff-alert sinks for the voice repo (12-P2 §3.2 / §4.5; ADR-017).

Ported from swedish-bot/crm/sinks.py (EmailSink + the independent + idempotent ``dispatch``
pattern), retargeted from ``service_request`` → ``VoiceCall``. The durable ``VoiceCall`` row IS the
record (``DBSink`` is a no-op); ``EmailSink`` sends a per-call digest to ``STAFF_ALERT_EMAIL`` —
with an ``— URGENT`` subject on an immediate-alert outcome (escalation / vendor / defective). Each
sink is independent (one failing never blocks the others) and ``dispatch`` is **idempotent** per
``(voice_call, sink)`` via the ``AlertDelivery`` ledger, so a re-delivered eocr (Vapi retries) never
re-sends an email. ``dispatch`` never raises — a sink failure is recorded ``failed``, never fatal.

Slack is the optional secondary sink (off until ``SLACK_WEBHOOK_URL`` is set, O-9) and only fires
on an immediate alert — the durable ``VoiceCall`` + email are authoritative.

Each outbound sink also follows its owner switch on /dashboard/capabilities/ (``alerts.email`` /
``alerts.slack`` / ``alerts.n8n``); the test-session suppression in ``dispatch`` runs before any
of them, and website-chat alerts are capped per visitor first and per store as a backstop, per hour
(``_over_text_alert_cap``). Visitor text reaches staff through ``defang`` — inert, never a link.

The transfer heads-up (``deliver_transfer_notice``, driven by ``crm.transfer_notice``) is a separate
path: it fires while a call is being transferred, not at the end, over Pushover + the Slack and
email channels.

Leak-Guard (12-P2 §4.5): the email body is built ONLY from ``VoiceCall`` fields + ``ai_summary`` —
no product cost/margin field exists on the row; a contract test asserts no ``cost``/``margin``
substring. PII: the hashed caller, never the raw number.
"""

from __future__ import annotations

import contextlib
import contextvars
import http.client
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.parse
import urllib.request
from html import escape

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives

from voice import capabilities, outcomes
from voice import constants as C

logger = logging.getLogger(__name__)


def _recipients_for(store: str) -> list[str]:
    """Recipient list for a store's alert: the shared ``STAFF_ALERT_EMAIL`` PLUS any per-store
    override (additive, not replacing — 12-P2 §9). De-duplicated, order-stable."""
    shared = getattr(settings, "STAFF_ALERT_EMAIL", "") or ""
    per_store = {
        "yakima": getattr(settings, "STAFF_ALERT_EMAIL_YAKIMA", ""),
        "mount-vernon": getattr(settings, "STAFF_ALERT_EMAIL_MTVERNON", ""),
        "pullman": getattr(settings, "STAFF_ALERT_EMAIL_PULLMAN", ""),
    }.get(store, "")
    out: list[str] = []
    for addr in (shared, per_store):
        if addr and addr not in out:
            out.append(addr)
    return out


def _is_immediate(voice_call) -> bool:
    """Whether this call warrants an immediate (URGENT) alert — escalation/vendor/defective."""
    return outcomes.is_immediate_alert(voice_call.outcome or "", voice_call.reason or "")


def _safe_text(value, default: str = "") -> str:
    """Email-safe text with the same no-cost/no-margin wall as spoken tool results."""
    from voice import guardrails

    text = str(value or default)
    scrubbed = guardrails.scrub_leak(text)
    if isinstance(scrubbed, dict):
        return "[redacted: leak blocked]"
    return scrubbed


# A visitor can type anything, and a mail client or Slack turns a URL in it into a link staff may
# click. ``defang`` makes it inert text: ``scheme://`` becomes ``hxxp://`` and the dot before a
# 2-24 letter TLD becomes ``[.]`` (the ideographic and fullwidth dots too, which browsers read as a
# dot). It cannot tell "evil.co" from "refund.Please" and does not try. Idempotent.
_SCHEME_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_DOMAIN_DOT_RE = re.compile(r"(?<=\w)[.。．｡](?=[^\W\d_]{2,24}(?!\w))")


def defang(text) -> str:
    """``text`` with every link made inert: ``https://evil.co/x`` -> ``hxxp://evil[.]co/x``."""
    return _DOMAIN_DOT_RE.sub("[.]", _SCHEME_RE.sub("hxxp://", str(text or "")))


def _staff_text(value, default: str = "") -> str:
    """Visitor-derived text for a staff-facing channel (email, Slack): leak-scrubbed, then defanged."""
    return defang(_safe_text(value, default))


def _conversation_lines(voice_call) -> list[str]:
    """Full conversation log for staff: VoiceTurn rows first, transcript fallback second."""
    turns = list(voice_call.turns.order_by("seq"))
    if turns:
        lines = []
        for t in turns:
            text = _staff_text(t.text)
            tool = _staff_text(t.tool_name)
            if not text and not tool:
                continue
            label = (t.role or "turn").upper()
            if tool:
                label = f"{label} [{tool}]"
            lines.append(f"{label}: {text or '(tool call)'}")
        return lines
    transcript = _staff_text(getattr(voice_call, "transcript", ""))
    if transcript:
        return [transcript]
    # A text-channel escalation fires DURING the turn, before that turn is persisted as a
    # VoiceTurn, so a first-message dispute has no turns yet. The tool's summary IS the caller's
    # message; show it rather than an empty log (the 2026-09-18 alerts read "(no transcript
    # captured)" three times).
    summary = _staff_text(getattr(voice_call, "ai_summary", ""))
    return [f"CALLER: {summary}"] if summary else ["(no transcript captured)"]


def _is_chat(voice_call) -> bool:
    """Website chat sessions are minted as ``s-…``; everything else is a phone call."""
    return str(voice_call.call_id or "").startswith("s-")


def _channel_label(voice_call) -> str:
    return "website chat" if _is_chat(voice_call) else "voice"


def _slack_escape(value) -> str:
    """Slack's required escaping for message text: a visitor's ``<!channel>`` or
    ``<https://evil|login>`` must reach staff as literal text, never as a ping or a disguised link."""
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TEXT_ALERT_WINDOW_S = 3600
_VISITOR_CAP_DEFAULT = 2  # website-chat alerts per visitor IP per hour
_STORE_CAP_DEFAULT = 20  # the per-store backstop behind it

# The website visitor behind the request being answered (see ``visitor_ip``). The alert fires in the
# same request, in the same thread, so a context variable carries it without a model field or a
# change to the chat brain's ctx; nothing is stored.
_visitor: contextvars.ContextVar[str] = contextvars.ContextVar("hht_visitor_ip", default="")


def _visitor_bucket(ip: str) -> str:
    """The cap bucket for a visitor address: IPv4 as is, IPv6 by its /64 (one subscriber holds a whole
    /64, so per-address counting would let them mint a fresh budget per request). ``""`` if not an IP."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    addr = getattr(addr, "ipv4_mapped", None) or addr
    return str(ipaddress.ip_network(f"{addr}/64", strict=False)) if addr.version == 6 else str(addr)


@contextlib.contextmanager
def visitor_ip(ip: str):
    """Mark ``ip`` as the website visitor for everything that runs inside the block, so a chat alert
    fired there is counted against that visitor. ``""`` means "no vouched visitor": only the store
    backstop applies."""
    token = _visitor.set(_visitor_bucket(ip))
    try:
        yield
    finally:
        _visitor.reset(token)


def _cap(name: str, default: int) -> int:
    try:
        return max(0, int(str(getattr(settings, name, default)).strip()))
    except (TypeError, ValueError):
        return default


def _count_this_hour(prefix: str) -> int:
    """Count one event under ``prefix`` in the current clock hour; returns the new total."""
    key = f"{prefix}:{int(time.time() // _TEXT_ALERT_WINDOW_S)}"
    try:
        cache.add(key, 0, timeout=_TEXT_ALERT_WINDOW_S)
        return cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, timeout=_TEXT_ALERT_WINDOW_S)
        return 1


def _over_text_alert_cap(voice_call) -> bool:
    """Whether this website-chat alert is held back instead of sent.

    One visitor rotating session ids mints a fresh VoiceCall (and a fresh URGENT alert) per id, and
    the ledger only dedupes per call. So chat alerts are counted per visitor IP per clock hour first
    (``HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR``, default 2): a flooder runs out of their own budget
    without touching anyone else's. A visitor over their own limit does not count against the store.
    The per-store count (``HHT_TEXT_ALERT_CAP_PER_STORE_HOUR``, default 20) is only the backstop; the
    first alert it holds in a store-hour sends one roll-up email so a flood never reads as silence.
    A phone call is never capped. A cache failure lets the alert through: a real dispute must not
    be silenced because Redis hiccupped."""
    if not _is_chat(voice_call):
        return False
    store_cap = _cap("HHT_TEXT_ALERT_CAP_PER_STORE_HOUR", _STORE_CAP_DEFAULT)
    try:
        ip = _visitor.get()
        if ip and _count_this_hour(f"text_alert_cap_visitor:{ip}") > _cap(
            "HHT_TEXT_ALERT_CAP_PER_VISITOR_HOUR", _VISITOR_CAP_DEFAULT
        ):
            return True
        held = _count_this_hour(f"text_alert_cap:{voice_call.store or '-'}") - store_cap
    except Exception:  # noqa: BLE001 — see docstring: fail open, the VoiceCall row is saved either way
        logger.warning("text alert cap unavailable; alerting anyway", exc_info=True)
        return False
    if held > 0:
        _send_rollup(voice_call.store, held, store_cap)
    return held > 0


def _send_rollup(store: str, held: int, cap: int) -> None:
    """ONE email per store per clock hour saying website-chat alerts are being held. Follows the
    email switch and the store's recipients; never raises."""
    marker = f"text_alert_rollup:{store or '-'}:{int(time.time() // _TEXT_ALERT_WINDOW_S)}"
    try:
        if not cache.add(marker, 1, timeout=_TEXT_ALERT_WINDOW_S):
            return  # this store-hour already has its roll-up
    except Exception:  # noqa: BLE001
        logger.warning("text alert roll-up marker unavailable; no roll-up sent", exc_info=True)
        return
    recipients = _recipients_for(store)
    if not (recipients and capabilities.is_enabled("alerts.email")):
        return
    name = C.spoken_store(store) if store else "A store"
    more = f"{held} more website-chat alert{' was' if held == 1 else 's were'} held this hour"
    try:
        EmailMultiAlternatives(
            subject=f"[Happy Time voice] {name} — website-chat alerts held",
            body=(
                f"{more} — see the dashboard.\n\n"
                f"{name} reached its limit of {cap} website-chat alerts per hour, so any further ones "
                "this hour are held too and this is the only email about it. Every chat is still "
                "logged on the dashboard."
            ),
            from_email=getattr(settings, "LEAD_EMAIL_FROM", "bot@happytimeweed.com"),
            to=recipients,
        ).send(fail_silently=False)
    except Exception:  # noqa: BLE001 — an alert must never crash the request that raised it
        logger.warning("text alert roll-up email failed", exc_info=True)
        with contextlib.suppress(Exception):
            cache.delete(marker)  # let the next held alert this hour try again


def _text_body(voice_call, transfer: str, reason_line: str) -> str:
    conversation = "\n".join(_conversation_lines(voice_call))
    opener = "New website chat" if _is_chat(voice_call) else "New voice call"
    return (
        f"{opener} - {voice_call.store or '-'}.\n"
        f"Outcome: {voice_call.outcome or '-'}{reason_line}\n"
        f"Caller (hashed): {(voice_call.caller_phone_hash or '-')[:12]}...\n"
        f"Duration: {voice_call.duration_s or '-'}s\n"
        f"Human requested: {voice_call.human_requested_count}x\n"
        f"Transfer: {transfer}\n\n"
        f"Summary:\n{_staff_text(voice_call.ai_summary, '(none)')}\n\n"
        f"Conversation log:\n{conversation}\n\n"
        f"Call id: {voice_call.call_id}   logged {voice_call.created_at}\n"
    )


def _html_body(voice_call, transfer: str, reason_line: str, immediate: bool) -> str:
    rows = []
    for line in _conversation_lines(voice_call):
        role, _, text = line.partition(":")
        rows.append(
            f"<tr><td>{escape(role)}</td><td>{escape(text.strip() if text else role)}</td></tr>"
        )
    badge = "URGENT" if immediate else "Call"
    return f"""<!doctype html>
<html>
  <body style="font-family:Arial,sans-serif;color:#1f2933;line-height:1.45">
    <h2>Happy Time {escape(_channel_label(voice_call))} alert</h2>
    <p><strong>{escape(badge)}</strong> - {escape(voice_call.store or 'store')} - {escape(voice_call.outcome or 'call')}</p>
    <table cellpadding="6" cellspacing="0" style="border-collapse:collapse">
      <tr><td><strong>Reason</strong></td><td>{escape(reason_line.strip() or '-')}</td></tr>
      <tr><td><strong>Caller hash</strong></td><td>{escape((voice_call.caller_phone_hash or '-')[:12])}</td></tr>
      <tr><td><strong>Duration</strong></td><td>{escape(str(voice_call.duration_s or '-'))}s</td></tr>
      <tr><td><strong>Human requested</strong></td><td>{voice_call.human_requested_count}x</td></tr>
      <tr><td><strong>Transfer</strong></td><td>{escape(transfer)}</td></tr>
      <tr><td><strong>Call id</strong></td><td>{escape(voice_call.call_id)}</td></tr>
    </table>
    <h3>Summary</h3>
    <p>{escape(_staff_text(voice_call.ai_summary, '(none)'))}</p>
    <h3>Conversation log</h3>
    <table cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
      <tr><th align="left">Role</th><th align="left">Message</th></tr>
      {''.join(rows)}
    </table>
  </body>
</html>"""


# ── The n8n webhook POST: public addresses only, no redirects ─────────────────────────────────────
# The dashboard checks the n8n URL's host NAME on save, but a name proves nothing about where it
# points (``169.254.169.254.nip.io`` is a public-looking name for the cloud metadata address) and a
# 3xx can bounce a request anywhere. So the call itself refuses unless EVERY address the host
# resolves to is public, connects to the very address it checked (no second lookup to rebind), and
# treats a redirect as a failure. https only. Used by ``N8nSink``, ``send_staff_alert`` and the
# ``notify_n8n`` tool. Slack and Pushover do not use it: their hosts are fixed (hooks.slack.com is
# pinned by the credentials editor, Pushover is a constant), so there is no name to be fooled by.


def _public_address(host: str, port: int) -> str:
    """The address to connect to for ``host``; raises unless every address it resolves to is public."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = list(dict.fromkeys(info[4][0].split("%")[0] for info in infos))  # de-duplicated, resolver order
    if not addrs or not all(ipaddress.ip_address(a).is_global for a in addrs):
        raise OSError(f"refusing {host}: it does not resolve to public addresses only")
    return addrs[0]


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection((_public_address(self.host, self.port), self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


# A bare opener, not ``build_opener``: no ``HTTPRedirectHandler`` is installed, so a 3xx surfaces as an
# ``HTTPError`` (a failure, never a second request), and with no plain-HTTP or file handler an
# ``http://`` or ``file://`` URL fails too.
_OPENER = urllib.request.OpenerDirector()
for _handler in (
    _PublicHTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.HTTPErrorProcessor(), urllib.request.UnknownHandler(),
):
    _OPENER.add_handler(_handler)


def post_webhook(url: str, data: bytes, *, timeout: float = 10) -> int:
    """POST ``data`` as JSON to an owner-configured webhook and return the HTTP status. Raises when
    the host is not public-only, on a redirect, on any non-2xx, and on a network error."""
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=timeout) as r:
        return r.status


class Sink:
    name = "base"

    def enabled(self, voice_call) -> bool:
        return True

    def deliver(self, voice_call) -> None:
        raise NotImplementedError


class DBSink(Sink):
    """The durable ``VoiceCall`` row IS the record — already written synchronously by the eocr
    handler. Always succeeds (the idempotency boundary is the unique ``call_id``)."""

    name = "db"

    def deliver(self, voice_call) -> None:
        return None


class EmailSink(Sink):
    name = "email"

    def enabled(self, voice_call) -> bool:
        return capabilities.is_enabled("alerts.email") and bool(_recipients_for(voice_call.store))

    def deliver(self, voice_call) -> None:
        recipients = _recipients_for(voice_call.store)
        immediate = _is_immediate(voice_call)
        urgent = " — URGENT" if immediate else ""
        subject = defang(
            f"[Happy Time voice] {voice_call.store or 'store'} — "
            f"{voice_call.outcome or 'call'}{urgent}"
        )
        reason_line = f"  (reason: {_staff_text(voice_call.reason)})" if voice_call.reason else ""
        transfer = (
            f"{voice_call.transfer_disposition or '—'} ({voice_call.transfer_number_key or '—'})"
        )
        body = _text_body(voice_call, transfer, reason_line)
        msg = EmailMultiAlternatives(
            subject=subject[:120],
            body=body,
            from_email=getattr(settings, "LEAD_EMAIL_FROM", "bot@happytimeweed.com"),
            to=recipients,
        )
        msg.attach_alternative(_html_body(voice_call, transfer, reason_line, immediate), "text/html")
        msg.send(fail_silently=False)


class SlackSink(Sink):
    name = "slack"

    def enabled(self, voice_call) -> bool:
        # Fires ONLY on an immediate alert, when the webhook URL is set and the switch is on.
        return bool(
            capabilities.is_enabled("alerts.slack")
            and getattr(settings, "SLACK_WEBHOOK_URL", "")
            and _is_immediate(voice_call)
        )

    def deliver(self, voice_call) -> None:
        url = settings.SLACK_WEBHOOK_URL
        block = {
            "store": voice_call.store or "store",
            "outcome": voice_call.outcome or "call",
            "reason": _staff_text(voice_call.reason),
            "summary": _staff_text(voice_call.ai_summary, "(no summary)"),
            "call_id": voice_call.call_id,
        }
        block = {key: _slack_escape(value) for key, value in block.items()}
        data = json.dumps({"text": json.dumps(block)}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (config-supplied URL)
            if r.status >= 300:
                raise RuntimeError(f"slack HTTP {r.status}")


class N8nSink(Sink):
    """POST a leak-safe call event to a configured n8n webhook (P6). Fires on EVERY call when
    ``N8N_WEBHOOK_URL`` is set (the credentials editor surfaces it) — n8n owns the downstream
    automation (CRM sync, SMS, sheets, etc.). Leak-safe: VoiceCall carries no cost/margin; the
    caller is the peppered hash, never a raw number (PII discipline)."""

    name = "n8n"

    def enabled(self, voice_call) -> bool:
        # The credentials editor applies N8N_WEBHOOK_URL to settings (and os.environ) on save.
        return capabilities.is_enabled("alerts.n8n") and bool(getattr(settings, "N8N_WEBHOOK_URL", ""))

    def deliver(self, voice_call) -> None:
        url = settings.N8N_WEBHOOK_URL
        payload = {
            "event": "voice_call",
            "call_id": voice_call.call_id,
            "store": voice_call.store or "",
            "outcome": voice_call.outcome or "",
            "reason": voice_call.reason or "",
            "escalated": bool(voice_call.escalated),
            "human_requested": voice_call.human_requested_count,
            "duration_s": voice_call.duration_s,
            "caller_hash": (voice_call.caller_phone_hash or "")[:16],
            "suggested_skus": list(voice_call.suggested_skus or []),
            "summary": _safe_text(voice_call.ai_summary, ""),
        }
        status = post_webhook(url, json.dumps(payload).encode())
        if status >= 300:
            raise RuntimeError(f"n8n HTTP {status}")


SINKS: list[Sink] = [DBSink(), EmailSink(), SlackSink(), N8nSink()]


# ── Transfer heads-up channels (the call.sms_on_transfer switch) ───────────────────────────────
# Real SMS is not available to a cannabis retailer (carriers/Twilio refuse it), so the "text" that
# says who is calling goes out as a phone push (Pushover) and/or the Slack + email sinks above.
# None of these is in ``SINKS``: that list fires once per FINISHED call; the heads-up is sent while
# the call is being transferred, by ``crm.transfer_notice`` (which owns the gates + the ledger).

_NOTICE_TIMEOUT_S = 5


def _post(url: str, data: bytes, content_type: str) -> bytes:
    """One bounded POST for a heads-up channel. Raises on a non-2xx; returns the body."""
    req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type}, method="POST")
    with urllib.request.urlopen(req, timeout=_NOTICE_TIMEOUT_S) as r:  # noqa: S310 (fixed or config-supplied URL)
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}")
        return r.read()


class PushoverSink:
    """A phone push through Pushover — one HTTPS POST per notice, to the store's own user key.
    Inert (no request at all) until BOTH ``PUSHOVER_APP_TOKEN`` and that store's
    ``PUSHOVER_USER_<KEY>`` are set."""

    name = "pushover"
    URL = "https://api.pushover.net/1/messages.json"

    @staticmethod
    def _user(store_key: str) -> str:
        return getattr(settings, f"PUSHOVER_USER_{store_key}", "") or ""

    def enabled(self, store_key: str) -> bool:
        return bool(getattr(settings, "PUSHOVER_APP_TOKEN", "") and self._user(store_key))

    def deliver(self, store_key: str, text: str) -> None:
        form = urllib.parse.urlencode(
            {"token": settings.PUSHOVER_APP_TOKEN, "user": self._user(store_key), "message": text}
        ).encode()
        reply = json.loads(_post(self.URL, form, "application/x-www-form-urlencoded") or b"{}")
        if reply.get("status") != 1:  # an accepted push says status 1; a refusal is not a send
            raise RuntimeError(f"pushover refused: {reply.get('errors') or 'no status'}")


def deliver_transfer_notice(store_key: str, store_slug: str, text: str) -> dict[str, str]:
    """Send one heads-up on every channel that is switched on and configured for this store.

    Each channel obeys its own switch the way the finished-call sinks do (``alerts.slack`` /
    ``alerts.email``); Pushover has no other switch than the ``call.sms_on_transfer`` master the
    caller already checked. Returns ``{channel: "sent" | "skipped" | "failed: why"}``. Never raises,
    and one channel failing never stops the next."""
    results: dict[str, str] = {}

    def attempt(channel: str, ready: bool, send) -> None:
        if not ready:
            results[channel] = "skipped"
            return
        try:
            send()
            results[channel] = "sent"
        except Exception as exc:  # noqa: BLE001 — a dead channel must not stop the others
            results[channel] = f"failed: {type(exc).__name__}"
            logger.warning("transfer heads-up via %s failed: %s", channel, exc)

    pushover = PushoverSink()
    attempt("pushover", pushover.enabled(store_key), lambda: pushover.deliver(store_key, text))

    slack_url = getattr(settings, "SLACK_WEBHOOK_URL", "")
    attempt(
        "slack",
        bool(slack_url and capabilities.is_enabled("alerts.slack")),
        lambda: _post(slack_url, json.dumps({"text": _slack_escape(text)}).encode(), "application/json"),
    )

    recipients = _recipients_for(store_slug)
    attempt(
        "email",
        bool(recipients and capabilities.is_enabled("alerts.email")),
        lambda: EmailMultiAlternatives(
            subject=f"[Happy Time voice] {C.spoken_store(store_slug)} — transfer incoming",
            body=text,
            from_email=getattr(settings, "LEAD_EMAIL_FROM", "bot@happytimeweed.com"),
            to=recipients,
        ).send(fail_silently=False),
    )
    return results


# Sessions that are never a real caller: the eval harness (``eval-``), the staff console
# (``pg-``), the phone simulator (``sim-``), the conversation test harness (``convo-``) and the
# live tool smoke (``text-smoke``). On 2026-09-18 an eval run emailed the store three URGENT
# escalations with no transcript in them. A test must never page a human.
_TEST_SESSION_PREFIXES = ("eval-", "pg-", "sim-", "convo-", "text-smoke")


def _suppression_reason(voice_call) -> str:
    """Why this call must not reach any outbound sink — "" when it is a real call."""
    import os

    call_id = str(getattr(voice_call, "call_id", "") or "")
    if call_id.startswith(_TEST_SESSION_PREFIXES):
        return "test session (eval/playground/simulator) — never alerts staff"
    if os.environ.get("HHT_ALERT_SINKS", "").strip().lower() in ("off", "0", "false", "dry-run"):
        return "HHT_ALERT_SINKS=off (dry run)"
    return ""


def dispatch(voice_call) -> dict[str, str]:
    """Fire every sink independently for one VoiceCall, idempotent per ``(voice_call, sink)``.

    Records one ``AlertDelivery`` row per sink; a row already ``success`` short-circuits (so a
    re-delivered eocr never re-sends). Returns ``{sink_name: status}``. Never raises — a sink
    failure is logged + recorded ``failed``, never fatal (the durable record is already safe)."""
    from crm.models import AlertDelivery

    results: dict[str, str] = {}
    suppressed = _suppression_reason(voice_call)
    if suppressed:
        logger.info("staff alert suppressed for %s: %s", voice_call.call_id, suppressed)
    capped: list[bool] = []  # decided once per dispatch, only when an outbound sink would fire

    def over_cap() -> bool:
        if not capped:
            capped.append(_over_text_alert_cap(voice_call))
            if capped[0]:
                logger.warning(
                    "text-chat alert cap reached for store %s; %s logged on the dashboard only",
                    voice_call.store or "-", voice_call.call_id,
                )
        return capped[0]

    for sink in SINKS:
        delivery, _ = AlertDelivery.objects.get_or_create(voice_call=voice_call, sink=sink.name)
        if delivery.status == "success":
            results[sink.name] = "success"  # idempotent: already delivered
            continue
        delivery.attempts += 1
        if suppressed:
            delivery.status = "skipped"
            delivery.last_error = suppressed
        elif not sink.enabled(voice_call):
            delivery.status = "skipped"
            delivery.last_error = "disabled or not configured"
        elif sink.name != DBSink.name and over_cap():  # the db "sink" is the record, not an alert
            delivery.status = "skipped"
            delivery.last_error = "text_alert_cap"
        else:
            try:
                sink.deliver(voice_call)
                delivery.status = "success"
                delivery.last_error = ""
            except Exception as exc:  # noqa: BLE001
                delivery.status = "failed"
                delivery.last_error = str(exc)[:500]
                logger.warning("voice sink %s failed: %s", sink.name, exc)
        delivery.save()
        results[sink.name] = delivery.status
    return results


def send_staff_alert(subject: str, markdown_table: str) -> None:
    """A one-off staff alert not tied to a ``VoiceCall`` (e.g. the nightly store-facts drift
    check) — the same two live sinks (email to ``STAFF_ALERT_EMAIL``, n8n) ``EmailSink``/
    ``N8nSink`` use, without the per-call ``AlertDelivery`` idempotency ledger (a nightly task is
    already idempotent by construction — it only fires once a night and only on drift). Never
    raises — each sink's failure is logged, not fatal."""
    recipients = _recipients_for("")
    if recipients and capabilities.is_enabled("alerts.email"):
        try:
            EmailMultiAlternatives(
                subject=subject[:120],
                body=markdown_table,
                from_email=getattr(settings, "LEAD_EMAIL_FROM", "bot@happytimeweed.com"),
                to=recipients,
            ).send(fail_silently=False)
        except Exception:  # noqa: BLE001 — an alert must never crash the beat worker
            logger.warning("send_staff_alert: email failed", exc_info=True)

    n8n_url = getattr(settings, "N8N_WEBHOOK_URL", "")
    if n8n_url and capabilities.is_enabled("alerts.n8n"):
        try:
            data = json.dumps({"event": "store_facts_drift", "subject": subject, "table": markdown_table}).encode()
            status = post_webhook(n8n_url, data)
            if status >= 300:
                raise RuntimeError(f"n8n HTTP {status}")
        except Exception:  # noqa: BLE001
            logger.warning("send_staff_alert: n8n failed", exc_info=True)
