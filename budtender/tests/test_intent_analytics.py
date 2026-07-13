"""Analytics roll-ups over classified conversations — the dashboard's headline
'what were people talking about' view, per-conversation and per-turn."""
import pytest

from budtender.intents import conversation_breakdown, intent_breakdown
from budtender.models import AnalyticsEvent, ChatSession


@pytest.mark.django_db
def test_conversation_breakdown_counts_primary_intent():
    for slug, intent in [("a", "product_suggestion"), ("b", "product_suggestion"),
                         ("c", "conflict_resolution"), ("d", "specials")]:
        ChatSession.objects.create(session_token=f"s-{slug}", primary_intent=intent)
    rows = conversation_breakdown(ChatSession.objects.all())
    counts = {r["intent"]: r["n"] for r in rows}
    assert counts == {"product_suggestion": 2, "conflict_resolution": 1, "specials": 1}
    # rows are ordered most-common-first with a percentage of the total.
    assert rows[0]["intent"] == "product_suggestion"
    assert rows[0]["pct"] == 50


@pytest.mark.django_db
def test_intent_breakdown_counts_user_turn_intents_only():
    def ev(role, intent):
        return AnalyticsEvent.objects.create(
            session_token="s1", event_type="chat_message", props={"role": role, "intent": intent})

    ev("user", "product_suggestion")
    ev("user", "return_policy")
    ev("assistant", "product_suggestion")  # assistant turns ignored
    rows = intent_breakdown(AnalyticsEvent.objects.filter(event_type="chat_message"))
    counts = {r["intent"]: r["n"] for r in rows}
    assert counts == {"product_suggestion": 1, "return_policy": 1}
