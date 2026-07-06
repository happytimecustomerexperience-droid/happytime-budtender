"""Unit checks for the dutchie package internals (no network)."""

import json

from dutchie.secrets import decrypt_secret, encrypt_secret, is_encrypted
from dutchie.session import EmployeeSession, PosClient, Store
from dutchie.stores import load_stores
from dutchie.transport import headers

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=700002, lsp_id=700045, loc_id=700498, register_id=700318,
              username="u", password="p")


def test_session_block_shape():
    c = PosClient(STORE)
    c._pinned = EmployeeSession("ck", "SID", 95602)
    b = c.session_block(with_register=True)
    assert b == {"SessionId": "SID", "LspId": "700045", "LocId": "700498",
                 "OrgId": "700002", "UserId": "95602", "Register": 700318}
    assert "Register" not in c.session_block(with_register=False)


def test_secrets_roundtrip():
    tok = encrypt_secret("hunter2")
    assert is_encrypted(tok) and tok != "hunter2"
    assert decrypt_secret(tok) == "hunter2"
    assert decrypt_secret("plain") == "plain"  # untagged passthrough


def test_headers_self_consistent():
    h = headers("https://ash.pos.dutchie.com")
    assert h["Origin"] == "https://ash.pos.dutchie.com"
    assert h["Referer"].startswith("https://ash.pos.dutchie.com")
    assert "Chrome" in h["User-Agent"]


def test_stores_loader(tmp_path, monkeypatch):
    p = tmp_path / "stores.json"
    p.write_text(json.dumps({"yak": {
        "org_id": 1, "lsp_id": 2, "loc_id": 3, "register_id": 4,
        "username": "u", "password": "p"}}))
    monkeypatch.setenv("BUDTENDER_STORES", str(p))
    stores = load_stores()
    assert "yak" in stores
    s = stores["yak"]
    assert s.register_id == 4 and s.pos_base_url == "https://ash.pos.dutchie.com"
