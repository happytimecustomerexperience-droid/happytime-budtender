"""``vapi_doctor``: a READ-ONLY end-to-end check of the phone line, for the owner to run.

It answers "if I call the number now, will everything work?" without placing a call:

  * our config (env / ``.env`` and the dashboard credentials store, which overrides it),
  * our own services (the budtender API, the public webhook route, ``/healthz``),
  * what Vapi really has (the squad, each member assistant, their tools, the phone number(s), any
    workflow still bound), compared with what this code would provision,
  * which AI steps may "think" (the owner wants none to).

Hard rules (pinned by ``voice/tests/test_vapi_doctor.py``):
  * Only HTTP GETs: ``vapi_get`` goes through ``core.services.vapi.get`` (GET only), ``http_get`` through
    ``requests.get``. Nothing is POSTed, PATCHed or deleted, no call is placed, no fake call event is
    sent to our webhook, nothing is written to the database.
  * No secret is ever printed: a secret is reported as set / missing (and matches / differs), a
    non-secret id or phone number by its last 4 characters only, and every output line is scrubbed of
    every secret value as a backstop.
"""

from __future__ import annotations

import contextlib
import hmac
import inspect
import json
import os
from dataclasses import asdict, dataclass

from django.conf import settings

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
_RANK = {PASS: 0, SKIP: 0, WARN: 1, FAIL: 2}
JSON_VERSION = 1

# The live squad the owner runs (2026-10; not a secret). ``--expect-squad`` overrides it.
KNOWN_LIVE_SQUAD_ID = "2b132e78-6b37-4b12-b99a-17d23f8906e7"

# Every value that must never appear in output (names of env/settings vars).
SECRET_NAMES = (
    "VAPI_PRIVATE_KEY", "VAPI_WEBHOOK_SECRET", "HHT_BACKEND_TOKEN", "HHT_VOICE_TOKEN", "GEMINI_API_KEY",
    "DJANGO_SECRET_KEY", "PHONE_HASH_PEPPER", "POSTGRES_PASSWORD", "EMAIL_HOST_PASSWORD",
    "SLACK_WEBHOOK_URL", "PUSHOVER_APP_TOKEN", "PUSHOVER_USER_YAKIMA", "PUSHOVER_USER_MTVERNON",
    "PUSHOVER_USER_PULLMAN",
)

# Where the Vapi docs (github.com/VapiAI/docs, fern/) say what this doctor relies on.
DOC_ASSISTANT_REQUEST = "Vapi docs fern/apis/api/openapi.json PhoneNumber.squadId/assistantId/workflowId"
DOC_THINKING = (
    "Vapi docs fern/apis/api/openapi.json components.schemas.GoogleModel (fields: model, provider, "
    "temperature, maxTokens, messages, tools, toolIds, toolRefs, knowledgeBase, "
    "emotionRecognitionEnabled, numFastTurns, realtimeConfig: no thinking field) and "
    "fern/providers/model/gemini.mdx; a thinking setting exists only for Anthropic "
    "(AnthropicThinkingConfig) and OpenAI (reasoningEffort)"
)
DOC_SERVER_AUTH = "Vapi docs fern/server-url/server-authentication.mdx (Legacy X-Vapi-Secret Support)"

# Models that do not think unless asked to (Google: 2.5 Flash-Lite thinks only with a budget set).
NON_THINKING_MODELS = {"gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-2.0-flash-lite"}

IN_CONTAINER = "docker compose exec voice-web python manage.py vapi_doctor"


@dataclass
class Check:
    id: str
    status: str
    title: str
    detail: str = ""
    fix: str = ""
    hint: str = ""


# ── the only two network functions (both GET; tests replace them) ──────────────────
def vapi_get(path: str, params: dict | None = None) -> tuple[int, object]:
    """``(status, body)`` of a Vapi GET; ``(0, None)`` on a transport failure. Never raises."""
    from core.services import vapi

    try:
        return 200, vapi.get(path, params=params)
    except vapi.VapiError as exc:
        return int(exc.status or 0), None


def http_get(url: str, headers: dict | None = None, timeout: float = 8.0) -> tuple[int, object]:
    """``(status, json-or-None)`` of a GET to one of OUR URLs, no redirects followed; ``(0, None)``
    when it cannot be reached. Never raises."""
    import requests

    try:
        resp = requests.get(url, headers=headers or {}, timeout=timeout, allow_redirects=False)
    except Exception:  # noqa: BLE001 - unreachable is a result, not a crash
        return 0, None
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


