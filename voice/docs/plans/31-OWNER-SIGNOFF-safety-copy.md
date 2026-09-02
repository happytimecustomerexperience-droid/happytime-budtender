# Owner sign-off — safety copy the agent speaks

**Status: AWAITING OWNER APPROVAL.** Both strings below are live in production today. They were
written by an AI assistant, not by you and not by a compliance advisor. They are deliberately
conservative — they diagnose nothing and promise nothing — but *what a cannabis retailer's agent
says to someone in a medical or emergency situation is your call and your licence*, so they need a
human yes.

Nothing here needs a code change to approve. Each is a single string in `voice/voice/chat.py`.
To edit: change the string, run `uv run pytest -q`, deploy. To approve: tick the box.

---

## 1. Ingestion emergency — a child or pet has eaten cannabis

**Where:** `_poison_emergency_answer` in `voice/voice/chat.py`
**Fires on:** a pet/child/toddler + ate/swallowed/got into, plus overdose / poison / won't wake up /
unresponsive / ER / 911 phrasings.

> "This could be an emergency. Please contact your vet, doctor, or emergency services right away
> — I'm not able to advise on what to do."
>
> …followed by the store contact hint.

**Why it is worded this way**
- Names no substance, dose, timeline or symptom — the agent has no way to assess any of them.
- Does **not** say the animal or child will be fine. Reassurance would be the most dangerous
  possible output.
- Points at a human who *can* assess: vet, doctor, emergency services.
- Does not tell them to come to the store, which would waste time in an emergency.

**What it replaced:** before this, "my dog just ate one of the edibles" was answered with a
**product pitch** — "My top pick is the Cannaquench Sparkling 5mg…" — because the word "edibles"
matched the category regex before any safety check existed.

**Worth your consideration:** should this name **Poison Control (1-800-222-1222)** and/or the
**ASPCA Animal Poison Control line (888-426-4435)** explicitly? That is more useful to a panicking
caller than "contact your vet". I did not add specific phone numbers on my own initiative, because
a wrong or out-of-date emergency number is worse than none. Your call.

- [ ] **Approved as written**
- [ ] **Approved with edits:** _______________________________________________

---

## 2. Impaired driving / allergens — questions the agent must not answer

**Where:** the driving/allergen escalation branch in `voice/voice/chat.py`
**Fires on:** "is it ok to drive after one gummy", "how long until I can drive", "does the chocolate
have nuts", "is it gluten free", allergy/allergen phrasings.

> "I'm not able to answer that safely myself — I'll get a person on it who can help."
>
> …followed by the store contact hint.

**Why it is worded this way**
- No dose, no timing, no legality ruling, no reassurance.
- Does not claim the product *is* or *is not* safe, and does not guess at ingredients.
- "safely" signals *why* it is declining without lecturing the caller.

**What it replaced:** these questions escalated correctly but reused the **dispute** copy — "I'm
sorry that happened. I can't confirm a return or refund outcome from the current Happy Time
knowledge base…" — a non-sequitur for a health question. Nothing had been returned or refunded.

**Worth your consideration:** for allergens specifically, would you rather it say "check the
packaging, or ask a budtender who can read the label with you"? That is more actionable than a
generic handoff, and it is a factual instruction rather than an ingredient claim. I did not assume
it, because it implies the packaging is reliable for allergens and I cannot verify that.

- [ ] **Approved as written**
- [ ] **Approved with edits:** _______________________________________________

---

## Categories that still have NO safety branch

Stated plainly so the gaps are yours to weigh, not hidden:

| Situation | Today's behaviour |
|---|---|
| Interstate transport ("can I take it to Idaho") | Answers from the KB's real "stays in WA" row. Escalation was tried and **reverted** — it turned a correct citable answer into a handoff. The residual defect is retrieval sometimes landing on the return-policy row. |
| Pregnancy / breastfeeding | Answers with the state health-warning row. Deliberately not escalated — `test_thread_17` pins that as acceptable. Worth a second opinion. |
| Someone who seems intoxicated or incapacitated | No detection. Arguably undetectable in text. Documented, not solved. |

---

## 3. 2026-09-02 additions (awaiting approval)

The lines below already ship in text chat (`voice/voice/chat.py`) or the FAQ tool
(`voice/voice/tools/faq.py`) and now also live as named constants in `voice/voice/safety_copy.py`
so the phone agent's provisioned prompt (`voice/voice/provision.py::_with_runtime_safety`) speaks
the identical sentence.

**`UNDER_21`** — caller is under 21, or won't confirm, or is buying for someone who is:

> "We can only sell to customers who are 21 or older with a valid ID, so I can't put an order
> together or recommend anything here. I'm still happy to answer general questions about the
> store."

**`NO_CURRENT_SPECIALS`** — the KB holds no `StoreFact(kind="special")` row valid today:

> "We don't have any specials posted right now. Our deals change month to month, so a budtender
> in store can tell you what's running today."

**The `specials` FAQ row** (`voice/kb/seed.py`, evergreen pointer — not a safety_copy constant,
quoted here for completeness since it is the sibling of `NO_CURRENT_SPECIALS`):

> "Our deals change month to month. Tell me which store you're shopping at and I'll tell you what's
> running right now, or ask a budtender in store and they'll walk you through the current offers."

**The three `_stock_check_reply` lines** (`voice/voice/chat.py`, not moved to `safety_copy.py` —
templated with live tool output, not fixed sentences):

> "I can't confirm that specific item is in stock right now. A team member can check the shelf for
> you if you share the best way to reach you."

> "Yes — the {item} is {band} at the moment."

> "The {item} isn't showing as in stock right now. I can help you find something similar."

**The `_pair_upsell_reply` no-offer line** (`voice/voice/chat.py`, spoken when `pair_upsell`'s own
strength gate says stay quiet):

> "Nothing jumps out as a natural add-on for that one. Tell me what else you're after and I'll take
> a look."

Voice now speaks §1/§2/§3 verbatim via the provisioned prompt (voice/voice/safety_copy.py);
approving here approves every channel.

**Open question:** the text channel does not yet say "your doctor or pharmacist" for medical
questions, while the voice prompt's `NO_MEDICAL_CLAIMS` line does. Owner's call whether text should
match, or voice should be relaxed to match text.

- [ ] **Approved as written**
- [ ] **Approved with edits:** _______________________________________________

---

## Who should review this

The wording is conservative enough that I do not think it needs a lawyer. But the **ingestion**
line is the one where a compliance advisor's opinion would be worth the hour — it is the only
string here that fires during an actual emergency, and it is the only one where the alternative
(naming Poison Control directly) is a real, defensible improvement I chose not to make unilaterally.
