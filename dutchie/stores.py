"""Store config — env-first (the .env file), stores.json only as a local fallback.

ALL credentials live in environment variables (loaded from .env). Nothing is hardcoded
and no secret is committed. Schema (see .env.example):

    DUTCHIE_STORES=yakima,pullman,mtvernon
    DUTCHIE_BASE_URL / DUTCHIE_POS_BASE_URL          (optional; sane defaults)
    DUTCHIE_ORG_ID / DUTCHIE_LSP_ID
    DUTCHIE_USERNAME / DUTCHIE_PASSWORD              (shared writer login; password
                                                     may be plaintext or enc:v1:)
    STORE_<NAME>_LOC_ID / STORE_<NAME>_REGISTER_ID / STORE_<NAME>_API_KEY
    STORE_<NAME>_USERNAME / STORE_<NAME>_PASSWORD   (optional per-store override)

If DUTCHIE_STORES is unset, falls back to a local stores.json (gitignored) for dev.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .session import Store

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BASE = "https://ash.backoffice.dutchie.com"
_DEFAULT_POS = "https://ash.pos.dutchie.com"


def _load_dotenv() -> None:
    import sys
    if "pytest" in sys.modules or os.environ.get("BUDTENDER_TESTING"):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except Exception:
        pass


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def _int(v: str) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _store_from_env(name: str) -> Store:
    up = name.upper().replace("-", "_")
    return Store(
        name=name,
        base_url=_env("DUTCHIE_BASE_URL", _DEFAULT_BASE),
        pos_base_url=_env("DUTCHIE_POS_BASE_URL", _DEFAULT_POS),
        org_id=_int(_env("DUTCHIE_ORG_ID")),
        lsp_id=_int(_env("DUTCHIE_LSP_ID")),
        loc_id=_int(_env(f"STORE_{up}_LOC_ID")),
        register_id=_int(_env(f"STORE_{up}_REGISTER_ID")),
        username=_env(f"STORE_{up}_USERNAME") or _env("DUTCHIE_USERNAME"),
        password=_env(f"STORE_{up}_PASSWORD") or _env("DUTCHIE_PASSWORD"),
        api_key=_env(f"STORE_{up}_API_KEY"),
    )


def _path() -> Path:
    return Path(os.environ.get("BUDTENDER_STORES") or (_ROOT / "stores.json"))


def _load_json() -> dict[str, Store]:
    p = _path()
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    stores: dict[str, Store] = {}
    for name, cfg in raw.items():
        merged = {"base_url": _DEFAULT_BASE, "pos_base_url": _DEFAULT_POS, **cfg}
        stores[name] = Store(
            name=name, base_url=merged["base_url"], pos_base_url=merged["pos_base_url"],
            org_id=int(merged["org_id"]), lsp_id=int(merged["lsp_id"]),
            loc_id=int(merged["loc_id"]), register_id=int(merged["register_id"]),
            username=merged["username"], password=merged["password"],
            api_key=merged.get("api_key", ""),
        )
    return stores


def load_stores() -> dict[str, Store]:
    names = [s.strip() for s in _env("DUTCHIE_STORES").split(",") if s.strip()]
    if names:
        return {n: _store_from_env(n) for n in names}
    return _load_json()  # local dev fallback


def get_store(name: str) -> Store:
    stores = load_stores()
    if name not in stores:
        raise KeyError(f"store {name!r} not configured (have: {list(stores)})")
    return stores[name]
