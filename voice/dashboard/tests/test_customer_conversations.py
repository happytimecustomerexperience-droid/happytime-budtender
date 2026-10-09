"""Customer page conversations panel (T4): linking, the chat+call list, per-conversation and
"Summarize all" summaries, and the clear-memory button. Offline: budtender and Gemini are faked."""

from __future__ import annotations

import json
import logging

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from core.services import gemini as gemini_mod
from crm.models import ConversationSummary, CustomerProfile, CustomerSummary
from voice import budtender_client
from voice.models import VoiceCall

SECRET_LINE = "ZEBRA-TRANSCRIPT-LINE-9931"


class FakeBT:
    """Stands in for ``BudtenderClient``; every method records its call."""

    def __init__(self, *, name_match=None, sessions=None, call_ids=None, transcripts=None):
        self.name_match = {"count": 1, "id": 77} if name_match is None else name_match
        self.sessions = sessions if sessions is not None else []
        self.call_ids = call_ids if call_ids is not None else []
        self.transcripts = transcripts or {}
        self.clear_result = {"status": "cleared", "sessions_cleared": 2}
        self.calls: list[tuple] = []
        self.chats_ok = True

    def get_customer(self, **kw):
        return None

    def customer_name_match(self, name):
        self.calls.append(("name_match", name))
        return self.name_match or None

    def customer_chat_sessions(self, customer_id, *, limit=100):
        self.calls.append(("sessions", customer_id))
        return {"ok": self.chats_ok, "sessions": self.sessions, "total": len(self.sessions)}

    def customer_chat_session(self, customer_id, session_id, *, message_limit=500):
        self.calls.append(("session", customer_id, session_id))
        return self.transcripts.get(int(session_id))

    def customer_call_ids(self, customer_id):
        self.calls.append(("call_ids", customer_id))
        return {"ok": True, "call_ids": list(self.call_ids)}

    def memory_clear(self, customer_id, actor):
        self.calls.append(("clear", customer_id, actor))
        return self.clear_result

    def names(self):
        return [c[0] for c in self.calls]


def chat_meta(sid, count=4, channel="chat", when="2026-10-02T10:00:00+00:00"):
    return {"id": sid, "channel": channel, "location_slug": "yakima", "primary_intent": "browse",
            "last_active_at": when, "started_at": when, "message_count": count}


def chat_body(sid, lines=None):
    msgs = [{"role": r, "content": c} for r, c in (lines or [("user", f"{SECRET_LINE} hello"), ("assistant", "hi")])]
    return {"id": sid, "channel": "chat", "messages": msgs, "message_count": len(msgs)}


@pytest.fixture
def staff(db):
    return User.objects.create_user("boss", password="x", is_staff=True)


@pytest.fixture
def client_staff(client, staff):
    client.force_login(staff)
    return client


@pytest.fixture
def jamie(db):
    return CustomerProfile.objects.create(customer_key="cust-j", name="Jamie Rivera", orders=3)


@pytest.fixture
def a_call(db):
    call = VoiceCall.objects.create(
        call_id="call-1", store="mount-vernon", outcome="suggested", duration_s=187,
        transcript=f"AI: hi\nUser: {SECRET_LINE} gummies please\nAI: sure")
    return call


@pytest.fixture
def bt(monkeypatch):
    fake = FakeBT()
    monkeypatch.setattr(budtender_client, "budtender", lambda: fake)
    return fake


@pytest.fixture
def gem(monkeypatch):
    """Fake Gemini: queued replies, every call recorded (contents, system, kwargs)."""

    class G:
        def __init__(self):
            self.replies = []
            self.calls = []

        def __call__(self, contents, *, model, system_instruction=None, **kw):
            self.calls.append({"contents": contents, "system": system_instruction, "kw": kw, "model": model})
            if not self.replies:
                return gemini_mod.GeminiResponse(text="Wanted gummies and left happy.", model=model)
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return gemini_mod.GeminiResponse(text=reply, model=model)

    g = G()
    monkeypatch.setattr(gemini_mod, "generate", g)
    return g


def page(client, pk):
    return client.get(reverse("dash-customer-detail", args=[pk]))


def toast(resp):
    return json.loads(resp["HX-Trigger"])["toast"]