# ── helpers ────────────────────────────────────────────────────────────────────
def tail(value: object) -> str:
    """A non-secret id / phone number as its last 4 characters, or "missing"."""
    text = str(value or "").strip()
    return f"...{text[-4:]}" if text else "missing"


def secret_values() -> list[str]:
    vals = {str(os.environ.get(n) or "") for n in SECRET_NAMES}
    vals |= {str(getattr(settings, n, "") or "") for n in SECRET_NAMES}
    try:
        from dashboard.models import Credential

        vals |= set(Credential.objects.filter(name__in=SECRET_NAMES).values_list("value", flat=True))
    except Exception:  # noqa: BLE001 - no DB: the env values are still scrubbed
        pass
    return sorted((v for v in vals if len(v) >= 4), key=len, reverse=True)


def scrub(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, "***")
    return text


def _stored_credentials() -> dict[str, str]:
    try:
        from dashboard.models import Credential

        return {c.name: c.value for c in Credential.objects.exclude(value="")}
    except Exception:  # noqa: BLE001 - DB unreachable: report env only
        return {}


@contextlib.contextmanager
def effective_config(stored: dict[str, str]):
    """Inside the block, os.environ + settings carry the values the WEB workers use (a dashboard
    credential overrides .env, as ``dashboard.credentials.apply_all`` does at each request). Restored
    afterwards; nothing is written anywhere."""
    from dashboard import credentials

    names = [n for n in stored if credentials.is_known(n)]
    saved = {n: (os.environ.get(n), getattr(settings, n, None), hasattr(settings, n)) for n in names}
    try:
        for n in names:
            os.environ[n] = stored[n]
            setattr(settings, n, stored[n])
        yield
    finally:
        for n, (env, conf, had) in saved.items():
            if env is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = env
            if had:
                setattr(settings, n, conf)
            else:
                with contextlib.suppress(AttributeError):
                    delattr(settings, n)


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _url(obj: object) -> str:
    return str(((obj or {}).get("server") or {}).get("url") or "") if isinstance(obj, dict) else ""


def our_webhook_url() -> str:
    from voice.provision import WEBHOOK_PATH

    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}{WEBHOOK_PATH}" if base else ""


