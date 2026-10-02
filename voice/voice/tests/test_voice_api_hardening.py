"""W5b fixes 4-6 — /api/voice/chat: session-id validation, a website-only Bearer, rate limiting.

4. The session id becomes ``VoiceCall.call_id`` (max 64): an over-long one failed the DB write
   silently every turn, and a test prefix silenced staff alerts.
5. The inbound Bearer was the same secret that opens budtender's customer DB.
6. The limiter keyed on ``session_token:ip`` (rotate the id, dodge the limit), parsed the body
   before auth, and had no ceiling across IPs.
"""

from __future__ import annotations

import json

import pytest

from voice import api

WEBSITE_TOKEN = "website-only-token"
BACKEND_TOKEN = "backend-db-token"


@pytest.fixture(autouse=True)
def _tokens(settings, monkeypatch):
    settings.HHT_VOICE_TOKEN = WEBSITE_TOKEN
    settings.HHT_BACKEND_TOKEN = BACKEND_TOKEN
    settings.HHT_VOICE_RATE_LIMIT = "60"
    settings.HHT_VOICE_GLOBAL_RATE_LIMIT = "600"
    seen = []
    monkeypatch.setattr(api, "answer_text_chat", lambda data: seen.append(data) or {"ok": True, "answer": "hi"})
    return seen


def _chat(client, body=None, *, token=WEBSITE_TOKEN, ip=None, raw=None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    if ip:
        headers["HTTP_X_HHT_CLIENT_IP"] = ip
    data = raw if raw is not None else json.dumps(body or {"message": "hi"})
    return client.post("/api/voice/chat", data=data, content_type="application/json", **headers)


# ── 4. session id ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "session",
    ["s-mfx1abc2-k3j9x0qz", "s-a-b", "S_long-Token_123", "a" * 64],
)
def test_website_shaped_session_ids_are_accepted(client, session):
    assert _chat(client, {"message": "hi", "session_token": session}).status_code == 200


@pytest.mark.parametrize(
    "session",
    [
        "a" * 65,  # past VoiceCall.call_id max_length — the write failed silently
        "s-" + "x" * 98,
        "s-abc def",
        "s-abc;drop",
        "s-abécd",
        "pg-123456789abc",  # every test prefix silences staff alerts
        "eval-123456789abc",
        "sim-123456789abc",
        "convo-123456789abc",
        "text-smoke-test",
    ],
)
def test_bad_or_test_prefixed_session_ids_are_rejected(client, _tokens, session):
    resp = _chat(client, {"message": "hi", "session_token": session})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid session_token"
    assert _tokens == [], "a rejected session id never reaches the brain"


def test_session_id_alias_is_validated_too_and_absent_is_allowed(client, _tokens):
    assert _chat(client, {"message": "hi", "session_id": "pg-abcdef"}).status_code == 400
    assert _chat(client, {"message": "hi"}).status_code == 200, "root's calls send no session id"


# ── 5. tokens ────────────────────────────────────────────────────────────────

def test_website_token_and_backend_token_both_work_nothing_else(client, settings):
    assert _chat(client, token=WEBSITE_TOKEN).status_code == 200
    assert _chat(client, token=BACKEND_TOKEN).status_code == 200, "root's server-to-server calls"
    assert _chat(client, token="guess").status_code == 401
    assert _chat(client, token=None).status_code == 401
    assert _chat(client, token="café").status_code == 401, "a non-ASCII header is a 401, not a 500"

    settings.HHT_VOICE_TOKEN = ""
    assert _chat(client, token=WEBSITE_TOKEN).status_code == 401, "an unset website token opens nothing"
    assert _chat(client, token=BACKEND_TOKEN).status_code == 200


# ── 6. rate limiting + body cap ──────────────────────────────────────────────

def test_rotating_session_ids_from_one_ip_hit_the_per_ip_limit(client, settings):
    settings.HHT_VOICE_RATE_LIMIT = "3"
    codes = [
        _chat(client, {"message": "hi", "session_token": f"s-rot{i:03d}-abcdefgh"}, ip="203.0.113.7").status_code
        for i in range(5)
    ]
    assert codes == [200, 200, 200, 429, 429]
    assert _chat(client, ip="198.51.100.9").status_code == 200, "another visitor has their own bucket"


def test_the_global_ceiling_trips_across_ips(client, settings):
    settings.HHT_VOICE_RATE_LIMIT = "100"
    settings.HHT_VOICE_GLOBAL_RATE_LIMIT = "4"
    codes = [_chat(client, ip=f"203.0.113.{i}").status_code for i in range(6)]
    assert codes == [200, 200, 200, 200, 429, 429]


def test_a_junk_client_ip_header_falls_back_to_the_proxy_view(client, settings):
    settings.HHT_VOICE_RATE_LIMIT = "2"
    codes = [_chat(client, ip=f"not-an-ip-{i}").status_code for i in range(3)]
    assert codes == [200, 200, 429], "a garbage header must not mint a fresh bucket per request"


def test_an_unauthenticated_10mb_body_is_refused_without_being_parsed(client, monkeypatch):
    parsed = []
    monkeypatch.setattr(api, "_body", lambda request: parsed.append(1) or {})
    big = "x" * (10 * 1024 * 1024)

    assert _chat(client, raw=big, token=None).status_code == 401
    assert _chat(client, raw=big).status_code == 413, "authorized but oversized: refused before parsing"
    assert parsed == []
    assert _chat(client, {"message": "hi"}).status_code == 200 and parsed == [1]  # control