# ── linking ───────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_unique_exact_name_links_and_remembers(client_staff, bt, jamie):
    bt.sessions = [chat_meta(5)]
    resp = page(client_staff, jamie.pk)
    assert b"conv-chat-5" in resp.content
    jamie.refresh_from_db()
    assert (jamie.budtender_customer_id, jamie.budtender_link) == (77, "name_unique")
    assert ("sessions", 77) in bt.calls


@pytest.mark.django_db
def test_ambiguous_live_name_stays_unlinked_and_says_so(client_staff, bt, jamie):
    bt.name_match = {"count": 2, "id": None}
    resp = page(client_staff, jamie.pk)
    assert "Not linked to a live customer record — conversations unavailable." in resp.content.decode()
    assert b"conv-summarize-all" not in resp.content and b"conv-clear-memory" not in resp.content
    assert "sessions" not in bt.names() and "call_ids" not in bt.names()
    jamie.refresh_from_db()
    assert jamie.budtender_customer_id is None and jamie.budtender_link == ""


@pytest.mark.django_db
def test_no_live_match_stays_unlinked(client_staff, bt, jamie):
    bt.name_match = {"count": 0, "id": None}
    assert b"conv-unlinked" in page(client_staff, jamie.pk).content


@pytest.mark.django_db
def test_two_imported_rows_with_the_same_name_never_link(client_staff, bt, jamie):
    CustomerProfile.objects.create(customer_key="cust-j2", name="jamie   RIVERA")
    resp = page(client_staff, jamie.pk)
    assert b"conv-unlinked" in resp.content
    assert "name_match" not in bt.names()  # decided locally; budtender is not even asked


@pytest.mark.django_db
def test_single_word_or_substring_name_never_links(client_staff, bt):
    first_only = CustomerProfile.objects.create(customer_key="cust-s", name="Jamie")
    CustomerProfile.objects.create(customer_key="cust-s2", name="Jamie Rivera Smith")
    resp = page(client_staff, first_only.pk)
    assert b"conv-unlinked" in resp.content
    assert "name_match" not in bt.names()
    first_only.refresh_from_db()
    assert first_only.budtender_customer_id is None


@pytest.mark.django_db
def test_stored_phone_or_manual_link_is_used_without_asking_by_name(client_staff, bt, jamie):
    CustomerProfile.objects.filter(pk=jamie.pk).update(budtender_customer_id=91, budtender_link="phone")
    bt.sessions = [chat_meta(8)]
    resp = page(client_staff, jamie.pk)
    assert b"conv-chat-8" in resp.content
    assert "name_match" not in bt.names() and ("sessions", 91) in bt.calls


@pytest.mark.django_db
def test_name_link_is_dropped_when_the_name_stops_being_unique(client_staff, bt, jamie):
    CustomerProfile.objects.filter(pk=jamie.pk).update(budtender_customer_id=77, budtender_link="name_unique")
    bt.name_match = {"count": 2, "id": None}
    assert b"conv-unlinked" in page(client_staff, jamie.pk).content
    jamie.refresh_from_db()
    assert jamie.budtender_customer_id is None


@pytest.mark.django_db
def test_budtender_down_neither_links_nor_unlinks(client_staff, bt, jamie):
    bt.name_match = {}  # FakeBT turns a falsy answer into None = unreachable
    assert b"conv-unlinked" in page(client_staff, jamie.pk).content
    CustomerProfile.objects.filter(pk=jamie.pk).update(budtender_customer_id=77, budtender_link="name_unique")
    bt.sessions = [chat_meta(5)]
    assert b"conv-chat-5" in page(client_staff, jamie.pk).content  # stored link kept while unknown
    jamie.refresh_from_db()
    assert jamie.budtender_customer_id == 77


@pytest.mark.django_db
def test_budtender_only_customer_is_keyed_by_its_own_id(client_staff, bt, monkeypatch):
    live = {"id": 4242, "name": "Walk In", "total_orders": 1}
    bt.get_customer = lambda **kw: live
    bt.sessions = [chat_meta(3)]
    resp = page(client_staff, 4242)
    assert b"conv-chat-3" in resp.content
    assert ("sessions", 4242) in bt.calls and "name_match" not in bt.names()


