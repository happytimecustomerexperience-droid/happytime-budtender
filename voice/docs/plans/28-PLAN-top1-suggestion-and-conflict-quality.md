# 28 — Top 1% Conflict Resolution + Suggestion Quality Plan

## Success criteria (ground truth)

**Objective:** every answer and recommendation must be grounded, safe, and defensively excellent for store-facing customer support.

### A) Conflict resolution and escalation quality
1. **Never invent policy**: for return/refund/age/compliance items, if no KB row exists with sources, response must be escalatable and clearly say staff follow-up is required.
2. **Tone + safety gate**: any conflict/defective complaint response must include empathy (`sorry`) and one of `escalate`/`transfer_to_staff` actions.
3. **Escalation contract**: every escalation path must include store context and customer contact hint when available.
4. **No hallucination check**: conflict answer can only include policy/legal claims that are from KB sources.

### B) Suggestion quality
5. **Customer match quality**: top 3 suggestions must be influenced by known purchase history, category affinities, and current session intent.
6. **Reasoning clarity**: each suggestion includes `why_this` with one clear customer-facing reason and no opaque margin/cost reasoning.
7. **Constraint compliance**: filters and exclusions (`exclude_skus`, budget, dosage/strain preference, category intent) are enforced before ranking.
8. **Diversity + freshness**: top 3 should not repeat identical brand/item family unless evidence is strong.
9. **Relevance stability**: repeated prompts from same session should not oscillate wildly unless context changed.

### C) Measurement gates
10. **Dialog matrix**: 20+ end-to-end scenarios with expected escalation flags, grounded flags, and source usage.
11. **Conflict suite**: at least 10 cases (angry, wrong item, defective, missing item, rude escalation, policy edge cases).
12. **Suggestion suite**: at least 12 cases (known customer, anonymous customer, contradictory request, budget-only, exclude list, do-hash only, vaporizer edge cases).

### D) Acceptance rubric (numeric)
13. **Grounding reliability:** on policy/legal/return/age questions, 0 source-less claims accepted as grounded (target 0%).
14. **Conflict safety:** 100% of conflict escalations include both empathy and an explicit escalation step.
15. **Suggestion explainability:** 100% of non-empty recommendations include non-empty `why_this`, no raw pricing keys (`price`, `price_was`, `cost`, `margin`) in tool-visible text.
16. **Personalization fidelity:** if `session_token`/`customer_phone` are present, they must be forwarded to suggestion tool calls and reflected in suggestion result metadata/log.
17. **Escalation fallback correctness:** ambiguous/no-KB conflict questions must go to `ask_staff` (not `answer`) unless KB source exists.

## Current state (as implemented)

**Confidence baseline:** strong, but not yet top-1.

- Conflict handling has stronger escalation and non-invented policy behavior for human/defect paths.
- Suggestions already use customer profile signals (`category/brand/subcategory/affinity`), explicit exclusions, and cleaned `why_this` copy.
- Text and voice now share one grounded tool path for policy/specials/hours/legal/conflict intents.
- Draft quality gaps remain: no dedicated “quality KPI dashboard” for these flows, no sustained golden-dataset review loop, and no CI-visible scorecards yet.

### Current-state evidence
- Source-grounded policy enforcement was added in `[voice/voice/chat.py]` via `_requires_sources(...)`.
- New quality gates were added in `[voice/tests/test_text_chat_quality_gates.py]`.
- Golden matrix test scaffold was added in `[voice/tests/test_top1_quality_matrix.py]` (28 seeded cases in `[voice/tests/data/top1_quality_matrix.json]`).
- Existing product-context forwarding and conflict coverage remains in `[voice/tests/test_text_chat_and_scrape.py]` and `[voice/voice/tests/test_suggest_tools.py]`.
- `profile_forwarding` now sets `known_caller: true` so matrix scorebook proves known-caller tool-context forwarding.

## 30-day plan to reach top-1% quality

### Phase 1 (Week 1): harden confidence
1. Add a golden dataset for conflict flows and suggestion flows (stored fixtures).
2. Add parity tests to prove text/voice outputs match on non-voice-specific actions.
3. Add explicit source-presence assertions for every policy/legal/age/refund answer.

### Phase 2 (Week 2): improve recall + precision
4. Expand profile signals used at ranking time with session recency weighting and "recent bad-match penalty."
5. Add explicit anti-duplication in recs (similar strain lineage/subcategory collapse).
6. Add low-confidence fallback for recommendations with safer alternatives.

### Phase 3 (Week 3): add human-loop quality control
7. Track disagreement between chat suggestions and manual override as signal.
8. Add a small operator feedback column in dashboard (liked / skipped / bad reason).
9. Build a monthly scrape+seed sanity check for policy rows and specials row completeness.

