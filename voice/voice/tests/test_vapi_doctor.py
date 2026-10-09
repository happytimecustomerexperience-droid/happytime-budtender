"""``manage.py vapi_doctor``: read-only, secret-free, PASS/WARN/FAIL per check, non-zero exit on FAIL.

Offline: Vapi (``doctor.vapi_get``) and our own URLs (``doctor.http_get``) are fakes; every write verb
on the Vapi client and on ``requests`` is made to explode, so a write attempt fails the test.
"""

from __future__ import annotations

import io
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from voice import doctor

SQUAD = doctor.KNOWN_LIVE_SQUAD_ID
NUMBER_ID = "pn-main-7d1c"
OURS = "https://voice.happytimeweed.com/api/voice/vapi"
BASE = "http://budtender.internal:8000"
VAPI_KEY = "vapi-private-key-Qx7Zk3Wv9Lm2Pa8R"
WEBHOOK_SECRET = "webhook-secret-Hy4Ue8Tr2Ow6Bn1M"
BACKEND_TOKEN = "backend-token-Jd5Kf9Xc3Vb7Nm2G"
OWNER = "+15095557788"
ROLES = ("entry_router", "budtender", "faq", "vendor", "escalation")


class FakeVapi:
    def __init__(self, model="gemini-2.5-flash-lite"):
        self.calls: list[str] = []
        self.routes: dict[str, tuple[int, object]] = {
            "/assistant": (200, []),
            f"/squad/{SQUAD}": (200, {"id": SQUAD, "name": "Happy Time Voice",
                                      "members": [{"assistantId": f"asst-{r}"} for r in ROLES]}),
            f"/phone-number/{NUMBER_ID}": (200, self.number()),
            "/phone-number": (200, [self.number()]),
            "/workflow": (404, None),
            "/tool/tool-faq": (200, {"type": "function", "function": {"name": "faq_lookup"}, "server": {"url": OURS}}),
            "/tool/tool-kb": (200, {"type": "query"}),
        }
        for r in ROLES:
            tools = [{"type": "transferCall", "destinations": []}] if r in ("vendor", "escalation") else []
            self.routes[f"/assistant/asst-{r}"] = (200, {
                "id": f"asst-{r}", "name": r,
                "model": {"provider": "google", "model": model, "toolIds": ["tool-faq", "tool-kb"], "tools": tools},
                "server": {"url": OURS, "secret": WEBHOOK_SECRET},
            })

    @staticmethod
    def number(**kw):
        return {"id": NUMBER_ID, "number": "+15095550123", "name": "Happy Time inbound",
                "squadId": None, "assistantId": None, "workflowId": None, "server": {"url": OURS}, **kw}

    def bind(self, **kw):
        self.routes[f"/phone-number/{NUMBER_ID}"] = (200, self.number(**kw))
        self.routes["/phone-number"] = (200, [self.number(**kw)])

    def __call__(self, path, params=None):
        self.calls.append(path)
        return self.routes.get(path, (404, None))


class FakeHttp:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.routes: dict[str, tuple[int, object]] = {
            f"{BASE}/api/v1/health/": (200, {"status": "ok"}),
            f"{BASE}/api/v1/products/categories?store=yakima": (200, {"options": []}),
            OURS: (405, None),
            "https://voice.happytimeweed.com/healthz": (200, {"status": "ok"}),
        }

    def __call__(self, url, headers=None, timeout=8.0):
        self.calls.append((url, dict(headers or {})))
        return self.routes.get(url, (0, None))