# ── the list ──────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_list_merges_chats_and_calls_and_leaves_out_voice_sessions(client_staff, bt, jamie, a_call):
    bt.sessions = [chat_meta(5, 4), chat_meta(6, 2, channel="voice"), chat_meta(7, 0)]
    bt.call_ids = ["call-1", "call-gone"]
    html = page(client_staff, jamie.pk).content.decode()
    assert "conv-chat-5" in html and "conv-call-call-1" in html
    assert "conv-chat-6" not in html  # a voice-channel session is the call, not a chat
    assert "conv-chat-7" not in html  # nothing was said
    assert "4 messages" in html and "3:07" in html and "mount-vernon" in html
    assert reverse("dash-call-detail", args=[a_call.pk]) in html
    assert reverse("dash-chat-detail") + "?id=5" in html
    assert "1 phone call on this customer have no stored call record" in html
    assert SECRET_LINE not in html  # transcripts are not dumped into the page


@pytest.mark.django_db
def test_a_down_source_is_reported_not_called_empty(client_staff, bt, jamie):
    bt.chats_ok = False
    html = page(client_staff, jamie.pk).content.decode()
    assert "conv-chats-down" in html and "conv-none" not in html


# ── per-conversation summaries ────────────────────────────────────────────────
def summary_url(pk, kind, ref):
    return reverse("dash-customer-conv-summary", args=[pk, kind, ref])


@pytest.fixture
def linked(jamie, bt):
    CustomerProfile.objects.filter(pk=jamie.pk).update(budtender_customer_id=77, budtender_link="manual")
    bt.sessions = [chat_meta(5, 2)]
    bt.transcripts = {5: chat_body(5)}
    return jamie


@pytest.mark.django_db
def test_chat_summary_generated_once_then_cached(client_staff, bt, gem, linked):
    gem.replies = ["Asked about gummies."]
    r1 = client_staff.post(summary_url(linked.pk, "chat", "5"))
    assert r1.status_code == 200 and b"Asked about gummies." in r1.content
    assert toast(r1)["type"] == "success"
    row = ConversationSummary.objects.get(kind="chat", ref="5")
    assert (row.text, row.message_count) == ("Asked about gummies.", 2)
    r2 = client_staff.post(summary_url(linked.pk, "chat", "5"))
    assert toast(r2)["type"] == "info" and len(gem.calls) == 1  # cached: no second model call
    assert bt.names().count("session") == 1  # and the transcript was not even re-fetched


@pytest.mark.django_db
def test_regenerated_when_the_message_count_changes_and_on_regenerate(client_staff, bt, gem, linked):
    gem.replies = ["First take.", "Second take.", "Third take."]
    client_staff.post(summary_url(linked.pk, "chat", "5"))
    bt.sessions = [chat_meta(5, 6)]  # the chat grew
    resp = client_staff.post(summary_url(linked.pk, "chat", "5"))
    assert ConversationSummary.objects.get(kind="chat", ref="5").text == "Second take."
    assert ConversationSummary.objects.get(kind="chat", ref="5").message_count == 6
    assert len(gem.calls) == 2 and toast(resp)["type"] == "success"
    client_staff.post(summary_url(linked.pk, "chat", "5"), {"regenerate": "1"})
    assert ConversationSummary.objects.get(kind="chat", ref="5").text == "Third take."
    assert len(gem.calls) == 3


@pytest.mark.django_db
def test_stale_summary_is_marked_on_the_page(client_staff, bt, gem, linked):
    ConversationSummary.objects.create(kind="chat", ref="5", text="old text", message_count=1)
    html = page(client_staff, linked.pk).content.decode()
    assert "old text" in html and "changed since this summary" in html and "Update summary" in html


@pytest.mark.django_db
def test_call_summary_uses_the_stored_transcript(client_staff, bt, gem, linked, a_call):
    bt.call_ids = ["call-1"]
    gem.replies = ["Wanted gummies."]
    resp = client_staff.post(summary_url(linked.pk, "call", "call-1"))
    assert resp.status_code == 200
    assert SECRET_LINE in gem.calls[0]["contents"]
    assert ConversationSummary.objects.get(kind="call", ref="call-1").message_count == 3


