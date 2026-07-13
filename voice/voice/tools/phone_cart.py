"""``stage_phone_cart``: staged phone-order intent, not checkout.

The handler is deliberately transport-only. It sends cart intent to the budtender/POS
service and never imports Dutchie/POS register clients.
"""

from __future__ import annotations

from voice.budtender_client import budtender
from voice.tools import register

_ACTIONS = {"add_item", "remove_item", "set_quantity", "quote", "release"}


def _store(args: dict, ctx: dict) -> str:
    store = str(args.get("store") or ctx.get("store") or "").strip()
    return store if store in {"yakima", "mount-vernon", "pullman"} else "yakima"


def _qty(value) -> int:
    try:
        return max(1, min(99, int(value)))
    except (TypeError, ValueError):
        return 1


def _summary(action: str, out: dict) -> str:
    if not out.get("ok"):
        return "I could not stage that cart change right now. A team member can help finish it."
    draft = out.get("draft") or {}
    quote = draft.get("quote") or {}
    token = draft.get("draft_token") or ""
    try:
        total = float(quote.get("total"))
    except (TypeError, ValueError):
        total = None
    try:
        discounts = float(quote.get("discounts") or 0)
    except (TypeError, ValueError):
        discounts = 0.0
    if action == "release":
        return (
            f"I released the staged phone cart for the register. The pickup token is {token}. "
            "Staff will verify ID, availability, discounts, and the final total before checkout."
        )
    if action == "quote" and total is not None:
        if discounts:
            return (
                f"The current staged estimate is ${total:.2f} after about ${discounts:.2f} in visible discounts. "
                "The register confirms the final total."
            )
        return f"The current staged estimate is ${total:.2f}. The register confirms the final total."
    return "I updated the staged cart. The register will confirm availability, discounts, and final total."


@register("stage_phone_cart")
def handle_stage_phone_cart(args: dict, ctx: dict) -> dict:
    args = args or {}
    ctx = ctx or {}
    action = str(args.get("action") or "").strip()
    if action not in _ACTIONS:
        return {"ok": False, "error": "unknown_action"}

    payload = {
        "action": action,
        "store": _store(args, ctx),
        "call_id": str(args.get("call_id") or ctx.get("call_id") or "")[:80],
        "session_token": str(args.get("session_token") or ctx.get("session_token") or "")[:80],
        "phone": str(ctx.get("_caller_phone") or ctx.get("caller_number") or "")[:40],
        "pickup_name": str(args.get("pickup_name") or "")[:120],
        "audit": {
            "source": "voice_tool",
            "tool_call_id": str(ctx.get("tool_call_id") or ""),
        },
    }
    if args.get("draft_token"):
        payload["draft_token"] = str(args["draft_token"])[:64]
    if args.get("sku"):
        payload["sku"] = str(args["sku"])[:64]
    if "quantity" in args:
        payload["quantity"] = _qty(args.get("quantity"))

    if action == "release":
        out = budtender().phone_cart_release(payload)
    else:
        out = budtender().phone_cart_upsert(payload)
    return {**out, "spoken_summary": _summary(action, out)}