@pytest.fixture
def world(settings, monkeypatch, db):
    """A fully configured, healthy line with the dynamic greeting ON."""
    import requests

    from core.services import gemini
    from core.services import vapi as vapi_client
    from dashboard.models import VendorAllowlistEntry
    from kb.models import AgentPrompt
    from voice.models import VapiObject

    def boom(*a, **k):  # any write = test failure
        raise AssertionError("vapi_doctor attempted a non-GET request")

    for verb in ("post", "patch", "delete"):
        monkeypatch.setattr(vapi_client, verb, boom)
    for verb in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(requests, verb, boom)

    monkeypatch.setattr(gemini, "health_check", lambda: {"mode": "mock", "ready": True, "reason": "mock"})
    monkeypatch.setenv("VAPI_PRIVATE_KEY", VAPI_KEY)
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("HHT_BACKEND_TOKEN", BACKEND_TOKEN)
    settings.VAPI_PRIVATE_KEY = VAPI_KEY
    settings.VAPI_WEBHOOK_SECRET = WEBHOOK_SECRET
    settings.PUBLIC_BASE_URL = "https://voice.happytimeweed.com"
    settings.VAPI_SQUAD_ID = SQUAD
    settings.VAPI_PHONE_NUMBER_ID = NUMBER_ID
    settings.VAPI_PHONE_NUMBER_STORE_MAP = ""
    settings.HHT_BUDTENDER_BASE_URL = BASE
    settings.HHT_BACKEND_TOKEN = BACKEND_TOKEN
    settings.HHT_OWNER_PHONE = OWNER
    settings.HHT_TRANSFER_NUMBER_YAKIMA = "+15095711106"
    settings.HHT_TRANSFER_NUMBER_MTVERNON = "+13604882923"
    settings.HHT_TRANSFER_NUMBER_PULLMAN = "+15093342788"
    settings.HHT_DEFAULT_STORE = "yakima"
    settings.HHT_DYNAMIC_GREETING = True
    settings.HHT_SQUAD_MODE = "multi"  # this world is the five-agent squad (single mode: test_single_squad_mode.py)

    for r in ROLES:
        AgentPrompt.objects.update_or_create(role=r, defaults={"body": "x", "vapi_assistant_id": f"asst-{r}"})
    VapiObject.objects.create(kind="squad", name="Happy Time Voice", vapi_id=SQUAD)
    VapiObject.objects.create(kind="tool", name="remember_caller", vapi_id="tool-rc")
    VendorAllowlistEntry.objects.create(name="Acme Farms", phone="+15095554321")

    fv, fh = FakeVapi(), FakeHttp()
    monkeypatch.setattr(doctor, "vapi_get", fv)
    monkeypatch.setattr(doctor, "http_get", fh)
    return fv, fh


def run(*args) -> tuple[str, int]:
    out = io.StringIO()
    try:
        call_command("vapi_doctor", *args, stdout=out)
        code = 0
    except CommandError:
        code = 1
    return out.getvalue(), code


def report(*args) -> tuple[dict, int]:
    text, code = run("--json", *args)
    data = json.loads(text)
    return {c["id"]: c for c in data["checks"]} | {"_": data}, code


def test_all_green(world):
    checks, code = report()
    s = checks["_"]["summary"]
    bad = {k: v for k, v in checks.items() if k != "_" and v["status"] in ("WARN", "FAIL")}
    assert bad == {} and s["fail"] == 0 and s["warn"] == 0 and checks["_"]["ok"] is True
    assert code == 0
    assert checks["vapi.phone_number"]["status"] == "PASS"
    assert "Vapi asks us per call" in checks["vapi.phone_number"]["detail"]
    assert {f"vapi.member.{r}" for r in ROLES} <= set(checks)


def test_text_output_is_pass_warn_fail_lines_with_fixes(world):
    fv, _ = world
    fv.bind(squadId="some-other-squad")
    text, code = run()
    assert code == 1
    assert "[FAIL] vapi.phone_number" in text and "fix: Run provision_vapi --dry-run" in text
    assert "[PASS] config.vapi_key" in text
    assert text.strip().splitlines()[-1].startswith("Summary:")


