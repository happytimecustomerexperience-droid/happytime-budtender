"""``remember_caller`` — save the first name a caller just gave on their budtender profile.

The model passes only a name. The number comes from the call (``ctx['caller_number']``, the carrier's
caller-ID), the write goes to budtender's own customer table (never Dutchie), and the model gets back
only ``{"saved": true|false}``: nothing about the profile, the number or what was already on file.
"""

from __future__ import annotations

import logging

from voice import capabilities
from voice.tools import register

logger = logging.getLogger(__name__)


@register("remember_caller")
def handle_remember_caller(args: dict, ctx: dict) -> dict:
    from voice import caller
    from voice.budtender_client import budtender
    from voice.recognition import normalize_e164

    name = caller.clean_name((args or {}).get("first_name"))
    e164 = normalize_e164((ctx or {}).get("caller_number") or "")
    if not name or not e164 or not capabilities.is_enabled("call.recognize_caller"):
        return {"saved": False}
    out = budtender().profile_upsert(e164, name=name, source="voice")
    # The name on file is budtender's call: it keeps an existing (Dutchie) name over the one given.
    stored = caller.clean_name(out.get("first_name")) if out.get("status") == "ok" else ""
    if not stored:
        return {"saved": False}
    call_id = ctx.get("call_id", "")
    known = caller.cached(call_id)
    if known:  # the agents' next turn reads the new name; with no cached read there is nothing to amend
        known = {**known, "first_name": stored}
        caller.put(call_id, known)
        ctx["caller"] = known
    return {"saved": True}