@pytest.mark.django_db
def test_gemini_failure_toasts_an_error_and_keeps_the_old_summary(client_staff, bt, gem, linked, caplog):
    ConversationSummary.objects.create(kind="chat", ref="5", text="keep me", message_count=1)
    gem.replies = [RuntimeError(f"upstream echoed {SECRET_LINE}")]
    with caplog.at_level(logging.DEBUG):
        resp = client_staff.post(summary_url(linked.pk, "chat", "5"))
    assert resp.status_code == 204 and toast(resp)["type"] == "error"
    assert ConversationSummary.objects.get(kind="chat", ref="5").text == "keep me"
    assert SECRET_LINE not in caplog.text


@pytest.mark.django_db
def test_output_with_a_price_or_contact_is_discarded_not_stored(client_staff, bt, gem, linked):
    gem.replies = ["Call 509-555-0199 or mail a@b.com, it costs $40."]
    resp = client_staff.post(summary_url(linked.pk, "chat", "5"))
    assert resp.status_code == 204 and toast(resp)["type"] == "error"
    assert not ConversationSummary.objects.exists()


@pytest.mark.django_db
def test_a_conversation_of_someone_else_cannot_be_summarized(client_staff, bt, gem, linked):
    resp = client_staff.post(summary_url(linked.pk, "chat", "999"))
    assert resp.status_code == 204 and toast(resp)["type"] == "error"
    assert gem.calls == []
    assert client_staff.post(summary_url(linked.pk, "bogus", "5")).status_code == 404


@pytest.mark.django_db
def test_unlinked_customer_cannot_be_summarized(client_staff, bt, gem, jamie):
    bt.name_match = {"count": 2, "id": None}
    resp = client_staff.post(summary_url(jamie.pk, "chat", "5"))
    assert resp.status_code == 204 and toast(resp)["type"] == "error" and gem.calls == []


# ── summarize all ─────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_summarize_all_is_map_reduce_over_the_conversation_summaries(client_staff, bt, gem, linked, a_call):
    bt.call_ids = ["call-1"]
    ConversationSummary.objects.create(kind="chat", ref="5", text="Cached chat note.", message_count=2)
    gem.replies = ["Call note.", "Overall: likes gummies."]
    resp = client_staff.post(reverse("dash-customer-summarize-all", args=[linked.pk]))
    assert resp.status_code == 200 and b"Overall: likes gummies." in resp.content
    assert len(gem.calls) == 2  # the cached chat was reused; one map call (the call) + one reduce
    reduce_prompt = gem.calls[1]["contents"]
    assert "Cached chat note." in reduce_prompt and "Call note." in reduce_prompt
    assert SECRET_LINE not in reduce_prompt  # the reduce step sees summaries, never transcripts
    overall = CustomerSummary.objects.get(budtender_customer_id=77)
    assert (overall.text, overall.covers_count) == ("Overall: likes gummies.", 2)
    assert toast(resp)["type"] == "success"


@pytest.mark.django_db
def test_summarize_all_caps_at_the_latest_30(client_staff, bt, gem, linked):
    bt.sessions = [chat_meta(i, 2, when=f"2026-09-{(i % 28) + 1:02d}T10:00:00+00:00") for i in range(1, 41)]
    for i in range(1, 41):
        ConversationSummary.objects.create(kind="chat", ref=str(i), text=f"note {i}.", message_count=2)
    gem.replies = ["All done."]
    client_staff.post(reverse("dash-customer-summarize-all", args=[linked.pk]))
    assert CustomerSummary.objects.get(budtender_customer_id=77).covers_count == 30
    assert gem.calls[0]["contents"].count("- (chat") == 30


@pytest.mark.django_db
def test_summarize_all_survives_one_failed_conversation(client_staff, bt, gem, linked, a_call):
    bt.call_ids = ["call-1"]
    gem.replies = [RuntimeError("boom"), "Only the call remained."]
    resp = client_staff.post(reverse("dash-customer-summarize-all", args=[linked.pk]))
    # the call is the newest item, so its map call (the first) failed; the chat's note was enough
    assert resp.status_code == 200 and toast(resp)["type"] == "info" and "left out" in toast(resp)["message"]


