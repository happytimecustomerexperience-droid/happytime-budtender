"""Owner-approved safety copy (31-OWNER-SIGNOFF-safety-copy.md).

Every string here is spoken VERBATIM by every channel — text chat (``voice/chat.py``) and the
phone agent (via the prompt block ``voice/provision.py::_with_runtime_safety`` appends). Editable
only in this file; a change here changes what every channel says, and re-requires owner sign-off.
"""

from __future__ import annotations

# _poison_emergency_answer (voice/chat.py) — a child/pet ingestion or possible overdose report.
POISON_EMERGENCY = (
    "This could be an emergency. Please contact your vet, doctor, or emergency services right "
    "away — I'm not able to advise on what to do. "
)

# _cannot_answer_safely_answer (voice/chat.py) — driving/allergen/dosing/medication questions.
CANNOT_ANSWER_SAFELY = "I'm not able to answer that safely myself — I'll get a person on it who can help. "

# _escalation_answer (voice/chat.py) — a return/refund dispute the agent cannot resolve.
DISPUTE = (
    "I'm sorry that happened. I can't confirm a return or refund outcome from the current "
    "Happy Time knowledge base, but I can get the store team involved. "
)

# _under_21_answer (voice/chat.py) — caller is under 21, or won't confirm, or is buying for one.
UNDER_21 = (
    "We can only sell to customers who are 21 or older with a valid ID, so I can't put an "
    "order together or recommend anything here. I'm still happy to answer general questions "
    "about the store."
)

# _specials_answer (voice/tools/faq.py) — no StoreFact(kind="special") row is valid today.
NO_CURRENT_SPECIALS = (
    "We don't have any specials posted right now. Our deals change month to month, so a "
    "budtender in store can tell you what's running today."
)

# faq_lookup's own no-confident-match fallback (voice/tools/faq.py).
FAQ_FALLBACK = "I'm not certain on that one — let me get a team member who can help."

# NEW COPY — owner sign-off. voice/tools/__init__.py::dispatch — the owner switched this tool off
# on /dashboard/capabilities/.
TOOL_DISABLED = "I can't help with that one here right now — a team member at the store can."

# NEW COPY — owner sign-off. voice/api.py::text_chat — the owner switched the website chat off.
CHAT_OFFLINE = (
    "Our website chat is offline right now — please call the store and a budtender will help."
)

# NEW COPY — owner sign-off. voice/chat.py — the caller talks about suicide or self-harm
# (voice/guardrails.py::in_scope reason "crisis"). Wins over every other route; no product tool runs.
CRISIS = (
    "I'm really sorry you're going through this, and I'm glad you said something. If you're "
    "thinking about hurting yourself, please call or text 988 right now to reach the Suicide & "
    "Crisis Lifeline — it's free, confidential, and open 24/7. If you're in immediate danger, "
    "call 911."
)

# NEW COPY — owner sign-off. voice/chat.py::_escalation_answer — a request for a person that is
# not a complaint ("can I talk to someone about a story", "representative"). DISPUTE's
# return/refund wording is a non-sequitur there.
HANDOFF = "I can get someone from the store team to help with that. "
