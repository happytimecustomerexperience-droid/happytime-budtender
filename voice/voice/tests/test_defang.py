"""W9-4: a visitor's words reach staff as inert text. One shared ``crm.sinks.defang`` turns
``scheme://`` into ``hxxp://`` and the dot before any 2-24 letter TLD into ``[.]``, and every
visitor-derived string in the staff email (body, HTML, subject), the Slack post and the transfer note
(Pushover, Slack, email) goes through it. The transfer note used to strip URLs for a fixed TLD list
only, so ``.xyz`` and ``.ru`` links went out live."""

from __future__ import annotations

import json
import re

import pytest
from django.core import mail

from crm import sinks, transfer_notice
from crm.sinks import defang
from voice.models import VoiceCall, VoiceTurn


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pay at evil.xyz now", "pay at evil[.]xyz now"),
        ("scam.ru", "scam[.]ru"),
        ("https://scam.ru/pay?a=1", "hxxp://scam[.]ru/pay?a=1"),
        ("HTTPS://EVIL.COM", "hxxp://EVIL[.]COM"),
        ("ftp://files.io/x", "hxxp://files[.]io/x"),
        ("javascript://x", "hxxp://x"),
        ("www.evil.co/path", "www[.]evil[.]co/path"),
        ("bare evil.co/path here", "bare evil[.]co/path here"),
        ("a.b.c.example.org", "a.b.c[.]example[.]org"),  # single-letter labels are not a TLD
        ("me@evil.test", "me@evil[.]test"),
        # internationalised names: Cyrillic, Latin with a diacritic, CJK, and the ideographic dot
        ("пример.рф", "пример[.]рф"),
        ("bücher.example", "bücher[.]example"),
        ("例え.jp", "例え[.]jp"),
        ("evil。com", "evil[.]com"),
        ("evil．com", "evil[.]com"),
        ("xn--bcher-kva.de", "xn--bcher-kva[.]de"),
        # ordinary words, numbers and punctuation are left alone
        ("3.5 grams, $20.00, 1.5oz, v2.10, e.g. one. Next.", "3.5 grams, $20.00, 1.5oz, v2.10, e.g. one. Next."),
        ("Wants a refund. Thanks!", "Wants a refund. Thanks!"),
        ("", ""),
        (None, ""),
    ],
)
def test_defang(text, expected):
    assert defang(text) == expected


@pytest.mark.parametrize("text", ["https://evil.co/x.php", "go to evil.xyz or пример.рф", "plain text.", "a.b"])
def test_defang_is_idempotent(text):
    assert defang(defang(text)) == defang(text)


def test_a_tld_longer_than_24_letters_is_not_a_domain():
    assert defang("a." + "x" * 25) == "a." + "x" * 25
    assert defang("a." + "x" * 24) == "a[.]" + "x" * 24


# ── the staff email and the Slack post ───────────────────────────────────────────────────────────

HOSTILE = "refund or see https://scam.ru/pay, www.evil.xyz and evil.co/path, пример.рф"


def _live_link(blob: str) -> list[str]:
    """What a mail client or Slack would still turn into a link."""
    return re.findall(r"https?://|\b[\w-]+\.[a-z]{2,24}\b", blob.lower())