def test_gemini_flash_members_are_flagged_as_an_owner_decision_never_changed(world):
    fv, _ = world
    for r in ROLES:
        fv.routes[f"/assistant/asst-{r}"][1]["model"]["model"] = "gemini-2.5-flash"
    checks, code = report()
    t = checks["thinking.vapi"]
    assert t["status"] == "WARN" and "OWNER DECISION" in t["title"]
    assert "gemini-2.5-flash-lite" in t["fix"] and "GoogleModel" in t["hint"]
    assert code == 0  # a WARN never fails the run
    assert checks["thinking.server"]["status"] == "PASS"


def test_dynamic_greeting_on_but_number_bound_to_squad_fails(world):
    fv, _ = world
    fv.bind(squadId=SQUAD)
    checks, code = report()
    pn = checks["vapi.phone_number"]
    assert pn["status"] == "FAIL" and "silently do nothing" in pn["detail"] and code == 1


def test_dynamic_greeting_on_number_without_our_server_url_fails(world):
    fv, _ = world
    fv.bind(server={"url": "https://old.example.com/hook"})
    checks, _ = report()
    assert checks["vapi.phone_number"]["status"] == "FAIL"


def test_dynamic_greeting_off_and_number_bound_to_the_squad_passes(world, settings):
    fv, _ = world
    settings.HHT_DYNAMIC_GREETING = False
    fv.bind(squadId=SQUAD)
    checks, code = report()
    assert checks["vapi.phone_number"]["status"] == "PASS"
    assert checks["config.dynamic_greeting"]["status"] == "WARN"
    assert "vendor allowlist" in checks["config.dynamic_greeting"]["detail"]
    assert code == 0


def test_dynamic_greeting_off_number_unbound_or_wrong_squad_fails(world, settings):
    fv, _ = world
    settings.HHT_DYNAMIC_GREETING = False
    checks, code = report()  # unbound
    assert checks["vapi.phone_number"]["status"] == "FAIL" and code == 1
    fv.bind(squadId="wrong-squad-0000")
    checks, code = report()
    assert checks["vapi.phone_number"]["status"] == "FAIL" and code == 1


def test_workflow_still_bound_fails(world):
    fv, _ = world
    fv.bind(workflowId="wf-old-9999")
    fv.routes["/workflow"] = (200, [{"id": "wf-old-9999"}])
    checks, code = report()
    assert checks["vapi.phone_number"]["status"] == "FAIL" and "WORKFLOW" in checks["vapi.phone_number"]["detail"]
    assert checks["vapi.workflows"]["status"] == "FAIL" and code == 1


def test_workflow_on_another_number_only_warns(world):
    fv, _ = world
    fv.routes["/phone-number"] = (200, [fv.number(), {"id": "pn-other", "workflowId": "wf-1"}])
    checks, _ = report()
    assert checks["vapi.workflows"]["status"] == "WARN"


def test_missing_key_fails_and_never_calls_vapi(world, monkeypatch, settings):
    fv, _ = world
    monkeypatch.delenv("VAPI_PRIVATE_KEY")
    settings.VAPI_PRIVATE_KEY = ""
    checks, code = report()
    assert checks["config.vapi_key"]["status"] == "FAIL" and "VAPI_PRIVATE_KEY" in checks["config.vapi_key"]["fix"]
    assert checks["vapi.auth"]["status"] == "SKIP"
    assert fv.calls == [] and code == 1


def test_all_red(world, monkeypatch, settings):
    fv, fh = world
    for name in ("VAPI_PRIVATE_KEY", "VAPI_WEBHOOK_SECRET", "HHT_BACKEND_TOKEN"):
        monkeypatch.delenv(name)
    for name in ("VAPI_PRIVATE_KEY", "VAPI_WEBHOOK_SECRET", "VAPI_SQUAD_ID", "VAPI_PHONE_NUMBER_ID",
                 "HHT_BUDTENDER_BASE_URL", "HHT_BACKEND_TOKEN", "HHT_OWNER_PHONE"):
        setattr(settings, name, "")
    settings.PUBLIC_BASE_URL = "http://localhost:8000"
    fh.routes.clear()
    checks, code = report()
    for cid in ("config.vapi_key", "config.webhook_secret", "config.server_url", "config.squad_id",
                "config.phone_number_id", "config.budtender"):
        assert checks[cid]["status"] == "FAIL", cid
    assert checks["_"]["ok"] is False and code == 1 and fv.calls == []


