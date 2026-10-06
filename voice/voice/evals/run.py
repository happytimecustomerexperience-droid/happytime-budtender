"""Run the golden set through a set of channels and score it. Shared by the management command
and the pytest module so both see exactly the same numbers."""

from __future__ import annotations

import contextlib
import os
import time
import uuid

from voice.evals import adapters, golden, score
from voice.evals.adapters import Answer

# Never the answer to a caller's question: notify_n8n is fired by the server; remember_caller is
# called on the phone after a caller gives their name (a CALLER line the text channels never carry).
INTERNAL_TOOLS = {"notify_n8n", "remember_caller"}


def ask(entry: golden.Entry, channel: str) -> Answer:
    """One entry on one channel, running ``setup_turns`` first on the same session where the
    channel keeps history (text / playground)."""
    store = entry.store or "yakima"
    q = entry.question
    if channel == "text":
        session = f"eval-{uuid.uuid4().hex[:12]}"
        for prior in entry.setup_turns:
            adapters.ask_text(prior, store=store, session=session, phone=entry.phone)
        return adapters.ask_text(q, store=store, session=session, phone=entry.phone)
    if channel == "playground":
        session = f"pg-{uuid.uuid4().hex[:12]}"
        for prior in entry.setup_turns:
            adapters.ask_playground(prior, store=store, session=session, phone=entry.phone)
        return adapters.ask_playground(q, store=store, session=session, phone=entry.phone)
    if channel == "storefront":
        return adapters.ask_storefront(q, store=store, category=entry.category)
    if channel == "pos":
        return adapters.ask_pos(q, store=store)
    if channel == "voice":
        time.sleep(float(os.environ.get("EVAL_VOICE_PAUSE", "2")))  # stay under Vertex's burst quota
        try:
            return adapters.ask_voice(q, store=store, setup_turns=list(entry.setup_turns))
        except Exception as exc:  # noqa: BLE001
            return Answer(channel=channel, text="", error=f"{type(exc).__name__}: {exc}")
    fn = adapters.ADAPTERS[channel]
    try:
        return fn(q, store=store)
    except NotImplementedError as exc:
        return Answer(channel=channel, text="", error=str(exc), applicable=False)
    except Exception as exc:  # noqa: BLE001 — a live adapter blowing up is a finding, not a crash
        return Answer(channel=channel, text="", error=f"{type(exc).__name__}: {exc}")


def channels_for(entry: golden.Entry, wanted: list[str]) -> list[str]:
    out = [c for c in entry.channels if c in wanted]
    if "web" in entry.channels and "web-fallback" in wanted:
        out.append("web-fallback")
    return out


def run(entries: list[golden.Entry], wanted: list[str]) -> list[score.Result]:
    results = []
    for e in entries:
        for ch in channels_for(e, wanted):
            ans = ask(e, ch)
            if not ans.applicable:
                continue
            results.append(score.score(e, ans))
    return results


@contextlib.contextmanager
def fake_budtender():
    """Swap the budtender HTTP client for the conversations harness's ``FakeBudtender`` (small
    realistic catalog) so product entries run offline. Restores on exit."""
    from voice import budtender_client, recognition
    from voice.tests.conversations.conftest import FakeBudtender
    from voice.tools import phone_cart, suggest

    fb = FakeBudtender()
    saved = []
    for module in (budtender_client, suggest, phone_cart, recognition):
        if hasattr(module, "budtender"):
            saved.append((module, module.budtender))
            module.budtender = lambda: fb
    try:
        yield fb
    finally:
        for module, original in saved:
            module.budtender = original
