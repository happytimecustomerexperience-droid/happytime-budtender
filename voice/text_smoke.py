#!/usr/bin/env python
"""Rigorous TEXT smoke test of the live voice agent — no phone call needed.

Drives the real Vapi webhook (``/api/voice/vapi``) the exact way Vapi does — a signed ``tool-calls``
message per tool — and asserts each tool actually works end to end (webhook -> dispatch -> budtender
/ KB -> leak-safe result). It simulates a full caller: ask FAQ, gather a product, check its stock,
ask for a pairing, and raise an issue. Run it BEFORE telling anyone to call.

    VAPI_WEBHOOK_SECRET=... python text_smoke.py --url https://voice.happytimeweed.com/api/voice/vapi

Exit code 0 = every check passed; 1 = at least one failed (the summary says which + why).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def _call(url: str, secret: str, tool: str, args: dict) -> dict:
    """POST one signed tool-call (Mode-B shared-secret header) and return the tool's result dict."""
    body = {
        "message": {
            "type": "tool-calls",
            "call": {"id": "text-smoke"},
            "toolCalls": [{"id": "tc1", "function": {"name": tool, "arguments": args}}],
        }
    }
    r = httpx.post(
        url,
        headers={"X-Vapi-Secret": secret, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    return results[0].get("result", {}) if results else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("VOICE_WEBHOOK_URL", "https://voice.happytimeweed.com/api/voice/vapi"))
    ap.add_argument("--secret", default=os.environ.get("VAPI_WEBHOOK_SECRET", ""))
    ap.add_argument("--store", default="yakima")
    ap.add_argument("--send-escalation-email", action="store_true",
                    help="Also fire notify_staff_issue (sends a clearly-marked TEST email to staff).")
    a = ap.parse_args()
    if not a.secret:
        print("ERROR: pass --secret or set VAPI_WEBHOOK_SECRET", file=sys.stderr)
        return 2

    rows: list[tuple[str, str, str]] = []  # (status, label, detail)

    def check(status, label, detail=""):
        rows.append((status, label, detail))
        mark = {"PASS": "[OK ]", "FAIL": "[!! ]", "WARN": "[ ? ]"}[status]
        print(f"{mark} {label}" + (f" - {detail}" if detail else ""))

    print(f"== Voice text smoke test ==\n   {a.url}  (store={a.store})\n")

    # 1) FAQ — grounded KB answer (proves webhook + auth + KB).
    for q in ("what are your hours", "what is your return policy", "what payment do you take"):
        res = _call(a.url, a.secret, "faq_lookup", {"query": q, "store": a.store})
        ans = (res or {}).get("answer", "")
        ok = bool(res.get("grounded")) and bool(ans)
        check(PASS if ok else FAIL, f"faq_lookup: {q!r}", (ans[:80] or "no answer"))

    # 2) suggest_products across every category (the product brain).
    categories = [
        ("flower", {"effect_desired": "relaxed", "price_tier": "mid"}),
        ("edible", {"effect_desired": "uplifted", "subcategory": "gummies"}),
        ("cartridge", {"effect_desired": "uplifted"}),
        ("concentrate", {"effect_desired": "relaxed", "doh_only": True}),
        ("tincture", {"effect_desired": "middle"}),
    ]
    first_sku = None
    any_picks = False
    for cat, extra in categories:
        args = {"store": a.store, "category": cat, **extra}
        res = _call(a.url, a.secret, "suggest_products", args)
        picks = (res or {}).get("picks") or []
        if picks:
            any_picks = True
            first_sku = first_sku or picks[0].get("sku")
            names = ", ".join(p.get("name", "?") for p in picks[:3])
            check(PASS, f"suggest_products: {cat}", f"{len(picks)} pick(s): {names}")
        else:
            check(FAIL, f"suggest_products: {cat}",
                  (res or {}).get("spoken_summary", "empty — no inventory?"))

    # 3) check_inventory + pair_upsell — only meaningful with a real SKU from step 2.
    if first_sku:
        inv = _call(a.url, a.secret, "check_inventory", {"store": a.store, "sku": first_sku})
        check(PASS if inv.get("in_stock") else FAIL, f"check_inventory: {first_sku}",
              f"in_stock={inv.get('in_stock')} otd={inv.get('price_otd')}")
        pair = _call(a.url, a.secret, "pair_upsell", {"store": a.store, "anchor_sku": first_sku})
        # A silent (no-offer) pairing is valid, not a failure — just report it.
        offered = bool(pair.get("offer"))
        check(PASS, f"pair_upsell: {first_sku}",
              "offer: " + (pair.get("pair", {}).get("name", "?") if offered else "none (valid)"))
    else:
        check(WARN, "check_inventory / pair_upsell", "skipped — no SKU (inventory empty)")

    # 4) Escalation email path (opt-in — sends a real, clearly-marked TEST email).
    if a.send_escalation_email:
        res = _call(a.url, a.secret, "notify_staff_issue", {
            "store": a.store, "issue_type": "other",
            "summary": "[AUTOMATED SYSTEM TEST] Please disregard — verifying the voice escalation path.",
            "caller_name": "System Test",
        })
        ok = bool(res) and not res.get("error")
        check(PASS if ok else FAIL, "notify_staff_issue (TEST email)", json.dumps(res)[:80])
    else:
        check(WARN, "notify_staff_issue", "skipped — pass --send-escalation-email to test the email")

    # Summary
    n_fail = sum(1 for s, *_ in rows if s == FAIL)
    n_pass = sum(1 for s, *_ in rows if s == PASS)
    print(f"\n== {n_pass} passed, {n_fail} failed ==")
    if not any_picks:
        print("HEADLINE: suggest_products returned 0 picks for every category -> budtender has no\n"
              "          in-stock inventory. Run the Dutchie sync on the VPS, then re-run this:\n"
              "          docker compose exec web python manage.py shell -c "
              "\"from budtender.tasks import sync_inventory_all; print(sync_inventory_all())\"")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