def test_wrong_vapi_key_fails_auth(world):
    fv, _ = world
    fv.routes["/assistant"] = (401, None)
    checks, code = report()
    assert checks["vapi.auth"]["status"] == "FAIL" and "401" in checks["vapi.auth"]["detail"] and code == 1


def test_member_secret_or_server_url_mismatch_fails(world):
    fv, _ = world
    fv.routes["/assistant/asst-faq"][1]["server"] = {"url": OURS, "secret": "an-old-secret-value"}
    fv.routes["/assistant/asst-budtender"][1]["server"] = {"url": "https://old.example.com/api/voice/vapi"}
    checks, _ = report()
    assert checks["vapi.member.faq"]["status"] == "FAIL" and "DIFFERS" in checks["vapi.member.faq"]["detail"]
    assert checks["vapi.member.budtender"]["status"] == "FAIL"


def test_member_with_credential_passes_and_without_auth_warns(world):
    fv, _ = world
    fv.routes["/assistant/asst-faq"][1]["server"] = {"url": OURS, "credentialId": "cred-1"}
    fv.routes["/assistant/asst-budtender"][1]["server"] = {"url": OURS}
    checks, _ = report()
    assert checks["vapi.member.faq"]["status"] == "PASS"
    assert checks["vapi.member.budtender"]["status"] == "WARN"


def test_transfer_tool_missing_on_vendor_fails(world):
    fv, _ = world
    fv.routes["/assistant/asst-vendor"][1]["model"]["tools"] = []
    checks, _ = report()
    assert checks["vapi.member.vendor"]["status"] == "FAIL" and "transferCall" in checks["vapi.member.vendor"]["detail"]


def test_squad_not_found_fails(world):
    fv, _ = world
    del fv.routes[f"/squad/{SQUAD}"]
    checks, code = report()
    assert checks["vapi.squad"]["status"] == "FAIL" and code == 1


def test_squad_id_not_the_expected_live_squad_warns(world):
    checks, _ = report("--expect-squad", "another-squad-1234")
    assert checks["config.squad_id"]["status"] == "WARN"


def test_tool_pointing_elsewhere_fails(world):
    fv, _ = world
    fv.routes["/tool/tool-faq"] = (200, {"function": {"name": "faq_lookup"}, "server": {"url": "https://x.example/h"}})
    checks, _ = report()
    assert checks["vapi.tools"]["status"] == "FAIL" and "faq_lookup" in checks["vapi.tools"]["detail"]


def test_budtender_down_or_token_refused(world):
    _, fh = world
    fh.routes[f"{BASE}/api/v1/products/categories?store=yakima"] = (401, None)
    checks, _ = report()
    assert checks["budtender.token"]["status"] == "FAIL"
    fh.routes[f"{BASE}/api/v1/health/"] = (0, None)
    checks, _ = report()
    assert checks["budtender.health"]["status"] == "FAIL" and "docker compose exec" in checks["budtender.health"]["fix"]


def test_webhook_route_and_healthz(world):
    _, fh = world
    fh.routes[OURS] = (404, None)
    fh.routes["https://voice.happytimeweed.com/healthz"] = (503, {"db": {"ok": True}, "gemini": {"ready": False},
                                                                  "vapi": {"ok": True}, "budtender": {"ok": True}})
    checks, _ = report()
    assert checks["webhook.reachable"]["status"] == "FAIL"
    assert checks["webhook.healthz"]["status"] == "WARN" and "gemini" in checks["webhook.healthz"]["detail"]


