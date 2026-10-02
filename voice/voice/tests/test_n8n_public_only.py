"""W9-5: the n8n webhook POST resolves the host at call time and refuses unless EVERY address is public,
connects to the very address it checked, and never follows a redirect.

The credentials editor only checks the host NAME on save, and a name proves nothing about where it
points: ``169.254.169.254.nip.io`` is a public-looking name for the cloud metadata address. Offline —
DNS and sockets are replaced, nothing here opens a connection."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from types import SimpleNamespace

import pytest

from crm import sinks
from dashboard import credentials

PUBLIC = "93.184.216.34"
NIP = "https://169.254.169.254.nip.io/webhook/abc"


def _dns(monkeypatch, *addresses):
    """Make every lookup answer ``addresses`` (IPv4 or IPv6 strings)."""
    monkeypatch.setattr(
        sinks.socket, "getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", (a, port)) for a in addresses],
    )


@pytest.fixture
def no_connections(monkeypatch):
    """Any attempt to open a socket is a test failure: a refused host must never be dialled."""
    dialled = []
    monkeypatch.setattr(sinks.socket, "create_connection", lambda *a, **k: dialled.append(a) or pytest.fail(f"dialled {a}"))
    return dialled


@pytest.mark.parametrize(
    "addresses",
    [
        ["169.254.169.254"],  # cloud metadata
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["172.16.0.9"],
        ["192.168.1.1"],
        ["100.64.0.1"],  # carrier-grade NAT
        ["0.0.0.0"],
        ["::1"],
        ["fe80::1%eth0"],
        ["fd00::5"],
        ["::ffff:127.0.0.1"],  # an IPv4 loopback in IPv6 clothes
        [PUBLIC, "10.0.0.1"],  # one private address among public ones is enough to refuse
        [],
    ],
)
def test_a_host_that_resolves_anywhere_non_public_is_refused(monkeypatch, addresses):
    _dns(monkeypatch, *addresses)
    with pytest.raises(OSError, match="refusing"):
        sinks._public_address("n8n.example.com", 443)


def test_a_host_that_resolves_only_to_public_addresses_is_pinned_to_the_first(monkeypatch):
    _dns(monkeypatch, PUBLIC, "2606:4700:4700::1111", PUBLIC)
    assert sinks._public_address("n8n.example.com", 443) == PUBLIC


def test_the_connection_dials_the_address_it_checked_and_names_the_original_host_for_tls(monkeypatch):
    """No second lookup between the check and the connect (DNS rebinding), and the TLS name is still
    the host the owner configured."""
    _dns(monkeypatch, PUBLIC)
    dialled = {}
    monkeypatch.setattr(sinks.socket, "create_connection", lambda addr, timeout=None: dialled.update(addr=addr) or "raw")
    conn = sinks._PublicHTTPSConnection("n8n.example.com", 443, timeout=7)
    conn._context = SimpleNamespace(wrap_socket=lambda sock, server_hostname: (sock, server_hostname))

    conn.connect()

    assert dialled["addr"] == (PUBLIC, 443)
    assert conn.sock == ("raw", "n8n.example.com")


def test_post_webhook_refuses_the_nip_io_metadata_trick_without_dialling(monkeypatch, no_connections):
    # the save-time name check lets it through; only the call-time resolution catches it
    assert credentials._ok_n8n(NIP) is True
    _dns(monkeypatch, "169.254.169.254")

    with pytest.raises(urllib.error.URLError, match="refusing"):
        sinks.post_webhook(NIP, b"{}")
    assert no_connections == []


@pytest.mark.django_db
def test_every_n8n_caller_goes_through_the_guard(settings, monkeypatch, no_connections):
    from django.core import mail

    from voice.models import VoiceCall
    from voice.tools import n8n

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.STAFF_ALERT_EMAIL = ""
    settings.N8N_WEBHOOK_URL = NIP
    _dns(monkeypatch, "169.254.169.254")

    # the bot tool: a soft failure, never a crash
    assert n8n.notify_n8n({"event_type": "menu_link"}, {"store": "yakima"}) == {"ok": False, "reason": "n8n unreachable"}
    # the nightly drift alert: logged, never raised
    sinks.send_staff_alert("drift", "| a |")
    # the per-call sink: the delivery fails (and is recorded), it does not reach the address
    vc = VoiceCall(call_id="c-n8n-guard", store="yakima", outcome="suggested")
    with pytest.raises(urllib.error.URLError, match="refusing"):
        sinks.N8nSink().deliver(vc)
    assert no_connections == [] and mail.outbox == []


# ── redirects ────────────────────────────────────────────────────────────────────────────────────


def _redirect_response(url):
    headers = Message()
    headers["Location"] = "https://169.254.169.254/latest/meta-data/"
    resp = urllib.response.addinfourl(io.BytesIO(b""), headers, url, 302)
    resp.msg = "Found"
    return resp


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_is_a_failure_and_is_never_followed(monkeypatch, code):
    seen = []

    def https_open(self, req):
        seen.append(req.full_url)
        resp = _redirect_response(req.full_url)
        resp.code = code
        return resp

    monkeypatch.setattr(sinks._PublicHTTPSHandler, "https_open", https_open)

    with pytest.raises(urllib.error.HTTPError) as err:
        sinks.post_webhook("https://n8n.example.com/webhook/abc", b"{}")

    assert err.value.code == code
    assert seen == ["https://n8n.example.com/webhook/abc"], "the Location was never requested"


def test_the_opener_has_no_redirect_handler_and_only_https():
    handlers = sinks._OPENER.handlers
    assert not [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert not [h for h in handlers if isinstance(h, (urllib.request.HTTPHandler, urllib.request.FileHandler))]


@pytest.mark.parametrize("url", ["http://n8n.example.com/x", "file:///etc/passwd", "ftp://n8n.example.com/x"])
def test_only_https_urls_can_be_posted(url, no_connections):
    with pytest.raises(urllib.error.URLError):
        sinks.post_webhook(url, b"{}")
    assert no_connections == []


def test_a_good_post_returns_the_status(monkeypatch):
    class _Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}
    monkeypatch.setattr(
        sinks._OPENER, "open",
        lambda req, timeout=None: seen.update(url=req.full_url, body=json.loads(req.data), t=timeout, ct=req.get_header("Content-type"))
        or _Resp(),
    )
    assert sinks.post_webhook("https://n8n.example.com/h", b'{"a": 1}', timeout=3) == 204
    assert seen == {"url": "https://n8n.example.com/h", "body": {"a": 1}, "t": 3, "ct": "application/json"}