### Phase 4 (Week 4): tighten operations
10. Add regression gates in CI for:
   - no-policy-guess tests for conflict paths,
   - grounded-contains-source tests for policy claims,
   - suggestion schema tests (`why_this`, `exclude_skus`, ranking stability).
11. Review top 10 real interactions weekly and promote fixes into ranking logic.

### Implementation priority (next 72h)
1. Add a golden dialog JSON pack for regression replay.
2. Add a weekly scorebook (pass/fail per criterion D13-D17) tracked in dashboard or CI artifacts.
3. Expand the matrix to 20+ non-regression-conflicting suggestion/conflict coverage and enforce growth in CI.
4. Add a “known vs anonymous path” marker in matrix rows and require both paths on every release run.

## Top risks
- KB drift from scrape gaps causing grounded claims to fail.
- Suggestion copy quality drifting when ranking changes.
- Over-indexing on short-session signals without enough purchase history.

## Current measurable state (as of this turn, 2026-07-09)
- **Live regression suites passing**:
- `tests/test_top1_quality_matrix.py` (now 30 passing checks from 28 matrix scenarios),
  - `tests/test_text_chat_quality_gates.py` (includes staff contact handoff assertions),
  - `tests/test_text_chat_and_scrape.py`,
  - `voice/voice/tests/test_suggest_tools.py`.
- **Focused verification on 2026-07-09**:
  - `cd voice; python -m pytest tests/test_top1_quality_matrix.py tests/test_text_chat_quality_gates.py voice/tests/test_suggest_tools.py -q` -> 60 passed.
  - `cd voice; python manage.py check` -> no issues.
  - `SQL_ENGINE=django.db.backends.sqlite3 SQL_DATABASE=:memory: python -m pytest pos/tests/test_personalization.py budtender/tests/test_engine.py budtender/tests/test_product_search_contract.py -q` -> 30 passed.
  - `SQL_ENGINE=django.db.backends.sqlite3 SQL_DATABASE=:memory: python manage.py check` -> no issues.
- **Conflict safety**: policy/legal/age/rule queries without KB sources are now forced into non-grounded outcomes and never return fabricated policy text.
- **Suggestion quality**: context forwarding is stable for profile/phone/session and exclusion constraints, with leak-safe pick shaping.
- **Profile fidelity**: matrix now explicitly tracks and validates both known and anonymous suggestion-call paths.
- **Scorecard**: `TOP1_SCORECARD_PATH` is now persisted by `tests/test_top1_quality_matrix.py`; CI can use it directly for regression budgets.
- **Current scorebook outcome**: `escalations=6`, `escalation_empathy=6`, `policy_safe_grounding=6`, `suggestion_paths=12`, `seen_known=True`, `seen_anon=True` on the latest fixture run (including both known and anonymous path coverage).
- **Natural-language constraint parsing**: the shared brain now infers edible/gummy, medical/DOH-only, simple budget caps such as "under 25", effect intent, indica/sativa/hybrid subcategory, and common size/dose terms like "1g", "eighth", and "10mg" before calling `suggest_products`.
- **New top-1 contract now enforced**: ask-staff / escalate flows emit `contact_hint` and staff-followup text in the response body; this closes operational handoff data drift.

### Operational definition of top-1 success

Top-1 is considered achieved when all of the following are true at release time:
1. Current scorebook gates are green (policy-safe grounding, escalation empathy, suggestion paths, known+anon coverage).
2. No production-path policy/legal/age/returns question returns grounded content without `sources`.
3. Every escalation/ask-staff response includes a staff-followup phrase and either a detected store or a normalized contact hint.
4. Recommendation tool calls are called with normalized profile context when present (`known`, `_caller_phone`, `session_token`, `caller_phone_hash`).
5. New matrix cases added when a regression is seen, with weekly scorebook trend review before merge.

### Scorecard gates now in use
- Enforced top-1 thresholds:
  - `policy_safe_grounding >= 5`
  - `escalation_empathy == escalations`
  - `suggestion_paths >= 12`
  - `seen_known == true` and `seen_anon == true`

## Top 1% target outcome
When complete, conflict prompts should almost always be:
- either grounded with a cited KB source, or
- escalated with empathy and clear staff handoff.

Suggestion prompts should return top candidates that are:
- visibly relevant,
- auditable (`why_this`),
- personalized,
- and less likely to produce a repeat bad-match complaint.

## Owners and rhythm
- **Owner:** `voice` team for KB, routing, and ranking contracts.
- **Owner:** `dashboard/budtender` for product endpoint behavior.
- **Owner:** `CRM` for profile integrity and edge-case data fixes.
- **Rhythm:** weekly scorecard on the above gates; block deploy if any gate fails.
