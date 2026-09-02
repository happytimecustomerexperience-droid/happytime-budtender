"""One ``ask()`` per channel. Every adapter returns the same ``Answer`` so the scorer never
cares where the words came from.

Offline (pytest, no keys): ``text``, ``playground``, ``pos``, ``storefront``.
Live (``manage.py eval_answers --live``): ``voice`` (Gemini + the real tools, the way Vapi runs
the prompt), ``web`` (the website view's reply through the shared brain) and ``web-fallback``
(the same view with the brain unreachable — a real channel until Phase 3 makes it visible).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # happytime-budtender/
VOICE_ROOT = Path(__file__).resolve().parents[2]  # happytime-budtender/voice/

OFFLINE_CHANNELS = ("text", "playground", "pos", "storefront")
LIVE_CHANNELS = ("voice", "web", "web-fallback")


@dataclass
class Answer:
    channel: str
    text: str
    source: str = ""          # brain | fallback | sim | static | education
    tool_calls: list[str] = field(default_factory=list)
    latency_ms: int = 0
    meta: dict = field(default_factory=dict)  # intent / grounded / escalated / args …
    error: str = ""
    applicable: bool = True   # False = this channel has no answer surface for this question


def _timed(fn):
    started = time.monotonic()
    out = fn()
    return out, int((time.monotonic() - started) * 1000)


# ── text: the shared brain in-process ────────────────────────────────────────

def _brain_meta(result: dict) -> dict:
    return {
        "intent": str(result.get("intent") or ""),
        "grounded": bool(result.get("grounded")),
        "escalated": bool(result.get("escalation_required")),
        "next_action": str(result.get("safe_next_action") or ""),
        "sources": result.get("sources") or [],
        "args": {t.get("tool"): (t.get("args") or {}) for t in (result.get("tool_results") or [])},
        "picks": next(
            ((t.get("result") or {}).get("picks") or []
             for t in (result.get("tool_results") or []) if t.get("tool") == "suggest_products"),
            [],
        ),
    }


def ask_text(question: str, *, store: str, session: str | None = None, phone: str = "") -> Answer:
    from voice.chat import answer_text_chat

    payload = {
        "message": question,
        "store": store,
        "phone": phone,
        "session_token": session or f"eval-{uuid.uuid4().hex[:12]}",
    }
    result, ms = _timed(lambda: answer_text_chat(payload))
    return Answer(
        channel="text",
        text=str(result.get("answer") or ""),
        source="brain",
        tool_calls=[str(t.get("tool")) for t in (result.get("tool_results") or [])],
        latency_ms=ms,
        meta=_brain_meta(result),
    )


# ── playground: the staff console view, end to end ───────────────────────────

def ask_playground(question: str, *, store: str, session: str | None = None, phone: str = "") -> Answer:
    """Drive ``dashboard.playground.playground_send`` exactly like the console's fetch does, then
    confirm the durable trace landed with ``source="playground"``."""
    from django.contrib.auth import get_user_model
    from django.test import RequestFactory

    from dashboard.playground import playground_send
    from voice.models import VoiceToolCall

    user_model = get_user_model()
    user, _ = user_model.objects.get_or_create(
        username="eval-console", defaults={"is_staff": True, "is_active": True}
    )
    call_id = session or f"pg-{uuid.uuid4().hex[:12]}"
    body = {"message": question, "store": store, "call_id": call_id, "phone": phone}
    request = RequestFactory().post(
        "/dashboard/playground/send", data=json.dumps(body), content_type="application/json"
    )
    request.user = user
    response, ms = _timed(lambda: playground_send(request))
    data = json.loads(response.content or b"{}")
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    call_id = str(data.get("call_id") or call_id)
    logged = VoiceToolCall.objects.filter(call_id=call_id, source="playground").exists()
    meta = _brain_meta(result)
    meta["logged_as_playground"] = logged
    return Answer(
        channel="playground",
        text=str(result.get("answer") or ""),
        source="brain",
        tool_calls=[str(t.get("tool")) for t in (result.get("tool_results") or [])],
        latency_ms=ms,
        meta=meta,
    )


# ── voice: simulate what Vapi does with the published prompt ─────────────────

_ROLE_FOR_INTENT = {"faq": "faq", "retail": "budtender", "escalation": "escalation", "vendor": "vendor"}


def _declarations(tool_names: list[str]):
    from google.genai import types

    from voice.constants import TOOL_SPECS

    decls = []
    for name in tool_names:
        spec = TOOL_SPECS.get(name)
        if not spec:
            continue
        decls.append(
            types.FunctionDeclaration(
                name=name, description=spec["description"], parameters=spec["parameters"]
            )
        )
    return [types.Tool(function_declarations=decls)] if decls else []


def _generate_with_retry(client, model_id, contents, cfg, attempts: int = 3):
    """Vertex returns 429 RESOURCE_EXHAUSTED under a burst of eval calls; back off and retry so a
    rate limit reads as latency, not as a wrong answer."""
    delay = 8.0
    for attempt in range(attempts):
        try:
            return client.models.generate_content(model=model_id, contents=contents, config=cfg)
        except Exception as exc:  # noqa: BLE001
            if "429" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def ask_voice(question: str, *, store: str, max_rounds: int = 4) -> Answer:
    """Gemini runs the SAME system prompt Vapi is provisioned with (``AgentPrompt.body`` plus the
    immutable runtime safety block) and the SAME tool schemas; every function call is answered by
    our real ``dispatch()``. The member is chosen the way the squad routes an opener."""
    from google.genai import types

    from core.services.gemini import make_client
    from kb.models import AgentPrompt
    from voice import routing
    from voice.provision import _with_runtime_safety
    from voice.tools import dispatch

    role = _ROLE_FOR_INTENT.get(routing.classify_intent(question), "faq")
    prompt = AgentPrompt.objects.filter(role=role, is_active=True).first()
    if prompt is None:
        return Answer(channel="voice", text="", error=f"no AgentPrompt(role={role})")
    system = _with_runtime_safety(prompt.body, role)
    tools = _declarations(list(prompt.tool_names or []))
    model_id = (prompt.vapi_model or "").strip() or "gemini-2.5-flash"

    client, _mode = make_client()
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        tools=tools or None,
        temperature=prompt.temperature if prompt.temperature is not None else 0.3,
        max_output_tokens=prompt.max_output_tokens or 400,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    ctx = {"call_id": f"sim-{uuid.uuid4().hex[:12]}", "store": store, "caller_number": ""}
    # A real call has already opened with the greeting and the 21+ confirmation before the caller
    # asks anything, so seed that exchange — otherwise every answer is "are you twenty-one?".
    from voice.provision import entry_greeting

    contents = [
        types.Content(role="model", parts=[types.Part(text=entry_greeting())]),
        types.Content(role="user", parts=[types.Part(text="hi, yes I'm over twenty-one")]),
        types.Content(role="model", parts=[types.Part(text="Great, thanks. What can I help you with?")]),
        types.Content(role="user", parts=[types.Part(text=question)]),
    ]
    called: list[str] = []
    started = time.monotonic()
    text = ""
    for _ in range(max_rounds):
        resp = _generate_with_retry(client, model_id, contents, cfg)
        cand = (resp.candidates or [None])[0]
        parts = list(getattr(getattr(cand, "content", None), "parts", None) or [])
        fcalls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        if not fcalls:
            text = " ".join((p.text or "") for p in parts if getattr(p, "text", None)).strip()
            break
        contents.append(cand.content)
        responses = []
        for fc in fcalls:
            args = dict(fc.args or {})
            args.setdefault("store", store)
            result = dispatch(fc.name, args, ctx)
            called.append(fc.name)
            responses.append(
                types.Part(function_response=types.FunctionResponse(name=fc.name, response={"result": result}))
            )
        contents.append(types.Content(role="user", parts=responses))
    ms = int((time.monotonic() - started) * 1000)
    return Answer(
        channel="voice", text=text, source="sim", tool_calls=called, latency_ms=ms,
        meta={"role": role, "model": model_id},
    )


# ── web / web-fallback: the website view's reply function, in the ROOT project ───

_BRIDGE = r"""
import json, os, sys
sys.path.insert(0, os.environ["EVAL_REPO_ROOT"])
os.environ["DJANGO_SETTINGS_MODULE"] = "core.settings"  # the parent is the VOICE project
os.environ.setdefault("SQL_ENGINE", "django.db.backends.sqlite3")
os.environ.setdefault("SQL_DATABASE", ":memory:")
import django
django.setup()
from types import SimpleNamespace as N
from budtender import gemini_chat as g
q = json.loads(os.environ["EVAL_QUESTION"])
store = os.environ.get("EVAL_STORE", "")
msgs = [N(role="user", content=q)]
out = {"text": "", "source": "", "error": ""}
try:
    if hasattr(g, "generate_chat_reply_with_source"):
        text, source, _intent = g.generate_chat_reply_with_source(msgs, store=store)
    else:
        shared = g._voice_chat(msgs, store=store)
        source = "brain" if shared and shared.get("answer") else "fallback"
        text = g.generate_chat_reply(msgs, store=store)
    out.update(text=text, source=source)
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