@pytest.fixture
def alert_env(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.STAFF_ALERT_EMAIL = "staff@happytimeweed.com"
    settings.SLACK_WEBHOOK_URL = ""
    settings.N8N_WEBHOOK_URL = ""


@pytest.mark.django_db
def test_staff_email_has_no_live_link_in_body_html_or_subject(alert_env):
    vc = VoiceCall.objects.create(
        call_id="s-mfx0000-linkmail", store="yakima", outcome="escalation",
        reason="dispute re evil.xyz", ai_summary=HOSTILE,
    )
    VoiceTurn.objects.create(call=vc, seq=1, role="user", text=HOSTILE)
    VoiceTurn.objects.create(call=vc, seq=2, role="tool", text="", tool_name="notify.evil.xyz")

    assert sinks.dispatch(vc)["email"] == "success"
    [msg] = mail.outbox
    [html] = [body for body, mime in msg.alternatives if mime == "text/html"]
    for part in (msg.subject, msg.body, html):
        assert _live_link(part) == [], part
    assert "hxxp://scam[.]ru/pay" in msg.body and "www[.]evil[.]xyz" in msg.body and "evil[.]co/path" in msg.body
    assert "hxxp://scam[.]ru/pay" in html and "(reason: dispute re evil[.]xyz)" in msg.body


@pytest.mark.django_db
def test_the_transcript_fallback_is_defanged_too(alert_env):
    only_transcript = VoiceCall.objects.create(
        call_id="s-mfx0001-transcr", store="yakima", outcome="escalation", transcript="visit evil.xyz"
    )
    sinks.dispatch(only_transcript)
    assert "evil[.]xyz" in mail.outbox[-1].body and "evil.xyz" not in mail.outbox[-1].body


@pytest.mark.django_db
def test_slack_post_has_no_live_link(alert_env, settings, monkeypatch):
    settings.SLACK_WEBHOOK_URL = "https://hooks.slack.test/alert"
    sent = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        sinks.urllib.request, "urlopen", lambda req, timeout: sent.update(p=json.loads(req.data.decode())) or _Resp()
    )
    vc = VoiceCall.objects.create(
        call_id="s-mfx0002-slacklnk", store="yakima", outcome="escalation", reason="dispute", ai_summary=HOSTILE
    )

    assert sinks.dispatch(vc)["slack"] == "success"
    assert _live_link(sent["p"]["text"]) == []
    assert "hxxp://scam[.]ru/pay" in sent["p"]["text"]


@pytest.mark.django_db
def test_the_n8n_payload_is_data_for_automation_and_is_not_defanged(alert_env, settings, monkeypatch):
    """n8n gets the machine-readable summary; only the channels a person reads are defanged."""
    settings.N8N_WEBHOOK_URL = "https://n8n.example/webhook/abc"
    sent = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        sinks._OPENER, "open", lambda req, timeout=10: sent.update(p=json.loads(req.data.decode())) or _Resp()
    )
    vc = VoiceCall.objects.create(
        call_id="s-mfx0003-n8nlink", store="yakima", outcome="escalation", ai_summary="see evil.xyz"
    )
    assert sinks.dispatch(vc)["n8n"] == "success"
    assert sent["p"]["summary"] == "see evil.xyz"


# ── the transfer note: Pushover, Slack and email carry the same composed line ────────────────────


def _forwarding(summary):
    return {
        "type": "status-update", "status": "forwarding", "call": {"id": "9f3a7c21-0000-4000-8000-000000000001"},
        "customer": {"number": "+15095551234"}, "destination": {"type": "number", "number": "+15095711106"},
        "summary": summary, "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": "transferCall"}}]}],
        "timestamp": 1760000000000,
    }


@pytest.mark.parametrize(
    ("summary", "must_have", "must_not"),
    [
        ("wants a refund, see evil.xyz", "evil[.]xyz", "evil.xyz"),
        ("report at scam.ru/pay now", "scam[.]ru", "scam.ru"),
        ("see ftp://x.example.xyz/a", "hxxp://x[.]example[.]xyz/a", "ftp://"),
        ("wants a refund, see пример.рф", "Wants: wants a refund, see [.]", "пример"),  # non-ASCII is dropped
    ],
)
@pytest.mark.django_db
def test_the_transfer_note_defangs_every_tld_not_just_a_fixed_list(summary, must_have, must_not):
    text = transfer_notice.compose(_forwarding(summary), "yakima", "yakima")
    assert must_have in text and must_not not in text
    assert text.isascii() and len(text) <= transfer_notice.MAX_LEN


@pytest.mark.parametrize("summary", ["see https://evil.example/x", "see www.bad.test", "see shop.com/deal"])
def test_urls_the_old_list_covered_are_still_removed_outright(summary):
    assert "evil" not in transfer_notice.clean_summary(summary) and "bad" not in transfer_notice.clean_summary(summary)
    assert "shop" not in transfer_notice.clean_summary(summary)
