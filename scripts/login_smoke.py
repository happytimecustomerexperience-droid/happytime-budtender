"""Smoke #1 — prove the employee login + cookie works.

    python scripts/login_smoke.py yakima

Prints (cookie_header[:40], session_gid, user_id). Non-empty session_gid == win.
Then does a trivial POS GET to check the cookie is accepted cross-subdomain
(ash.backoffice login cookie -> ash.pos.dutchie.com).
"""

import sys

from dutchie.pos_register_client import PosRegisterClient
from dutchie.stores import get_store


def main(store_name: str) -> int:
    store = get_store(store_name)
    client = PosRegisterClient(store)
    sess = client._session()
    print("cookie:", (sess.cookie_header or "")[:40], "...")
    print("session_gid:", sess.session_gid)
    print("user_id:", sess.user_id)
    if not sess.session_gid:
        print("FAIL: empty session_gid — login did not return a session")
        return 1
    # cross-subdomain cookie probe (read-only)
    probe = client.get("/")
    print("pos GET / ->", probe.get("status"))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/login_smoke.py <store>")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