def _ask_web_http(question: str, *, store: str, base: str) -> Answer:
    """The deployed website API (`EVAL_WEB_URL`, e.g. https://budtender-api.happytimeweed.com):
    start a session, send one message, read back the reply and which path answered it. This is
    the real production hop — brain vs fallback is whatever the server decided."""
    import requests

    token = os.environ.get("HHT_BACKEND_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    started = time.monotonic()
    try:
        s = requests.post(f"{base}/api/v1/chat/session/start", json={"location": store, "channel": "chat"},
                          headers=headers, timeout=(5, 30))
        s.raise_for_status()
        session_token = s.json().get("session_token", "")
        r = requests.post(f"{base}/api/v1/chat/message",
                          json={"session_token": session_token, "message": question, "location": store},
                          headers=headers, timeout=(5, 60))
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return Answer(channel="web", text="", error=f"{type(exc).__name__}: {exc}"[:300],
                      latency_ms=int((time.monotonic() - started) * 1000))
    msg = data.get("message") or {}
    return Answer(
        channel="web", text=str(msg.get("content") or ""), source=str(data.get("source") or ""),
        latency_ms=int((time.monotonic() - started) * 1000), meta={"intent": data.get("intent", "")},
    )


def _ask_web(question: str, *, store: str, force_fallback: bool) -> Answer:
    base = os.environ.get("EVAL_WEB_URL", "").rstrip("/")
    if base:
        if force_fallback:  # the deployed server decides the path; it can't be forced from here
            return Answer(channel="web-fallback", text="", applicable=False)
        return _ask_web_http(question, store=store, base=base)
    env = dict(os.environ)
    env.update(
        EVAL_REPO_ROOT=str(REPO_ROOT),
        EVAL_QUESTION=json.dumps(question),
        EVAL_STORE=store,
        PYTHONIOENCODING="utf-8",
    )
    if force_fallback:
        env["HHT_VOICE_BASE_URL"] = ""
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", _BRIDGE], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    ms = int((time.monotonic() - started) * 1000)
    channel = "web-fallback" if force_fallback else "web"
    line = (proc.stdout or "").strip().splitlines()
    try:
        data = json.loads(line[-1]) if line else {}
    except ValueError:
        data = {}
    if not data:
        return Answer(channel=channel, text="", latency_ms=ms,
                      error=(proc.stderr or "no output")[-400:])
    return Answer(
        channel=channel, text=str(data.get("text") or ""), source=str(data.get("source") or ""),
        latency_ms=ms, error=str(data.get("error") or ""),
    )


def ask_web(question: str, *, store: str) -> Answer:
    return _ask_web(question, store=store, force_fallback=False)


def ask_web_fallback(question: str, *, store: str) -> Answer:
    return _ask_web(question, store=store, force_fallback=True)


# ── pos: the register's education helper (product/education questions only) ─

_STRAIN_TYPE = re.compile(r"\b(indica|sativa|hybrid|cbd)\b", re.I)
_EFFECT = re.compile(
    r"\b(relax(?:ed|ing)?|calm|uplift(?:ed|ing)?|happy|euphoric|sleepy|sedat(?:ed|ing)|focus(?:ed)?|energ(?:etic|y)|creative|hungry|talkative|tingly)\b",
    re.I,
)
_TERPENE = re.compile(
    r"\b(myrcene|limonene|caryophyllene|linalool|pinene|humulene|terpinolene|ocimene)\b", re.I
)


def ask_pos(question: str, *, store: str = "") -> Answer:
    """What a budtender at the register can read out from ``pos/education.py``. Only education
    questions have a surface here; anything else is marked not applicable, never scored."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from pos import education

    bits = []
    m = _STRAIN_TYPE.search(question)
    if m and education.strain_type_info(m.group(1)):
        bits.append(education.strain_type_info(m.group(1)))
    m = _EFFECT.search(question)
    if m and education.effect_info(m.group(1)):
        bits.append(education.effect_info(m.group(1)))
    m = _TERPENE.search(question)
    if m and education.terpene_info(m.group(1)):
        bits.append(education.terpene_info(m.group(1)))
    if not bits:
        return Answer(channel="pos", text="", source="education", applicable=False)
    return Answer(channel="pos", text=" ".join(bits), source="education")


# ── storefront: what the /custom-order shopper reads ─────────────────────────

_TPL_DIR = REPO_ROOT / "bundles" / "templates" / "bundles"
_TAG = re.compile(r"<[^>]+>")
_DJANGO_TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)


def _template_text(name: str) -> str:
    try:
        raw = (_TPL_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return ""
    raw = re.sub(r"\{#.*?#\}", " ", raw, flags=re.S)
    raw = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)  # developer notes are not shopper copy
    raw = _DJANGO_TAG.sub(" ", raw)
    raw = _TAG.sub(" ", raw)
    return re.sub(r"\s+", " ", raw)


def ask_storefront(question: str, *, store: str, category: str = "") -> Answer:
    """The storefront has no chat; its 'answers' are the fixed copy the shopper sees for the
    same fact — hours/address/phone from ``bundles/catalog.py``, the age gate and the
    tax-inclusive line from the templates."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from bundles import catalog

    q = question.lower()
    info = catalog.STORES.get(store or "yakima", {})
    if category == "store-facts" or re.search(r"\b(hours?|open|close|address|where|phone|number|call)\b", q):
        text = f"{info.get('street', '')}, {info.get('city', '')} · {info.get('phone', '')} · {info.get('hours', '')}"
        return Answer(channel="storefront", text=text, source="static")
    if category == "age-id" or re.search(r"\b(21|age|id\b|identification)", q):
        page = _template_text("base.html") + " " + _template_text("checkout.html")
        m = re.search(r"Are you 21 or older\?.{0,200}?visit\.", page)
        pickup = re.search(r"You must be 21\+ with valid ID[^.]*\.", page)
        text = " ".join(x.group(0) for x in (m, pickup) if x)
        return Answer(channel="storefront", text=text, source="static")
    if category == "pricing-tax" or re.search(r"\btax", q):
        page = _template_text("checkout.html") + " " + _template_text("landing.html")
        sentences = re.findall(r"[^.]{0,160}\b(?:tax|taxes|tax-inclusive)\b[^.]{0,160}\.", page, flags=re.I)
        return Answer(channel="storefront", text=" ".join(s.strip() for s in sentences[:3]), source="static")
    return Answer(channel="storefront", text="", source="static", applicable=False)


# ── sms: not built ───────────────────────────────────────────────────────────

def ask_sms(question: str, *, store: str = "") -> Answer:
    raise NotImplementedError("SMS channel is not built yet (voice/crm/sinks.py is email/n8n only); "
                              "when it is, add its ask() here and nothing else changes.")


ADAPTERS = {
    "text": ask_text,
    "playground": ask_playground,
    "voice": ask_voice,
    "web": ask_web,
    "web-fallback": ask_web_fallback,
    "pos": ask_pos,
    "storefront": ask_storefront,
    "sms": ask_sms,
}
