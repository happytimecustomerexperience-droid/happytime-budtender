"""An under-21 admission made earlier in a website chat session keeps the retail decline.

The shared brain (voice service) declines the turn that SAYS "I'm 19", but the website bridge sends it
one message at a time with no session key (voice only accepts its own token shape), so the next turn
("ok show me flower") looks like a fresh adult. This module reads the session's own stored user turns
(``ChatMessage`` rows the server wrote - never anything the client sends now) and, for a SHOPPING ask
only, answers with the same decline the phone/text brain uses. General questions (hours, returns)
still go to the brain: the decline promises "happy to answer general questions about the store".

Mirrors ``voice/voice/chat.py`` (``_SELF_UNDERAGE_RE`` / ``_session_declared_underage``): only a
FIRST-PERSON admission sticks ("my friend who's 19" does not). Stores nothing and asks for nothing:
the flag is derived on the fly from messages already kept, and the decline text is fixed. The text is
``voice/voice/safety_copy.UNDER_21``; ``tests/test_age_gate.py`` fails if the two drift.
"""
from __future__ import annotations

import datetime
import re

from .intents import _PRODUCT_NOUN_RE, _SHOP_VERB_RE

UNDER_21_DECLINE = (
    "We can only sell to customers who are 21 or older with a valid ID, so I can't put an "
    "order together or recommend anything here. I'm still happy to answer general questions "
    "about the store."
)

# Ages 10-20 only (21+ is an ordinary customer); spelled ages too; "20 minutes away" is not an age.
_AGE = (
    r"(?:1\d|20|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty(?![\s-]*(?:one|two|three|four|five|six|seven|eight|nine)\b))"
)
_NOT_A_QUANTITY = (
    r"(?!\s*-?\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?|miles?|blocks?|bucks|dollars|"
    r"percent|mg|grams?|g\b|away|out\b))"
)
_ADVERB = r"(?:(?:only|just|barely|merely|still|like|actually|honestly|already)\s+)?"
_SELF_UNDERAGE_RE = re.compile(
    r"\b(?:i'?m|i\s+am)\s+" + _ADVERB + _AGE + r"\b" + _NOT_A_QUANTITY + r"|"
    r"\bi\s+(?:just\s+)?turned\s+" + _AGE + r"\b" + _NOT_A_QUANTITY + r"|"
    r"\bi'?m\s+turning\s+(?:21|twenty[-\s]?one)\b|"
    r"\b(?:i'?ll|i\s+will|i'?m\s+gonna|i'?m\s+going\s+to)\s+be\s+(?:21|twenty[-\s]?one)\b|"
    r"\bi\s+(?:won'?t|will\s+not)\s+be\s+(?:21|twenty[-\s]?one)\s+until\b|"
    r"\bi'?m\s+(?:under\s*(?:21|twenty[-\s]?one)|underage|not\s+21\s+yet)\b",
    re.I,
)
_BORN_SELF_RE = re.compile(r"(?:\bi\s+was\s+|^\s*)\bborn\b[^.?!\d]{0,30}\b((?:19|20)\d{2})\b", re.I)


def _says_self_underage(text: str) -> bool:
    if _SELF_UNDERAGE_RE.search(text):
        return True
    year_now = datetime.date.today().year
    return any(0 <= year_now - int(m.group(1)) <= 20 for m in _BORN_SELF_RE.finditer(text))


def declared_underage(history) -> bool:
    """Whether any user turn of this session (ChatMessage rows) is a first-person under-21 admission."""
    return any(
        getattr(m, "role", "") == "user" and _says_self_underage(str(getattr(m, "content", "") or ""))
        for m in history
    )


def must_decline(history, message: str) -> bool:
    """True when the session has said it is under 21 AND this turn is a shopping ask."""
    # A product noun or a shopping verb anywhere in the turn - NOT the intent label, which files "any
    # deals on gummies" under specials. Fail closed: an over-decline of a borderline question is cheap.
    asks_to_shop = bool(_PRODUCT_NOUN_RE.search(message or "") or _SHOP_VERB_RE.search(message or ""))
    return asks_to_shop and declared_underage(history)