def test_dynamic_on_without_saved_entry_router_fails(world):
    from kb.models import AgentPrompt

    AgentPrompt.objects.filter(role="entry_router").update(vapi_assistant_id="")
    checks, _ = report()
    assert checks["vapi.call_squad"]["status"] == "FAIL"


def test_owner_phone_and_transfer_numbers(world, settings):
    settings.HHT_OWNER_PHONE = "509 555 7788"
    settings.HHT_TRANSFER_NUMBER_PULLMAN = ""
    checks, _ = report()
    assert checks["config.owner_phone"]["status"] == "FAIL"
    assert checks["config.transfer_numbers"]["status"] == "WARN"


def test_key_stored_on_dashboard_is_used_and_source_reported(world, monkeypatch, settings):
    from dashboard.models import Credential

    monkeypatch.delenv("VAPI_PRIVATE_KEY")
    settings.VAPI_PRIVATE_KEY = ""
    Credential.objects.create(name="VAPI_PRIVATE_KEY", value=VAPI_KEY)
    checks, _ = report()
    c = checks["config.vapi_key"]
    assert "dashboard" in c["detail"] and c["status"] == "WARN" and ".env" in c["fix"]
    assert checks["vapi.auth"]["status"] == "PASS"  # the stored key was used for the GETs
    import os

    assert "VAPI_PRIVATE_KEY" not in os.environ  # and put back afterwards


def test_no_secret_value_ever_appears_in_any_output(world, monkeypatch):
    from dashboard.models import Credential

    fv, _ = world
    Credential.objects.create(name="HHT_BACKEND_TOKEN", value=BACKEND_TOKEN)
    # Worst case: Vapi echoes a secret back in a field the doctor prints (the number's name).
    fv.bind(name=f"inbound {VAPI_KEY}", squadId=SQUAD)
    fv.routes["/assistant/asst-faq"][1]["server"] = {"url": OURS, "secret": "wrong-" + WEBHOOK_SECRET}
    outs = [run()[0], run("--fix-hints")[0], run("--json")[0], run("--json", "--fix-hints")[0]]
    for text in outs:
        for secret in (VAPI_KEY, WEBHOOK_SECRET, BACKEND_TOKEN):
            assert secret not in text
            assert secret[-6:] not in text
        assert OWNER not in text and "5095557788" not in text  # phone numbers: last 4 only
        assert NUMBER_ID not in text and SQUAD not in text  # ids: last 4 only


def test_read_only_no_database_writes(world):
    from dashboard.models import Credential
    from kb.models import AgentPrompt
    from voice.models import VapiObject, VoiceCall

    before = [list(m.objects.order_by("pk").values()) for m in (AgentPrompt, VapiObject, Credential, VoiceCall)]
    run()
    after = [list(m.objects.order_by("pk").values()) for m in (AgentPrompt, VapiObject, Credential, VoiceCall)]
    assert before == after


def test_json_is_stable(world):
    text, _ = run("--json")
    data = json.loads(text)
    assert set(data) == {"version", "ok", "summary", "checks"} and data["version"] == 1
    assert set(data["summary"]) == {"pass", "warn", "fail", "skip"}
    for c in data["checks"]:
        assert set(c) == {"id", "status", "title", "detail", "fix", "hint"}
        assert c["status"] in ("PASS", "WARN", "FAIL", "SKIP")
    assert [c["id"] for c in data["checks"]] == [c["id"] for c in json.loads(run("--json")[0])["checks"]]


def test_real_vapi_get_is_a_get_and_never_raises(monkeypatch):
    from core.services import vapi as vapi_client

    seen = []
    monkeypatch.setattr(vapi_client, "get", lambda path, params=None: seen.append(path) or {"ok": 1})
    assert doctor.vapi_get("/squad/x") == (200, {"ok": 1}) and seen == ["/squad/x"]

    def fail(path, params=None):
        raise vapi_client.VapiError("nope", status=404)

    monkeypatch.setattr(vapi_client, "get", fail)
    assert doctor.vapi_get("/squad/x") == (404, None)