# ── the doctor ─────────────────────────────────────────────────────────────────
class Doctor:
    def __init__(self, *, expect_squad: str = KNOWN_LIVE_SQUAD_ID):
        self.expect_squad = (expect_squad or "").strip()
        self.checks: list[Check] = []

    def add(self, *args, **kw) -> Check:
        c = Check(*args, **kw)
        self.checks.append(c)
        return c

    def run(self) -> list[Check]:
        env_before = {n: os.environ.get(n, "") for n in ("VAPI_PRIVATE_KEY", "VAPI_WEBHOOK_SECRET")}
        stored = _stored_credentials()
        with effective_config(stored):
            self._config(stored, env_before)
            self._budtender()
            self._ours()
            self._thinking_server_side()
            self._vapi()
        return self.checks

    # (a) config ----------------------------------------------------------------
    def _source(self, name: str, stored: dict, env_before: dict) -> str:
        if name in stored:
            return "dashboard Credentials page (overrides .env)"
        if (env_before.get(name) if name in env_before else os.environ.get(name)):
            return ".env / container environment"
        return "missing"

    def _config(self, stored: dict, env_before: dict) -> None:
        key = os.environ.get("VAPI_PRIVATE_KEY", "")
        src = self._source("VAPI_PRIVATE_KEY", stored, env_before)
        if not key:
            self.add("config.vapi_key", FAIL, "Vapi private key (VAPI_PRIVATE_KEY)", "missing",
                     "Put the Vapi PRIVATE key in voice/.env as VAPI_PRIVATE_KEY= (or on /dashboard/credentials/) and restart.",
                     "Vapi dashboard > API Keys. The variable is VAPI_PRIVATE_KEY (there is no VAPI_API_KEY in this code).")
        else:
            c = self.add("config.vapi_key", PASS, "Vapi private key (VAPI_PRIVATE_KEY)", f"set (source: {src})")
            if "VAPI_PRIVATE_KEY" in stored and env_before.get("VAPI_PRIVATE_KEY") != stored["VAPI_PRIVATE_KEY"]:
                c.status = WARN
                c.detail += "; the .env value is missing or different"
                c.fix = "Make voice/.env carry the same key: provision_vapi on the command line reads .env only."

        secret = getattr(settings, "VAPI_WEBHOOK_SECRET", "") or ""
        if not secret:
            self.add("config.webhook_secret", FAIL, "Webhook secret (VAPI_WEBHOOK_SECRET)", "missing",
                     "Set VAPI_WEBHOOK_SECRET to a long random string, restart, then run provision_vapi.",
                     "Without it every Vapi webhook is refused with 401 (fail-closed, voice/signing.py).")
        else:
            st = PASS if len(secret) >= 24 else WARN
            self.add("config.webhook_secret", st, "Webhook secret (VAPI_WEBHOOK_SECRET)",
                     f"set (source: {self._source('VAPI_WEBHOOK_SECRET', stored, env_before)})"
                     + ("" if st == PASS else "; shorter than 24 characters"),
                     "" if st == PASS else "Use a longer random secret (python -c \"import secrets;print(secrets.token_urlsafe(32))\").")

        url = our_webhook_url()
        base = getattr(settings, "PUBLIC_BASE_URL", "") or ""
        if not url.startswith("https://") or "localhost" in base or "127.0.0.1" in base:
            self.add("config.server_url", FAIL, "Webhook server URL (PUBLIC_BASE_URL)", f"{url or 'missing'}",
                     "Set PUBLIC_BASE_URL=https://voice.happytimeweed.com in voice/.env (Vapi must reach it over HTTPS).")
        else:
            self.add("config.server_url", PASS, "Webhook server URL (PUBLIC_BASE_URL)", url)

        squad = getattr(settings, "VAPI_SQUAD_ID", "") or ""
        provisioned = self._provisioned_squad_id()
        if not squad:
            self.add("config.squad_id", FAIL, "Squad id (VAPI_SQUAD_ID)", "missing",
                     f"Set VAPI_SQUAD_ID={self.expect_squad or '<your squad id>'} in voice/.env (or on Credentials).")
        else:
            notes, st = [f"{tail(squad)}"], PASS
            if self.expect_squad and squad != self.expect_squad:
                st = WARN
                notes.append(f"differs from the expected live squad {tail(self.expect_squad)}")
            if provisioned and provisioned != squad:
                st = WARN
                notes.append(f"differs from the squad provision_vapi recorded ({tail(provisioned)})")
            self.add("config.squad_id", st, "Squad id (VAPI_SQUAD_ID)", "; ".join(notes),
                     "" if st == PASS else "Make VAPI_SQUAD_ID, the recorded squad and the live squad the same id.")

        number = getattr(settings, "VAPI_PHONE_NUMBER_ID", "") or ""
        store_map = self._store_map()
        if not number and not store_map:
            self.add("config.phone_number_id", FAIL, "Inbound phone number id (VAPI_PHONE_NUMBER_ID)", "missing",
                     "Copy the number's id from Vapi > Phone Numbers into VAPI_PHONE_NUMBER_ID in voice/.env.")
        else:
            detail = f"{tail(number)}" + (f"; per-store numbers: {len(store_map)}" if store_map else "")
            self.add("config.phone_number_id", PASS, "Inbound phone number id (VAPI_PHONE_NUMBER_ID)", detail)

        base_url = getattr(settings, "HHT_BUDTENDER_BASE_URL", "") or ""
        token = getattr(settings, "HHT_BACKEND_TOKEN", "") or ""
        if not base_url or not token:
            self.add("config.budtender", FAIL, "Budtender API (HHT_BUDTENDER_BASE_URL + HHT_BACKEND_TOKEN)",
                     f"url: {'set' if base_url else 'missing'}; token: {'set' if token else 'missing'}",
                     "Set both in voice/.env; the token must equal budtender's HHT_BACKEND_TOKEN in the root .env.")
        else:
            self.add("config.budtender", PASS, "Budtender API (HHT_BUDTENDER_BASE_URL + HHT_BACKEND_TOKEN)",
                     f"url: {base_url}; token: set")

        from voice import vendor_allowlist as va

        owner_raw = getattr(settings, "HHT_OWNER_PHONE", "") or ""
        owner = va.normalize_us_e164(owner_raw)
        if not owner_raw:
            self.add("config.owner_phone", WARN, "Owner phone for allowlisted vendors (HHT_OWNER_PHONE)", "missing",
                     "Set it on /dashboard/vendor-allowlist/ (Owner phone) or HHT_OWNER_PHONE=+1XXXXXXXXXX.")
        elif owner != owner_raw:
            self.add("config.owner_phone", FAIL, "Owner phone for allowlisted vendors (HHT_OWNER_PHONE)",
                     "set but not E.164 (+1 then 10 digits)", "Write it as +15095551212 (no spaces or dashes).")
        else:
            self.add("config.owner_phone", PASS, "Owner phone for allowlisted vendors (HHT_OWNER_PHONE)", f"E.164, ends {owner[-4:]}")

        from voice import constants as C

        bad, missing, ok = [], [], []
        for key, _slug in C.TRANSFER_STORES:
            raw = getattr(settings, f"HHT_TRANSFER_NUMBER_{key}", "") or ""
            if not raw:
                missing.append(key)
            elif va.normalize_us_e164(raw) != raw:
                bad.append(key)
            else:
                ok.append(f"{key} ends {raw[-4:]}")
        st = FAIL if bad else WARN if missing else PASS
        self.add("config.transfer_numbers", st, "Store transfer numbers (HHT_TRANSFER_NUMBER_*)",
                 "; ".join(ok + [f"{k} missing (placeholder +10000000000 is sent)" for k in missing]
                           + [f"{k} not E.164" for k in bad]),
                 "" if st == PASS else "Set HHT_TRANSFER_NUMBER_YAKIMA / _MTVERNON / _PULLMAN as +1XXXXXXXXXX, then provision_vapi.")

        try:
            from core.services import gemini

            g = gemini.health_check()
            st = PASS if g.get("ready") else WARN
            self.add("config.gemini", st, "Gemini for the server-side steps (summaries, website text brain, KB search)",
                     f"mode: {g.get('mode')}; ready: {bool(g.get('ready'))}" + ("" if st == PASS else f" ({g.get('reason')})"),
                     "" if st == PASS else "Set GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS and GEMINI_API_KEY in voice/.env.")
        except Exception:  # noqa: BLE001
            self.add("config.gemini", WARN, "Gemini for the server-side steps", "could not be checked")

        from voice import caller

        if caller.dynamic_greeting():
            self.add("config.dynamic_greeting", PASS, "Dynamic greeting (HHT_DYNAMIC_GREETING)",
                     "ON: Vapi must send assistant-request (number bound to no squad), checked below")
        else:
            self.add("config.dynamic_greeting", WARN, "Dynamic greeting (HHT_DYNAMIC_GREETING)",
                     "OFF: greeting by name, the customer-memory brief and the vendor allowlist do nothing",
                     "Set HHT_DYNAMIC_GREETING=1 in voice/.env, restart, run provision_vapi --dry-run then provision_vapi.",
                     "README 'Dynamic greeting rollout'. Only a number bound to no squad/assistant/workflow makes Vapi "
                     f"ask our server per call ({DOC_ASSISTANT_REQUEST}).")

        self._switches()

    def _switches(self) -> None:
        try:
            from voice import capabilities
            from voice import vendor_allowlist as va

            keys = ("call.recognize_caller", "call.greet_by_name", "call.customer_memory",
                    "call.vendor_allowlist", "call.transfer")
            off = [k for k in keys if not capabilities.is_enabled(k)]
            self.add("dashboard.switches", WARN if off else PASS, "Capabilities switches for the phone line",
                     "all on" if not off else f"off: {', '.join(off)}",
                     "" if not off else "Turn them on at /dashboard/capabilities/ if you want those features.")
            r = va.routing_status()
            st = PASS if r["entries"] else WARN
            self.add("dashboard.vendor_allowlist", st, "Vendor allowlist entries",
                     f"active numbers: {r['entries']}; direct routing live: {r['active']}",
                     "" if st == PASS else "Add vendor numbers at /dashboard/vendor-allowlist/.")
        except Exception:  # noqa: BLE001
            self.add("dashboard.switches", WARN, "Capabilities switches", "database not reachable from here",
                     f"Run the doctor where the voice database is: {IN_CONTAINER}")

    def _provisioned_squad_id(self, store: str | None = None) -> str:
        try:
            from voice import provision
            from voice.models import VapiObject

            rec = VapiObject.objects.filter(kind="squad", name=provision.squad_name(store)).first()
            return rec.vapi_id if rec else ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _store_map() -> dict[str, str]:
        from voice.webhooks import _phone_number_store_map

        return _phone_number_store_map()

    # budtender -----------------------------------------------------------------
    def _budtender(self) -> None:
        base = (getattr(settings, "HHT_BUDTENDER_BASE_URL", "") or "").rstrip("/")
        token = getattr(settings, "HHT_BACKEND_TOKEN", "") or ""
        if not base:
            self.add("budtender.health", SKIP, "Budtender /api/v1/health/", "no HHT_BUDTENDER_BASE_URL")
            return
        status, body = http_get(f"{base}/api/v1/health/")
        if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
            self.add("budtender.health", PASS, "Budtender /api/v1/health/", "ok")
        else:
            self.add("budtender.health", FAIL, "Budtender /api/v1/health/",
                     "unreachable" if status == 0 else f"HTTP {status}",
                     f"Start budtender (docker compose up -d web) and run this inside the stack: {IN_CONTAINER}",
                     "http://budtender.internal:8000 only resolves inside the docker network on the VPS.")
            return
        if not token:
            return
        store = getattr(settings, "HHT_DEFAULT_STORE", "yakima") or "yakima"
        status, _ = http_get(f"{base}/api/v1/products/categories?store={store}",
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if status == 200:
            self.add("budtender.token", PASS, "Budtender accepts HHT_BACKEND_TOKEN", "read-only GET products/categories: 200")
        elif status in (401, 403):
            self.add("budtender.token", FAIL, "Budtender accepts HHT_BACKEND_TOKEN", f"HTTP {status}: token refused",
                     "Copy HHT_BACKEND_TOKEN from the root .env into voice/.env exactly, then restart voice-web.")
        else:
            self.add("budtender.token", WARN, "Budtender accepts HHT_BACKEND_TOKEN",
                     "unreachable" if status == 0 else f"HTTP {status}")

    # (e) our public endpoints ----------------------------------------------------
    def _ours(self) -> None:
        url = our_webhook_url()
        if not url:
            return
        status, _ = http_get(url)
        if status == 405:
            self.add("webhook.reachable", PASS, "Our webhook route answers", f"GET {url}: 405 (POST-only, as designed)",
                     hint="There is no signed health probe on the webhook; no fake call event was sent.")
        elif status == 0:
            self.add("webhook.reachable", FAIL, "Our webhook route answers", f"{url}: unreachable",
                     "Check DNS for the host, that voice-web is running, and the proxy (Traefik) route.")
        elif status in (301, 302, 307, 308):
            self.add("webhook.reachable", WARN, "Our webhook route answers", f"HTTP {status} redirect",
                     "Use the final https:// address as PUBLIC_BASE_URL; Vapi does not follow redirects for POSTs.")
        else:
            self.add("webhook.reachable", FAIL, "Our webhook route answers", f"HTTP {status} (expected 405)",
                     "PUBLIC_BASE_URL must point at the voice service (path /api/voice/vapi).")
        base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
        status, body = http_get(f"{base}/healthz")
        if status == 200:
            self.add("webhook.healthz", PASS, "Voice /healthz", "status ok")
        elif isinstance(body, dict) and status == 503:
            down = [k for k in ("db", "gemini", "vapi", "budtender")
                    if not (body.get(k) or {}).get("ok", (body.get(k) or {}).get("ready"))]
            self.add("webhook.healthz", WARN, "Voice /healthz", f"degraded: {', '.join(down) or 'unknown'}",
                     "Fix the parts listed; /dashboard/health/ shows the background jobs.")
        else:
            self.add("webhook.healthz", WARN, "Voice /healthz", "unreachable" if status == 0 else f"HTTP {status}")

    # (f) thinking, server side -------------------------------------------------------
    def _thinking_server_side(self) -> None:
        from core import constants
        from core.services import gemini

        def budget(fn):
            param = inspect.signature(fn).parameters.get("thinking_budget")
            return param.default if param is not None else "not set"

        gen, stream = budget(gemini.generate), budget(gemini.generate_stream)
        ok = gen == 0 and stream == 0
        self.add("thinking.server", PASS if ok else FAIL, "Server-side AI steps never think",
                 f"model {constants.MODELS['flash']} via core/services/gemini.py: generate thinking_budget={gen}, "
                 f"generate_stream thinking_budget={stream} (post-call summary, prompt assist, evals)",
                 "" if ok else "Restore thinking_budget=0 defaults in core/services/gemini.py.")

    # (b)(c)(d) Vapi side ------------------------------------------------------------
    def _vapi(self) -> None:
        if not os.environ.get("VAPI_PRIVATE_KEY"):
            self.add("vapi.auth", SKIP, "Vapi API reachable with the key", "no VAPI_PRIVATE_KEY")
            return
        status, _ = vapi_get("/assistant", {"limit": 1})
        if status != 200:
            self.add("vapi.auth", FAIL, "Vapi API reachable with the key",
                     "unreachable" if status == 0 else f"HTTP {status}",
                     "401 = wrong key (use the PRIVATE key, not the public one); 0 = no network to api.vapi.ai.")
            return
        self.add("vapi.auth", PASS, "Vapi API reachable with the key", "GET /assistant: 200")

        ours = our_webhook_url()
        squad_id = getattr(settings, "VAPI_SQUAD_ID", "") or ""
        members = self._squad(squad_id)
        if members is not None:
            self._members(members, ours)
        self._numbers(squad_id, ours)
        self._workflows()

    def _squad(self, squad_id: str) -> list | None:
        if not squad_id:
            self.add("vapi.squad", SKIP, "Squad exists in Vapi", "no VAPI_SQUAD_ID")
            return None
        status, body = vapi_get(f"/squad/{squad_id}")
        if status != 200 or not isinstance(body, dict):
            self.add("vapi.squad", FAIL, "Squad exists in Vapi", f"{tail(squad_id)}: HTTP {status}",
                     "Wrong VAPI_SQUAD_ID, or the key belongs to another Vapi org.")
            return None
        members = [m for m in body.get("members") or [] if isinstance(m, dict)]
        self.add("vapi.squad", PASS if members else FAIL, "Squad exists in Vapi",
                 f"'{body.get('name', '')}' ({tail(squad_id)}), {len(members)} member(s)",
                 "" if members else "The squad has no members: run provision_vapi.")
        self._saved_ids_vs_squad(members)
        return members

    def _saved_ids_vs_squad(self, members: list) -> None:
        from voice import caller, provision

        try:
            saved = provision.saved_member_ids()
        except Exception:  # noqa: BLE001
            return
        live = {m.get("assistantId") for m in members}
        stray = sorted(r for r, a in saved.items() if a not in live)
        dynamic = caller.dynamic_greeting()
        if dynamic and "entry_router" not in saved:
            self.add("vapi.call_squad", FAIL, "Per-call squad can be built (dynamic greeting)",
                     "no saved entry_router assistant id: assistant-request falls back to one assistant, no squad",
                     "Run provision_vapi so every agent's Vapi id is saved.")
        elif stray:
            self.add("vapi.call_squad", WARN if dynamic else PASS, "Saved agent ids match the live squad",
                     f"saved but not in the squad: {', '.join(stray)}",
                     "Run provision_vapi so the saved ids and the squad agree." if dynamic else "")
        else:
            self.add("vapi.call_squad", PASS, "Saved agent ids match the live squad", f"{len(saved)} agent id(s) saved")
        if dynamic:
            try:
                from voice.models import VapiObject

                has = VapiObject.objects.filter(kind="tool", name="remember_caller").exclude(vapi_id="").exists()
            except Exception:  # noqa: BLE001
                has = True
            if not has:
                self.add("vapi.remember_caller", WARN, "remember_caller tool provisioned", "not provisioned",
                         "Run provision_vapi (README 'Dynamic greeting rollout' step 2-4).")

    def _members(self, members: list, ours: str) -> None:
        from voice import capabilities

        secret = getattr(settings, "VAPI_WEBHOOK_SECRET", "") or ""
        transfer_on = True
        with contextlib.suppress(Exception):
            transfer_on = capabilities.is_enabled("call.transfer")
        models: list[tuple[str, str, str]] = []
        tool_ids: list[str] = []
        for m in members:
            aid = m.get("assistantId") or ""
            if not aid:
                self.add("vapi.member", WARN, "Squad member", "an inline (unsaved) assistant: not checked")
                continue
            status, a = vapi_get(f"/assistant/{aid}")
            if status != 200 or not isinstance(a, dict):
                self.add(f"vapi.member.{tail(aid)}", FAIL, f"Squad member {tail(aid)}", f"HTTP {status}",
                         "The squad points at an assistant that is gone: run provision_vapi.")
                continue
            name = str(a.get("name") or tail(aid))
            model = a.get("model") or {}
            provider, model_id = str(model.get("provider") or ""), str(model.get("model") or "")
            models.append((name, provider, model_id))
            tool_ids += [t for t in model.get("toolIds") or [] if isinstance(t, str)]
            problems, worst = [], PASS
            url = _url(a)
            if url != ours:
                problems.append(f"serverUrl {'missing' if not url else 'is ' + url}, expected {ours}")
                worst = FAIL
            srv = a.get("server") or {}
            vapi_secret = str(srv.get("secret") or "")
            if vapi_secret and ("*" in vapi_secret or "REDACTED" in vapi_secret.upper()):
                auth = "secret shown masked by Vapi (cannot compare)"
            elif vapi_secret:
                auth = "secret matches ours" if _same(vapi_secret, secret) else "secret DIFFERS from ours"
                if "DIFFERS" in auth:
                    worst = FAIL
            elif srv.get("credentialId"):
                auth = "Vapi credential attached (cannot compare from here)"
            else:
                auth = "no webhook auth visible"
                worst = max(worst, WARN, key=_RANK.get)
            kinds = [t.get("type") for t in model.get("tools") or [] if isinstance(t, dict)]
            if name in ("vendor", "escalation") and transfer_on and "transferCall" not in kinds:
                problems.append("transferCall tool missing")
                worst = FAIL
            elif name in ("vendor", "escalation"):
                auth += "; transferCall present" if "transferCall" in kinds else "; transfers switched off"
            fix = ""
            if worst == FAIL:
                fix = "Run provision_vapi (it rewrites serverUrl, secret and tools on every agent)."
            elif worst == WARN:
                fix = "If our logs show webhook 401s, add an X-Vapi-Secret credential in Vapi."
            self.add(f"vapi.member.{name}", worst, f"Squad member '{name}'",
                     f"{provider}/{model_id}; {auth}" + (f"; {'; '.join(problems)}" if problems else ""),
                     fix, DOC_SERVER_AUTH if worst != PASS else "")
        self._tools(tool_ids, ours)
        self._thinking_vapi(models)

    def _tools(self, tool_ids: list[str], ours: str) -> None:
        if not tool_ids:
            return
        wrong, checked = [], 0
        for tid in dict.fromkeys(tool_ids):
            status, t = vapi_get(f"/tool/{tid}")
            if status != 200 or not isinstance(t, dict):
                wrong.append(f"{tail(tid)} HTTP {status}")
                continue
            url = _url(t)
            if not url:  # the KB query tool has no server
                continue
            checked += 1
            if url != ours:
                name = ((t.get("function") or {}).get("name")) or tail(tid)
                wrong.append(f"{name} -> {url}")
        self.add("vapi.tools", FAIL if wrong else PASS, "Agent tools call our webhook",
                 f"{checked} function tool(s) checked" + (f"; wrong: {', '.join(wrong)}" if wrong else ""),
                 "Run provision_vapi to point every tool at PUBLIC_BASE_URL." if wrong else "")

    def _thinking_vapi(self, models: list[tuple[str, str, str]]) -> None:
        if not models:
            return
        thinking = [f"{n} ({p}/{m})" for n, p, m in models if m not in NON_THINKING_MODELS]
        detail = "; ".join(f"{n}: {p}/{m}" for n, p, m in models)
        if not thinking:
            self.add("thinking.vapi", PASS, "Phone agents use non-thinking models", detail)
            return
        self.add("thinking.vapi", WARN, "Phone agents may think (OWNER DECISION)",
                 f"{detail}. Vapi's Google model settings have no thinking switch",
                 "Owner decision: for no thinking use gemini-2.5-flash-lite (durably: ASSISTANT_MODEL in voice/constants.py, then provision_vapi).",
                 f"gemini-2.5-flash thinks by default at Google; whether Vapi turns it off is not documented. {DOC_THINKING}. "
                 "A model typed on the dashboard Agents page is reset by seed_kb, which the root docker-compose runs at every voice-web start. "
                 "This doctor never changes models.")

    def _numbers(self, squad_id: str, ours: str) -> None:
        from voice import caller

        dynamic = caller.dynamic_greeting()
        targets: list[tuple[str, str, str]] = []
        number = getattr(settings, "VAPI_PHONE_NUMBER_ID", "") or ""
        if number:
            targets.append(("main", number, squad_id))
        for pn_id, store in self._store_map().items():
            targets.append((store, pn_id, self._provisioned_squad_id(store)))
        if not targets:
            self.add("vapi.phone_number", SKIP, "Phone number binding", "no phone number id configured")
            return
        for label, pn_id, expected in targets:
            status, pn = vapi_get(f"/phone-number/{pn_id}")
            cid = f"vapi.phone_number{'' if label == 'main' else '.' + label}"
            title = f"Phone number {tail(pn_id)} ({label})"
            if status != 200 or not isinstance(pn, dict):
                self.add(cid, FAIL, title, f"HTTP {status}", "Wrong phone number id, or another Vapi org.")
                continue
            e164 = str(pn.get("number") or "")
            bound = {k: pn.get(k) for k in ("squadId", "assistantId", "workflowId") if pn.get(k)}
            url = _url(pn)
            head = f"{'ends ' + e164[-4:] if e164 else 'number unknown'}; bound to: " + (
                ", ".join(f"{k} {tail(v)}" for k, v in bound.items()) or "nothing") + f"; serverUrl: {url or 'none'}"
            if bound.get("workflowId"):
                self.add(cid, FAIL, title, head + ". A WORKFLOW is still bound (workflows are retired)",
                         "Run provision_vapi, or in Vapi > Phone Numbers > Inbound Settings remove the workflow.",
                         DOC_ASSISTANT_REQUEST)
            elif dynamic and (bound or url != ours):
                self.add(cid, FAIL, title,
                         head + ". With HHT_DYNAMIC_GREETING on it must be bound to NOTHING and use our serverUrl, "
                         "or Vapi never sends assistant-request: the vendor allowlist, the memory brief and the name "
                         "greeting silently do nothing",
                         "Run provision_vapi --dry-run, check the PATCH /phone-number shows squadId null, then provision_vapi.",
                         DOC_ASSISTANT_REQUEST)
            elif not dynamic and (pn.get("squadId") != expected or pn.get("assistantId")):
                self.add(cid, FAIL, title,
                         head + f". With HHT_DYNAMIC_GREETING off it must be bound to the squad {tail(expected)}",
                         "Run provision_vapi (it binds the number to the squad), or set it in Vapi > Phone Numbers.")
            else:
                note = "unbound + our serverUrl: Vapi asks us per call" if dynamic else f"bound to squad {tail(expected)}"
                st = PASS
                if not dynamic and url and url != ours:
                    st, note = WARN, note + f"; serverUrl differs from {ours}"
                self.add(cid, st, title, f"{head}. OK: {note}")

    def _workflows(self) -> None:
        status, body = vapi_get("/workflow", {"limit": 100})
        if status == 404:
            wf_note = "GET /workflow: 404 (workflows retired / not in this account)"
        elif status == 200:
            n = len(body) if isinstance(body, list) else len((body or {}).get("results") or []) if isinstance(body, dict) else 0
            wf_note = f"{n} workflow(s) exist in the account"
        else:
            wf_note = f"workflow list: HTTP {status}"
        status, nums = vapi_get("/phone-number", {"limit": 100})
        items = nums if isinstance(nums, list) else (nums or {}).get("results", []) if isinstance(nums, dict) else []
        ours = {getattr(settings, "VAPI_PHONE_NUMBER_ID", "") or ""} | set(self._store_map())
        bound = [p for p in items if isinstance(p, dict) and p.get("workflowId")]
        mine = [p for p in bound if p.get("id") in ours]
        if mine:
            self.add("vapi.workflows", FAIL, "No workflow bound to our numbers",
                     f"{wf_note}; {len(mine)} of our numbers still use a workflow",
                     "Unbind the workflow (provision_vapi, or Vapi > Phone Numbers > Inbound Settings).")
        elif bound:
            self.add("vapi.workflows", WARN, "No workflow bound to our numbers",
                     f"{wf_note}; {len(bound)} other number(s) in the account use a workflow",
                     "Check those numbers in Vapi > Phone Numbers if they should be on the squad.")
        else:
            self.add("vapi.workflows", PASS, "No workflow bound to our numbers", wf_note)


# ── rendering ────────────────────────────────────────────────────────────────
def summary(checks: list[Check]) -> dict:
    out = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for c in checks:
        out[c.status.lower()] += 1
    return out


def to_json(checks: list[Check]) -> str:
    s = summary(checks)
    data = {"version": JSON_VERSION, "ok": s["fail"] == 0, "summary": s, "checks": [asdict(c) for c in checks]}
    return scrub(json.dumps(data, indent=2, sort_keys=True), secret_values())


def to_text(checks: list[Check], *, hints: bool = False) -> str:
    lines = ["Happy Time Voice: Vapi doctor (read-only: GET requests only, no call placed, no secret shown)", ""]
    for c in checks:
        lines.append(f"[{c.status}] {c.id:<28} {c.title}: {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"       fix: {c.fix}")
        if hints and c.hint and c.status != PASS:
            lines.append(f"       why: {c.hint}")
    s = summary(checks)
    lines += ["", f"Summary: {s['pass']} pass, {s['warn']} warn, {s['fail']} fail, {s['skip']} skip"]
    return scrub("\n".join(lines), secret_values())