@pytest.mark.django_db
def test_summarize_all_failure_keeps_the_old_paragraph(client_staff, bt, gem, linked):
    CustomerSummary.objects.create(budtender_customer_id=77, text="old paragraph", covers_count=1)
    ConversationSummary.objects.create(kind="chat", ref="5", text="note.", message_count=2)
    gem.replies = [RuntimeError("down")]
    resp = client_staff.post(reverse("dash-customer-summarize-all", args=[linked.pk]))
    assert resp.status_code == 204 and toast(resp)["type"] == "error"
    assert CustomerSummary.objects.get(budtender_customer_id=77).text == "old paragraph"


# ── clear memory ──────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_clear_memory_posts_the_live_id_and_the_staff_username(client_staff, bt, linked):
    resp = client_staff.post(reverse("dash-customer-memory-clear", args=[linked.pk]))
    assert ("clear", 77, "boss") in bt.calls
    assert toast(resp)["type"] == "success" and "Memory cleared" in toast(resp)["message"]


@pytest.mark.django_db
@pytest.mark.parametrize("status,word", [("not_found", "no such customer"), ("error", "refused"),
                                          ("unreachable", "unreachable")])
def test_clear_memory_reports_each_outcome_honestly(client_staff, bt, linked, status, word):
    bt.clear_result = {"status": status, "sessions_cleared": 0}
    resp = client_staff.post(reverse("dash-customer-memory-clear", args=[linked.pk]))
    assert toast(resp)["type"] == "error" and word in toast(resp)["message"]


@pytest.mark.django_db
def test_clear_memory_is_neither_offered_nor_sent_when_unlinked(client_staff, bt, jamie):
    bt.name_match = {"count": 0, "id": None}
    assert b"conv-clear-memory" not in page(client_staff, jamie.pk).content
    resp = client_staff.post(reverse("dash-customer-memory-clear", args=[jamie.pk]))
    assert toast(resp)["type"] == "error" and "NOT cleared" in toast(resp)["message"]
    assert "clear" not in bt.names()


@pytest.mark.django_db
def test_clear_button_is_confirmed_and_csrf_protected_in_the_markup(client_staff, bt, linked):
    html = page(client_staff, linked.pk).content.decode()
    assert "hx-confirm" in html and "csrfmiddlewaretoken" in html
    assert 'hx-post="' + reverse("dash-customer-memory-clear", args=[linked.pk]) in html


# ── staff gating + CSRF on every new route ────────────────────────────────────
NEW_ROUTES = [
    ("dash-customer-summarize-all", {"pk": 1}),
    ("dash-customer-conv-summary", {"pk": 1, "kind": "chat", "ref": "5"}),
    ("dash-customer-memory-clear", {"pk": 1}),
]


@pytest.mark.django_db
@pytest.mark.parametrize("name,kwargs", NEW_ROUTES, ids=[r[0] for r in NEW_ROUTES])
def test_new_routes_refuse_anonymous_and_non_staff(client, name, kwargs, bt, gem):
    url = reverse(name, kwargs=kwargs)
    assert client.post(url).status_code in (301, 302)
    User.objects.create_user("plain", password="x", is_staff=False)
    client.login(username="plain", password="x")
    assert client.post(url).status_code in (301, 302)
    assert bt.calls == [] and gem.calls == []


@pytest.mark.django_db
@pytest.mark.parametrize("name,kwargs", NEW_ROUTES, ids=[r[0] for r in NEW_ROUTES])
def test_new_routes_are_post_only_and_csrf_checked(staff, name, kwargs, bt, gem):
    url = reverse(name, kwargs=kwargs)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(staff)
    assert strict.post(url).status_code == 403  # no CSRF token
    assert strict.get(url).status_code == 405
    assert bt.calls == [] and gem.calls == []


# ── no transcript or summary text in logs; no phone number anywhere ───────────
@pytest.mark.django_db
def test_nothing_sensitive_reaches_the_logs(client_staff, bt, gem, linked, caplog):
    gem.replies = ["SUMMARY-MARKER-7714 wanted gummies."]
    with caplog.at_level(logging.DEBUG):
        client_staff.post(summary_url(linked.pk, "chat", "5"))
        client_staff.post(reverse("dash-customer-summarize-all", args=[linked.pk]))
    assert SECRET_LINE not in caplog.text and "SUMMARY-MARKER-7714" not in caplog.text
