#!/usr/bin/env python3
"""Pre-send check for a Happy Time bundle campaign.

Answers the one question that decides whether a send works: does a link signed
with alpine-automations' STORED secret verify on the LIVE site?

That has to be tested against the deployed site, not by diffing two local files.
The local happytime-budtender/.env is a dev secret and differs from production
perfectly legitimately — only the VPS value is authoritative, so a local-vs-local
comparison would cry wolf on a healthy setup.

The secret is read out of alpine's .env rather than the process environment,
because that is exactly how a broken send hides: an exported BUNDLE_URL_SECRET in
your shell silently overrides .env, so a link you build by hand verifies while the
campaign — built later, in another shell, or on a schedule — signs with the stored
value and 400s for every recipient.

Secrets are never printed; only lengths, a short fingerprint, and match/mismatch.

    python .claude/skills/bundle-campaign/scripts/preflight.py
    python .claude/skills/bundle-campaign/scripts/preflight.py --items 3483543:1,10:2

Exit code 0 = safe to send. 1 = do not send.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import pathlib
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

DEFAULT_BUDTENDER = pathlib.Path.home() / "OneDrive/Desktop/happytime-budtender"
DEFAULT_ALPINE = pathlib.Path.home() / "OneDrive/Desktop/alpine-automations"
# The ORIGIN, not the apex, and deliberately so: this script fetches the page with
# urllib, and Vercel answers scripted requests to happytimeweed.com with a bot
# checkpoint (HTTP 429 + a "Vercel Security Checkpoint" HTML body). Pointing this
# at the apex would report every healthy deploy as broken. Creatives still link the
# apex — verify that one in a browser.
DEFAULT_BASE = "https://budtender-api.happytimeweed.com/custom-order/"

SIGNED_PARAMS = ("b", "loc", "i", "c", "exp")

OK, BAD, WARN = "  [ok] ", "  [!!] ", "  [--] "


def read_env_value(env_path: pathlib.Path, key: str) -> str | None:
    """The value as STORED in the file. Deliberately ignores os.environ."""
    if not env_path.exists():
        return None
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def fingerprint(secret: str) -> str:
    """Short, non-reversible tag so two values can be compared in a log safely."""
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def canonical(params: dict) -> str:
    parts: list[str] = []
    for key in sorted(params):
        if key not in SIGNED_PARAMS:
            continue
        value = params[key]
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in sorted(str(x) for x in values):
            parts.append(f"{key}={v}")
    return "&".join(parts)


def build_url(secret: str, base: str, bundle: str, store: str,
              items: list[tuple[str, int]], ttl_days: int = 14) -> str:
    params: dict = {
        "b": bundle,
        "loc": store,
        "i": [f"{sku}:{qty}" for sku, qty in items],
        "exp": str(int(time.time()) + ttl_days * 86400),
    }
    sig = hmac.new(secret.encode(), canonical(params).encode(), hashlib.sha256).hexdigest()
    pairs: list[tuple[str, str]] = []
    for key in ("b", "loc", "i", "exp"):
        value = params[key]
        if isinstance(value, list):
            pairs.extend((key, v) for v in value)
        else:
            pairs.append((key, str(value)))
    pairs.append(("sig", sig))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(pairs)}"


def fetch(url: str, timeout: int = 60) -> tuple[int, str]:
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # DNS, TLS, timeout — a send-blocking condition either way
        return 0, f"{type(e).__name__}: {e}"


def parse_items(raw: str) -> list[tuple[str, int]]:
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        sku, _, qty = chunk.partition(":")
        out.append((sku.strip(), int(qty or 1)))
    return out


def main() -> int:
    # Windows consoles default to cp1252 and mangle the em-dashes in page titles into
    # replacement chars, which makes a healthy run look broken.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Bundle campaign pre-send check.")
    ap.add_argument("--budtender", type=pathlib.Path, default=DEFAULT_BUDTENDER)
    ap.add_argument("--alpine", type=pathlib.Path, default=DEFAULT_ALPINE)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--bundle", default="roll-relax")
    ap.add_argument("--store", default="yakima")
    ap.add_argument("--items", default="1:1,10:2,20:1",
                    help="Real product ids make this a truer test of the live page.")
    args = ap.parse_args()

    problems: list[str] = []
    print("Bundle campaign preflight\n" + "=" * 46)

    print("\n1. Stored secrets (read from .env, NOT the environment)")
    bud_env = args.budtender / ".env"
    alp_env = args.alpine / ".env"
    bud = read_env_value(bud_env, "BUNDLE_URL_SECRET")
    alp = read_env_value(alp_env, "BUNDLE_URL_SECRET")

    if not alp_env.exists():
        print(f"{BAD}alpine     no .env at {alp_env}")
        problems.append("alpine .env missing")
    elif not alp:
        print(f"{BAD}alpine     BUNDLE_URL_SECRET not set — cannot sign anything")
        problems.append("alpine secret unset")
    else:
        print(f"{OK}alpine     set (len {len(alp)}, fp {fingerprint(alp)})  <- signs the campaign")

    if bud:
        same = hmac.compare_digest(bud, alp) if alp else False
        note = "same as alpine" if same else "differs from alpine (fine — local dev secret)"
        print(f"{WARN}budtender  set (len {len(bud)}, fp {fingerprint(bud)})  local only, {note}")
    else:
        print(f"{WARN}budtender  no local secret — only affects local dev, not the send")
    print("       Only the DEPLOYED secret matters; step 2 tests that directly.")

    signer = alp or bud
    if not signer:
        print("\nNo secret available to sign with — cannot test the live site.")
        return 1

    print("\n2. Live site, signed with alpine's STORED secret  <- THE decisive check")
    items = parse_items(args.items)
    url = build_url(signer, args.base, args.bundle, args.store, items)
    code, body = fetch(url)
    print(f"   {args.base}  bundle={args.bundle} store={args.store} items={len(items)}")
    if code == 200:
        print(f"{OK}HTTP 200 ({len(body)} bytes)")
        title = ""
        if "<title>" in body:
            title = body.split("<title>", 1)[1].split("</title>", 1)[0].strip()
        print(f"       title: {title[:70]}")
        for label, needle in (("store name", "Happy Time"), ("stylesheet", "bundle.css")):
            if needle in body:
                print(f"{OK}{label} present")
            else:
                print(f"{BAD}{label} MISSING")
                problems.append(f"{label} missing")
        low = body.lower()
        leaks = [w for w in ("margin_pct", "velocity", "price_z", "serialno", "batchid") if w in low]
        if leaks:
            print(f"{BAD}staff data leaked to a public page: {leaks}")
            problems.append("leak")
        else:
            print(f"{OK}no staff-only fields in the page")
    elif code == 400:
        print(f"{BAD}HTTP 400 - the live site rejected this signature.")
        print("       The DEPLOYED secret differs from alpine's. Every link you send")
        print("       would fail. Fix: copy the VPS .env value into alpine-automations/.env")
        print("       (change alpine, not the VPS - restarting it breaks links in flight).")
        problems.append("live rejected alpine's signature")
    elif code == 0:
        print(f"{BAD}could not reach the site: {body}")
        problems.append("unreachable")
    else:
        print(f"{BAD}HTTP {code} (unexpected)")
        problems.append(f"HTTP {code}")

    print("\n3. Forgery still refused")
    # Swap to a DIFFERENT slug than the one under test — replacing a bundle with
    # itself is a no-op that re-fetches the valid URL, reports 200, and then calls a
    # healthy site forged. Pick the first known bundle that isn't the current one.
    other = next((b for b in ("weekend", "roll-relax", "vape-munch") if b != args.bundle), None)
    if not other:
        print(f"{WARN}no alternate bundle slug to tamper with; skipped")
    else:
        tampered = url.replace(f"b={args.bundle}", f"b={other}")
        if tampered == url:
            print(f"{WARN}could not construct a tampered URL; skipped")
        else:
            code, _ = fetch(tampered)
            if code == 400:
                print(f"{OK}editing the bundle slug ({args.bundle} -> {other}) is rejected")
            else:
                print(f"{BAD}tampered link returned {code}, expected 400 - signature not enforced")
                problems.append("forgery accepted")

    print("\n" + "=" * 46)
    if problems:
        print("DO NOT SEND — " + "; ".join(problems))
        return 1
    print("Safe to send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
