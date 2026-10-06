"""What the bots may do — one switchboard the owner controls at /dashboard/capabilities/.

Every capability is DECLARED here: what exactly it lets the bots do, what happens when it is off,
which channels it touches, and its default. The on/off state lives in ``dashboard.BotCapability``
rows; a key with no row uses its declared default, so a fresh install behaves as before.

Code asks ``is_enabled(key)`` (or ``tool_allowed(name)`` for a tool) and nothing else decides.
An unknown key, or a database that cannot be read, answers False (fail closed) and logs it —
except the three ``alerts.*`` delivery switches, which read ON when unreadable (see ``_ALERT_KEYS``).
``test_capabilities.py::test_every_capability_is_enforced`` fails if a key is declared here but no
code outside this module checks it — a switch that changes nothing is worse than no switch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PHONE = "phone"
WEBSITE = "website chat"
CONSOLE = "staff playground"


@dataclass(frozen=True)
class Capability:
    key: str
    group: str
    label: str
    does: str  # owner-facing: exactly what it lets the bots do
    when_off: str  # owner-facing: exactly what happens instead
    channels: tuple[str, ...]
    default: bool = True
    tool: str = ""  # the voice.tools TOOL_REGISTRY name this switch gates, if any
    paid: bool = False  # turning it on can spend money (SMS, calls)


CAPABILITIES: tuple[Capability, ...] = (
    # ── Answering ─────────────────────────────────────────────────────────────
    Capability(
        "tool.faq_lookup", "Answering", "Answer store questions from the knowledge base",
        "Answers hours, addresses, phone numbers, ID rules, payment, pickup, purchase limits, "
        "returns, deals and other store questions, using only the rows on the Knowledge base, "
        "Policies and Specials & hours pages.",
        "The bots say they can't confirm that and offer a team member for every store question.",
        (PHONE, WEBSITE, CONSOLE), tool="faq_lookup",
    ),
    Capability(
        "tool.suggest_products", "Answering", "Recommend products",
        "Searches live in-stock inventory and recommends up to three products, each with its "
        "out-the-door price (tax included) and a one-line reason.",
        "The bots don't recommend products; they point shoppers to the online menu or a budtender.",
        (PHONE, WEBSITE, CONSOLE), tool="suggest_products",
    ),
    Capability(
        "tool.check_inventory", "Answering", "Check stock on a named product",
        "Checks live stock for a product the shopper names or just picked.",
        "The bots say they can't check stock right now and offer a budtender.",
        (PHONE, WEBSITE, CONSOLE), tool="check_inventory",
    ),
    Capability(
        "tool.pair_upsell", "Answering", "Offer one add-on",
        "After a shopper picks something, offers at most one complementary item when the "
        "ranking says it fits.",
        "No add-on is ever offered.",
        (PHONE, WEBSITE, CONSOLE), tool="pair_upsell",
    ),
    # ── Orders ────────────────────────────────────────────────────────────────
    Capability(
        "tool.stage_phone_cart", "Orders", "Hold items for pickup",
        "Puts the shopper's picks on hold under their phone number. The hold appears in the POS "
        "'Orders waiting' queue for a budtender to load; no payment is taken.",
        "The bots tell shoppers to order on the online menu or in store; nothing is held.",
        (PHONE, WEBSITE, CONSOLE), tool="stage_phone_cart",
    ),
    # ── Staff, vendors and transfers ──────────────────────────────────────────
    Capability(
        "tool.notify_staff_issue", "Staff & vendors", "Alert staff about a problem",
        "Files complaints, disputes, overcharges, defective-product reports and restock requests "
        "as an escalation, and sends the staff alert (email / Slack / n8n, per the Alerts switches).",
        "The bot still apologizes and asks for details, but no escalation is filed or sent.",
        (PHONE, WEBSITE, CONSOLE), tool="notify_staff_issue",
    ),
    Capability(
        "tool.notify_vendor_callback", "Staff & vendors", "Take vendor callback requests",
        "When a vendor, distributor or driver calls, logs who they are and what they need on the "
        "Vendor callbacks page and alerts receiving to call back within one business day.",
        "Vendors are asked to call back during business hours; nothing is logged.",
        (PHONE, WEBSITE, CONSOLE), tool="notify_vendor_callback",
    ),
    Capability(
        "call.transfer", "Staff & vendors", "Transfer phone calls to a person",
        "The escalation and vendor phone agents can warm-transfer a caller to that store's staff "
        "number (set on Credentials); staff hear a short summary before the caller is connected.",
        "The phone agent never transfers; it takes the details and alerts staff instead.",
        (PHONE,),
    ),
    Capability(
        "call.sms_on_transfer", "Staff & vendors", "Tell staff who's calling on a transfer",
        "When a call is transferred, sends that store's staff one short note: the caller's first "
        "name only if they said it, the last 4 digits of their number, what they want, and whether "
        "they're a known customer. It goes out as a Pushover phone push (needs the Pushover keys on "
        "Credentials), and also to Slack and email when those Alerts switches are on and set up. "
        "It is not a text message: carriers block SMS for cannabis businesses, so real texting "
        "is not available.",
        "No note is sent; staff still hear the spoken summary when they pick up.",
        (PHONE,), default=False, paid=True,
    ),
    # ── Personalization ───────────────────────────────────────────────────────
    Capability(
        "call.recognize_caller", "Personalization", "Recognize returning customers",
        "Looks the caller's phone number up in the customer profiles to tune picks to what they "
        "usually buy. The number is hashed and never read back to the caller.",
        "Every caller is treated as a new customer.",
        (PHONE, WEBSITE, CONSOLE),
    ),
    Capability(
        "call.greet_by_name", "Personalization", "Greet callers by first name",
        "The phone agent opens with \"Welcome back to Happy Time, <first name>!\" when the caller's "
        "first name is on their profile, every agent may use that name, and a caller whose name we "
        "do not have is asked for it once and it is saved on their profile (never to Dutchie). "
        "Needs the dynamic greeting turned on for the phone line (README: Dynamic greeting rollout).",
        "Every caller gets the standard greeting; the agents never use the name and never ask for it.",
        (PHONE,),
    ),
    # ── Channels ──────────────────────────────────────────────────────────────
    Capability(
        "channel.website_chat", "Channels", "Answer the website chat",
        "The happytimeweed.com chat widget answers through the same brain, knowledge base and "
        "safety rules as the phone agent.",
        "The website chat replies that chat is unavailable and gives the store phone number.",
        (WEBSITE,),
    ),
    # ── Automation ────────────────────────────────────────────────────────────
    Capability(
        "auto.publish_on_save", "Automation", "Publish prompt edits to the phone instantly",
        "Saving an agent prompt or greeting, or flipping a switch on this page, updates the live "
        "Vapi phone assistants within seconds.",
        "Edits are saved but reach the phone only when you press Publish to Vapi.",
        (PHONE,),
    ),
    Capability(
        "auto.deals_sync", "Automation", "Sync deals from Dutchie",
        "Reads every current deal from Dutchie for each store and keeps the Specials & hours rows "
        "in step, so the phone and the website chat quote today's deals.",
        "Deals change only when someone edits them on Specials & hours.",
        (PHONE, WEBSITE, CONSOLE), default=False,
    ),
    Capability(
        "auto.nightly_drift_check", "Automation", "Nightly website check",
        "At 3 AM compares the hours, addresses and phone numbers on happytimeweed.com with the "
        "knowledge base and alerts staff if they disagree.",
        "No nightly check runs.",
        (),
    ),
    # ── Alerts ────────────────────────────────────────────────────────────────
    Capability(
        "alerts.email", "Alerts", "Email staff alerts",
        "Escalations, vendor callbacks and nightly-check problems are emailed to the store "
        "alert addresses set on Credentials.",
        "No alert emails; alerts are only visible on the dashboard.",
        (),
    ),
    Capability(
        "alerts.slack", "Alerts", "Post staff alerts to Slack",
        "The same alerts are posted to the Slack webhook set on Credentials.",
        "Nothing is posted to Slack.",
        (),
    ),
    Capability(
        "alerts.n8n", "Alerts", "Send staff alerts to n8n",
        "The same alerts are sent to the n8n webhook set on Credentials, for any automation "
        "you build there.",
        "Nothing is sent to n8n.",
        (),
    ),
)

BY_KEY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}
TOOL_KEY: dict[str, str] = {c.tool: c.key for c in CAPABILITIES if c.tool}

_CACHE_KEY = "capabilities:v1"
_CACHE_SECONDS = 30

# The staff-alert delivery switches. Unlike every other key, an UNREADABLE state reads ON for these:
# an outage of the cache or database must not hide a staff alert, and a delivery recorded "skipped"
# is never retried. Only an explicit OFF row the owner saved turns them off.
_ALERT_KEYS = frozenset({"alerts.email", "alerts.slack", "alerts.n8n"})


def states() -> dict[str, bool]:
    """``{key: enabled}`` for every declared capability (cached briefly; cleared on every save).
    The cache is only a speed-up: when it cannot be read or written the table is read instead."""
    from django.core.cache import cache

    try:
        cached = cache.get(_CACHE_KEY)
    except Exception:  # noqa: BLE001 — a cache outage must not hide the owner's saved switches
        logger.warning("capabilities cache unreadable — reading the table", exc_info=True)
        cached = None
    if isinstance(cached, dict):
        return cached
    from dashboard.models import BotCapability

    rows = dict(BotCapability.objects.values_list("key", "enabled"))
    out = {c.key: bool(rows.get(c.key, c.default)) for c in CAPABILITIES}
    try:
        cache.set(_CACHE_KEY, out, _CACHE_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("capabilities cache unwritable", exc_info=True)
    return out


def is_enabled(key: str) -> bool:
    if key not in BY_KEY:
        logger.error("capability %r is not declared — treated as OFF", key)
        return False
    try:
        return states()[key]
    except Exception:  # noqa: BLE001 — see _ALERT_KEYS: alerts fail open, everything else fails closed
        if key in _ALERT_KEYS:
            logger.exception("capability state unreadable — %s treated as ON (a staff alert must not be hidden)", key)
            return True
        logger.exception("capability state unreadable — %s treated as OFF", key)
        return False


def tool_allowed(tool_name: str) -> bool:
    """A tool with no switch is always allowed; a switched tool follows its switch."""
    key = TOOL_KEY.get(tool_name)
    return True if key is None else is_enabled(key)


def set_enabled(key: str, enabled: bool, *, by: str = ""):
    """Persist one switch and drop the cache so every worker sees it on the next request. Returns
    the saved ``BotCapability`` row (its ``publish_note`` says what the save signal published)."""
    if key not in BY_KEY:
        raise KeyError(key)
    from django.core.cache import cache

    from dashboard.models import BotCapability

    row, _ = BotCapability.objects.update_or_create(
        key=key, defaults={"enabled": bool(enabled), "updated_by": (by or "")[:150]}
    )
    cache.delete(_CACHE_KEY)
    return row
