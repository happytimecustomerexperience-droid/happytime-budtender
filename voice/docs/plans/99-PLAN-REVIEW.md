# 99 — PLAN REVIEW — Happy Time Voice-Agent Plan Set (FRESH COVERAGE AUDIT, full set)

> **Status:** REVIEW (execution-readiness gate). Re-written 2026-06-22 by the coverage reviewer, against the COMPLETE plan set now on disk.
> **Scope reviewed (19 files, all read in full):** foundation `00`–`03`; executable phase docs `10-P0` · `11-P1` · `12-P2` · `13-P3` · `14-P4` · `15-P5` · `16-CAPABILITY-EXPANSIONS`; cross-cutting specs `20-SPEC-vapi-deploy` · `21-SPEC-budtender-contract` · `22-SPEC-kb-seed` · `23-SPEC-security-guardrails` · `24-SPEC-testing`; research `_research-suggestion-engine` · `_research-education-blogs`.
> **Verdict in one line:** the plan set is now **fully authored and genuinely execution-ready** — the four BLOCKER gaps from the prior review (`10`–`13` missing) are closed, the `2X` specs exist and are exhaustive, and every required capability/weakness/subsystem is specified to a file-by-file + data-contract + acceptance + test-plan + Vapi-deploy bar. A small set of **non-blocking cross-doc inconsistencies** remain (a spec filename reference, a webhook field-name drift, two stale ADR-label references). **GO for Wave 0 (P0).**

---

## 0. What changed since the prior review

The prior `99` (same date, earlier pass) found the program **NOT executable** because `10`–`13` and `20`–`24` did not exist. **All of them now exist on disk** and are authored to the `14`/`15` standard (or above). Re-verifying the prior gap list:

| Prior gap | Was | Now |
|---|---|---|
| G-1 `10-P0-CHASSIS-FAQ.md` | 🔴 missing | ✅ present, 510 lines — fork gate (§0), file-by-file, the frozen webhook contract (§4), KB-seed map (§4.7), acceptance A–H, 4-plane test plan |
| G-2 `11-P1-DUTCHIE-SUGGESTIONS.md` | 🔴 missing | ✅ present — `budtender_client`, 3 tool handlers, recognition flow + the Option-A ADR, Leak-Guard gate, manual script |
| G-3 `12-P2-ESCALATION-TRANSFER-EMAIL.md` | 🔴 missing | ✅ present — eocr handler, `crm/sinks.py` port, escalation prompt + warm transfer, outcome classifier, `AlertDelivery` idempotency |
| G-4 `13-P3-VENDOR-ROUTING.md` | 🔴 missing | ✅ present — entry_router classifier, `vendor` member, `notify_vendor_callback`, `VendorCallback`, warm-transfer→no-answer→capture |
| G-5 `20`–`24` specs | 🟠 none exist | ✅ all five present and exhaustive (`20` Vapi-deploy, `21` budtender-contract, `22` KB-seed, `23` security-guardrails, `24` testing) |
| G-6 embeddings spec | 🟠 none | ✅ `22-SPEC` §4 (rank_faq/reindex/keyword-fallback/pgvector-seam) + acceptance B1–B5 |
| G-7 KB-seed content map | 🟡 missing | ✅ `22-SPEC` §7 section→model→rows table + §8 literal rows |
| G-9 ADR-021 (Celery) | 🟡 unwritten | ✅ `02` ADR-021 authored |
| G-11 P0 fork-gate sequencing | 🟢 implicit | ✅ `10-P0` §0 "the fork gate" explicit |

The prior G-8/G-10/G-12 (stale `01` layout, `16` filename, read-order index) are partly addressed and now reclassified below as the low-severity residue.

---

## 1. Scorecard

Scores are 0–5. **Coverage** = how much of the required surface is specified; **Quality** = executable depth. Both are now high; the program is gated only on the small residue in §8.

| # | Review dimension | Score (/5) | One-line justification |
|---|---|---|---|
| **1** | **Placeholders / TBD / empty sections** | **4.5** | No lazy `TBD`/`TODO`/`FIXME`/`???` in any executable doc. Owner-unknowns are correctly modeled as named env placeholders (O-1…O-10, O-E*) with a per-doc graceful-degradation rule. Legitimate residue: `15` `brand/tokens.json` `#______` hex (intentional, provisional-gated); `16` E8/E19 "verify Dutchie write/read surface" (a *backlog* open-q, correctly flagged); `22` education rows `provisional=True` (Vercel wall). None block execution. |
| **2** | **Contradictions (vs 02-DECISIONS / cross-doc)** | **4** | No ADR is contradicted. Four real but non-blocking cross-doc inconsistencies: (CN-1) `22` refers twice to `21-SPEC-webhook-contract.md` which does not exist (the file is `21-SPEC-budtender-contract.md`; the webhook/HMAC contract actually lives in `10-P0` §4 + `23-SPEC`); (CN-2) webhook `tool-calls` field name drifts — `10-P0`/`13-P3` use `toolCalls`, `24-SPEC` fixtures use `toolCallList`; (CN-3) `11-P1` §3.4/§9 still cite "ADR-021" for the recognition decision, but `02` renumbered it to **ADR-022** (ADR-021 is now Celery) — `02` flags the supersede, `11` was not updated; (CN-4) `00`/`01` folder layout and the roadmap §7 spec checklist are stale vs the files that actually shipped (`23`/`24` exist but §7 lists generic names; many `voice/`/`dashboard/` modules added by `14`/`15`/`16`/`20`–`23` are absent from `01` §2). |
| **3a** | **Coverage of the 7 owner capabilities (execution-ready)** | **5** | All 7 now have a full executable doc, not just architecture. C1 FAQ-KB → `10-P0` + `22-SPEC`; C2 dashboard → `14-P4`; C3 Dutchie suggestions+personalization → `11-P1` + `21-SPEC`; C4 vendor → `13-P3`; C5 de-escalation → `12-P2`; C6 staff email → `12-P2`; C7 branding → `15-P5`. **Execution-ready: 7/7.** |
| **3b** | **Coverage of the 12 export weaknesses (execution-ready)** | **5** | All 12 mapped AND each now lands in an executable doc with an acceptance criterion. #1 tools-bound + #5 FAQ + #9 durable-log → P0; #2 Dutchie → P1; #3 escalation-orphan + #10 defective-path → P2; #6 vendor → P3; #7 config-dup + #8 single-model + #11 unhydrated-vars → P4/`20-SPEC` by construction (no-dup test, single `gpt-4.1-mini`, per-phone overrides); #4 cartridge + #12 back-edges → P5. **Execution-ready: 12/12.** |
| **4** | **Executable-doc completeness (file tasks + data contracts + acceptance + test plan + Vapi deploy)** | **5** | Every executable doc (`10`–`15`, `20`–`24`) carries: a file-by-file table with exact paths + responsibility + key functions + **port-from path:line**; JSON/Python data contracts; lettered acceptance criteria; a 4-plane test plan with named test files; explicit Vapi `PATCH`/provision steps; risks; DoD with the doc-protocol close-out. `16` is a disciplined value÷effort backlog with per-item contracts + cross-cutting enablers. This is at or above the `03` §6 bar. |
| **5** | **Specificity of the 4 "must-be-genuinely-specified" subsystems** | **5** | **Budtender margin+personalization ranking:** fully specified — `21-SPEC` freezes the wire contract (W_ANON/W_KNOWN switch = presence of identity, the slot-#1 margin pin, the strength gate) + `11-P1` builds the consumer; the real phone-hash-vs-raw-phone boundary mismatch is *resolved* (Option A, ADR-022), not papered over. **Education/blogs/weights KB:** `22-SPEC` §8 writes every literal row (8 FAQs, the WAC-314-55-079 policy body, ~14 store-facts, 5 specials, 4 WA limits, the full taxonomy across 9 axes, 5 education + 3 blog rows, the persona prompt) + §7 the section→model→rows map. **Embeddings engine:** `22-SPEC` §4 gives rank_faq/reindex signatures, the content-hash cosine cache, the keyword fallback, the pgvector seam, and acceptance B1–B5. **Full+expanded dashboard:** `14-P4` exhaustive (11 surfaces, publish mapping, `_clean_graph`, weights tuner). **All 4 are execution-ready.** |
| **6** | **Dependency / sequencing sanity (waves + P0 fork-gate)** | **4.5** | The wave graph (roadmap §4/§6), the P0 fork-gate (`10-P0` §0 — P1/P2/P3 do not fork until the webhook contract + `vapi.py` + `voice/tools/` registry + `VoiceCall` land green), the disjoint-file parallelism (P1=`suggest.py`, P2=`webhooks.py` eocr + sinks, P3=`vendor.py`), and the one-line-append mitigation for `voice/tools/__init__.py` are all coherent and now point at real nodes. P4-serial-after-P1–P3, P5-last, EXP-independent are correct. The `20-SPEC` reconciliation of the two provisioner names (`tools/provision_vapi.py` shim → `voice/provision.py`) is explicitly handled. Half-point off only for the residual sequencing-citation drift (CN-1/CN-4). |
| | **OVERALL PROGRAM READINESS** | **4.7 / 5** | Foundation + every phase + every spec is authored to a high, internally-consistent bar; the security spine (Leak-Guard / HMAC-fail-closed / Numbers-Guard / OTD / per-store-keys-in-budtender-only / prod-fail-closed) is enforced uniformly and tested as two mandatory gates. Ready to execute Wave 0. The remaining items are cosmetic/cross-reference cleanups that do not block any build. |

---

## 2. Capability-coverage matrix (the 7 owner capabilities — now execution-ready)

| # | Owner capability | Executable doc(s) | Data contract | Acceptance + tests | Status |
|---|---|---|---|---|---|
| C1 | FAQ + return-policy KB (grounded) | `10-P0` §3.4/§4/§5 + `22-SPEC` (full) | `faq_lookup` result (`10-P0` §4.3 / `22` §5); KB models (`22` §3) | `10-P0` C/D/E; `22` A–G; gates: leak + grounded-no-invent | ✅ READY |
| C2 | Editable dashboard | `14-P4` (full) | `build_*_payload`, `PublishResult`, `RankingWeights` (`14` §4) | `14` A–H; staff-gate sweep | ✅ READY |
| C3 | Dutchie high-margin + personalized suggestions | `11-P1` + `21-SPEC` (full) | budtender HTTP contract (`21` §5); tool arg schemas (`11` §4) | `11` A–H; `21` A–H; **Leak-Guard gate** | ✅ READY |
| C4 | Vendor detect / transfer / no-answer callback | `13-P3` (full) | classifier output + `notify_vendor_callback` + `VendorCallback` (`13` §4) | `13` A–H; warm-transfer-first invariant test | ✅ READY |
| C5 | Problem resolution / de-escalation | `12-P2` (full) | eocr payload + `classify_outcome` precedence (`12` §4) | `12` A–G; defective/dispute/repeated matrix | ✅ READY |
| C6 | Staff email alerts | `12-P2` §3.2/§4.5 (full) | `AlertDelivery` idempotency + email body (`12` §4.2/§4.5) | `12` D/E; idempotent-on-`vapi_call_id`; record-survives-email-failure | ✅ READY |
| C7 | Branding | `15-P5` §3.1 (full, + capture runbook) | `brand/tokens.json` (`15` §4.1) | `15` AC-3; provisional fallback documented | ✅ READY |

**Execution-ready: 7/7.**

---

## 3. Export-weakness coverage matrix (the 12 — now execution-ready)

| # | Export weakness | Fixing phase / spec | Acceptance anchor | Status |
|---|---|---|---|---|
| 1 | Tools named but never bound | P0 + `20-SPEC` | provision attaches `toolIds`; `server.url` on every tool | ✅ READY |
| 2 | No Dutchie inventory / suggestion path | P1 + `21-SPEC` | `11` B1–B4; ≤3 in-stock leak-safe picks | ✅ READY |
| 3 | Escalation orphan + empty transfer destinations | P2 + `20-SPEC` §4.7 | `12` A1/A2 (3 inbound edges, non-empty `destinations`); `20` D2 (orphan unreproducible by construction) | ✅ READY |
| 4 | Cartridge buried under concentrate | P5 | `15` AC-1 | ✅ READY |
| 5 | No FAQ / return-policy knowledge | P0 + `22-SPEC` | `22` D2/D4 (literal rows, WAC literal) | ✅ READY |
| 6 | No vendor path | P3 | `13` A1 (vendor before retail) | ✅ READY |
| 7 | Config duplicated ~51× per node | P4 / `20-SPEC` / ADR-011 | `20` C1 (voice/transcriber/model once; 33 keyterms once) | ✅ READY |
| 8 | Two conflicting models | ADR-010 / `20-SPEC` | `20` C2 (no `gpt-5.2-chat-latest`; single `gpt-4.1-mini`) | ✅ READY |
| 9 | No durable call log (best-effort Slack) | P0/P2 / ADR-017 | `12` D1–D3 (durable write survives sink failure) | ✅ READY |
| 10 | No age/ID + no defective-return path | P2 / ADR-018 / `23-SPEC` | `12` C2; `23` AC-9 (age gate is a code boundary) | ✅ READY |
| 11 | Unhydrated `{{store_name}}` | P4/P0 per-phone overrides | `10-P0` B2 (vars hydrated) | ✅ READY |
| 12 | Strictly-forward graph, no back-edges | P5 | `15` AC-2 | ✅ READY |

**Execution-ready: 12/12.**

---

## 4. The four "genuinely specified?" subsystems — verdict (all READY)

| Subsystem | Decided | Researched | File-task + data contract + acceptance + tests | Verdict |
|---|---|---|---|---|
| **Budtender margin+personalization ranking** | ADR-005/006/007/022 | `_research-suggestion-engine` (W_ANON/W_KNOWN, `_why`, resume-by-phone, strength gate, allowlist) | `21-SPEC` §5/§6/§7 freezes the wire + the selection switch + the handshake; `11-P1` §3/§4 builds the client+tools; the phone-hash-vs-raw-phone mismatch is *resolved* (Option A) with a contract test | ✅ READY |
| **Education / blogs / weights KB** | ADR-012 | `_research-education-blogs` (dosing, WA limits, taxonomy, house style) | `22-SPEC` §3 (six models) + §7 (section→model→rows) + §8 (every literal row) + acceptance D1–D5 | ✅ READY |
| **Embeddings engine** | ADR-013 | `01` §4 + swedish-bot `kb/semantic.py` | `22-SPEC` §4 (rank_faq/reindex/keyword-fallback/cache/pgvector-seam) + acceptance B1–B5 + `reindex_kb` command | ✅ READY |
| **Full + expanded dashboard** | ADR-014 | n/a | `14-P4` (views, urls, models, payloads, AC A–H, test plan, publish mapping, `_clean_graph` boundary) | ✅ READY |

---

## 5. Contradiction / consistency findings (detail — all NON-blocking)

| ID | Sev | Finding | Where | Fix |
|---|---|---|---|---|
| **CN-1** | 🟡 MED | Dangling spec reference. `22-SPEC` §1.2/§0 twice cites `21-SPEC-webhook-contract.md`, which does not exist on disk. The webhook envelope + HMAC contract actually live in `10-P0` §4 + `23-SPEC-security-guardrails`; `21-SPEC` is the **budtender** contract. A fresh agent following `22`'s pointer hits a missing file. | `22-SPEC` §0, §1.2 | Repoint the two citations to `10-P0 §4` (envelope) + `23-SPEC` (HMAC), or rename — but DO NOT create a phantom `21-SPEC-webhook-contract.md`. One-line edits. |
| **CN-2** | 🟡 MED | Webhook field-name drift. The `tool-calls` payload uses `"toolCalls"` in `10-P0` §4.3 and `13-P3` §4.2, but the canonical test fixtures in `24-SPEC` §5.2 use `"toolCallList"`. Both are plausible Vapi shapes, but the dispatcher and the fixtures must agree or every `tool-calls` contract test mis-parses. | `10-P0` §4.3 / `13-P3` §4.2 vs `24-SPEC` §5.2 | Pin ONE field name (verify against the live Vapi server-message schema; the export JSON is the tiebreaker), update the other doc + fixtures in the same change. The webhook parser should tolerate both keys defensively, but the docs must name one canonical. |
| **CN-3** | 🟢 LOW | Stale ADR label. `11-P1` §3.4 and §9 instruct "record the choice as **ADR-021**" for the recognition decision, but `02-DECISIONS` already renumbered it to **ADR-022** (ADR-021 is Celery) and notes the supersede. `11`'s body was not updated. | `11-P1` §3.4/§9 | Replace "ADR-021" → "ADR-022" in `11-P1` (3 occurrences). `02` is already correct. |
| **CN-4** | 🟢 LOW | Stale layout/checklist. `01` §2 folder tree and roadmap §7 spec-checklist predate the modules/specs that shipped: `01` §2 omits `dashboard/models.py`, `dashboard/branding.py/publish.py/weights.py/monitor.py`, `voice/routing.py/corrections.py/analytics.py/tasks.py/provision.py/vendor_flow.py`, `core/celery.py`, `core/services/redact.py`, `voice/management/commands/*`, the `VapiObject` model, `kb/management/commands/reindex_kb.py`; roadmap §7 lists `16-EXP-CAPABILITY-EXPANSIONS.md` (disk: `16-CAPABILITY-EXPANSIONS.md`) and generic spec names. All additive; the doc-protocol (`03` §6) says reconcile in the same change a phase lands. | `01` §2, `00` §7 | Update `01` §2 + roadmap §7 as each phase/spec lands (per `03` §6). Trivial; not a blocker. |
| **CN-5** | 🟢 LOW | Provisioner module duality. `10-P0` references `tools/provision_vapi.py`; `20-SPEC` makes `voice/provision.py` + `manage.py provision_vapi` authoritative with `tools/provision_vapi.py` a 3-line shim. This is **explicitly reconciled** in `20-SPEC` §2/§9 — not a contradiction, but the executor must read `20-SPEC` to know the shim relationship. | `10-P0` §3.6 vs `20-SPEC` §2 | Already handled; ensure `10-P0` builds the shim form (or `voice/provision.provision_all(only_members=["faq"])`) so P0 and `20-SPEC` agree. No doc change strictly required. |

No ADR-vs-doc contradiction was found. The Celery decision (ADR-021), the recognition decision (ADR-022), the path-level HMAC gate, and the crisis-route carve-out are all authored.

---

## 6. Placeholder / TBD findings (detail)

- **PH-1 (resolved):** the prior BLOCKER (empty `10`–`13`, absent `20`–`24`) is GONE — all authored.
- **PH-2 (acceptable, backlog):** `16` E8/E19/E20 carry genuine technical unknowns (Dutchie write surface; pickup-order-status read) as Open-questions O-E8/O-E19 — correct for a *backlog* doc; must resolve before those items are scheduled (the doc says so).
- **PH-3 (intentional):** `15` `brand/tokens.json` `#______` hex pending O-10 capture — `provisional:true` + neutral fallback + `brand/CAPTURE.md` runbook. Not a defect.
- **PH-4 (intentional):** `22` `EducationDoc`/`BlogDoc` rows seed `provisional=True` (Vercel wall blocks verbatim copy); `seed.py` is re-runnable to upgrade. The FAQ/limits/returns/weights deliverable is fully confirmed-fact, not provisional.
- **PH-5:** no lazy `TBD`/`TODO`/`FIXME`/`???` markers in any of the 12 executable docs — only the legitimate env-placeholder pattern (O-*) and the research `[SITE]`/`[GENERAL]` provenance tags.

---

## 7. Dependency / sequencing sanity (the execution waves + the P0 fork-gate)

```
Wave 0:  P0 (serial, first)            ← 10-P0 ✅  (fork gate explicit in §0)
Wave 1:  P1 ∥ P2 ∥ P3 (parallel worktrees, disjoint files)  ← 11/12/13 ✅
Wave 2:  P4 (serial, after P1–P3)      ← 14-P4 ✅
Wave 3:  P5 (serial, last)             ← 15-P5 ✅
Wave 4:  EXP (independent backlog)     ← 16 ✅
Cross-cutting specs consumed throughout: 20 (provision) · 21 (budtender) · 22 (KB) · 23 (security) · 24 (testing) ✅
```

**Soundness:** strong and now fully populated.
- **The P0 fork-gate is explicit and correct** (`10-P0` §0): P1/P2/P3 do not fork until (a) `voice/webhooks.py` dispatches all 4 event kinds with the §4 frozen shapes, (b) `core/services/vapi.py` GET/POST/PATCH is merged, (c) `voice/tools/__init__.py::TOOL_REGISTRY` + `register()` is merged, (d) the P0 acceptance passes. This is the single most important sequencing constraint and it is pinned.
- **Disjoint-file parallelism holds:** P1 = `budtender_client.py` + `tools/suggest.py` + `recognition.py`; P2 = `webhooks.py` eocr branch + `crm/sinks.py` + escalation prompt; P3 = `tools/vendor.py` + `vendor_flow.py` + `crm/models.VendorCallback`. The only shared-file touch (`voice/tools/__init__.py`) is mitigated to two one-line `from . import …` appends (ADR-020). P1 further prefers resolving recognition *lazily inside `suggest.py`* to avoid touching `webhooks.py` at all.
- **`20-SPEC` resolves the provisioner-name duality** (CN-5) and `21-SPEC` resolves the phone-hash boundary (ADR-022) — the two seams most likely to cause a parallel-merge collision are pre-resolved.

**Sequencing defects:** none blocking. The only residue is the citation drift (CN-1/CN-4) — pointers that name a file/module slightly off from reality, which an executor reconciles in-flight.

---

## 8. Prioritized gap list (residue — NONE are execution blockers)

| ID | Severity | Gap | Where | Fix |
|---|---|---|---|---|
| **R-1** | 🟡 MED | Dangling `21-SPEC-webhook-contract.md` reference (CN-1). | `22-SPEC` §0/§1.2 | Repoint to `10-P0 §4` + `23-SPEC`. 2 one-line edits. Do before P0 executes so the executor isn't sent to a missing file. |
| **R-2** | 🟡 MED | `tool-calls` field-name drift `toolCalls` vs `toolCallList` (CN-2). | `10-P0`/`13-P3` vs `24-SPEC` | Pin one against the live Vapi schema/export; make the parser tolerate both; align the fixtures. Do during P0 (it owns the webhook parser). |
| **R-3** | 🟢 LOW | `11-P1` cites ADR-021 for recognition; canonical is ADR-022 (CN-3). | `11-P1` §3.4/§9 | s/ADR-021/ADR-022/ (3×). |
| **R-4** | 🟢 LOW | `01` §2 layout + roadmap §7 checklist stale vs shipped modules/specs (CN-4). | `01` §2, `00` §7 | Reconcile per phase per `03` §6; add `23`/`24` to §7; fix the `16-EXP` filename. |
| **R-5** | 🟢 LOW | Read-order in `00` §1 / phase docs implies a `21-SPEC-webhook-contract` and a `23`/`24` "reserved" status that is now obsolete (they're authored). | `00` §1, `22` §0 | Update the "reserved" note; `23`/`24` are live. |
| **R-6** | 🟢 LOW (carry to build) | EXP open-qs O-E8/O-E19 (Dutchie write/read surface) are genuinely unresolved — fine for backlog, must resolve before scheduling E8/E19. | `16` §6 | Owner decision when those items are scheduled. Not P0–P5. |

**There is no 🔴 BLOCKER.** The prior G-1…G-4 blockers are closed; the `20`–`24` specs (prior G-5) are authored and exceed the recommended depth.

---

## 9. What is genuinely good (preserve, do not "fix")

- **The foundation (`00`–`03`) remains exemplary** — 7-subsystem decomposition, the 4-plane mental model, an ADR log with rationale+consequences (now 22 ADRs incl. Celery + recognition), the full env catalog, the testing planes, and the Numbers-Guard/Leak-Guard rails.
- **`20-SPEC-vapi-deploy` is a standout** — a single idempotent `manage.py provision_vapi`, the `VapiObject` zero-drift registry with a `last_provision_hash` oracle, the create-or-PATCH-by-id-then-by-name reconcile, secret redaction, the no-`/workflow` guard, and the escalation-orphan-fixed-by-construction `SQUAD_SHAPE`. The shared `build_*_payload` builders (one shape, two callers: provision + P4 publish) eliminate the worst drift risk.
- **`21-SPEC-budtender-contract` does the hard thing** — it does not paper over the phone-hash-vs-raw-phone key mismatch; it diagnoses it against the real budtender code (`views._profile_for_phone` keys on the normalized raw phone, not the peppered hash) and resolves it (Option A + ADR-022 + four additive budtender TODOs) with a contract test.
- **`22-SPEC-kb-seed` writes the KB out concretely** — every literal FAQ/policy/store-fact/limit/taxonomy/education/blog row, the WAC-314-55-079 body verbatim, the O-8 Mt-Vernon "call to confirm" stub, taxonomy parity with budtender, and a self-consistency cross-check vs P0 (§11).
- **`23-SPEC-security-guardrails` is a real security layer** — HMAC fail-closed (both modes, constant-time), prod-fail-closed boot extended with a `DUTCHIE_*_POS_KEY`-in-voice-repo veto, the three-layer leak guarantee (budtender allowlist → `scrub_leak` central choke point → contract test), the age gate as a *code* boundary (products withheld until confirmed, not a prompt line), the medical/scope vetoes with a **crisis→escalation carve-out** (911/988, not a stonewall), and a 12-row threat table.
- **`24-SPEC-testing` is the master index it claims to be** — six planes mapped to pytest lanes, hand-authored deterministic fixtures, the leak-bait "proof-of-strip" pattern, five golden voice scenarios with hand-authored expected dicts, the two mandatory gates re-run per phase, the CI gate order with blocking semantics, and a §12 enumeration of every named test across every phase/spec.
- **The security spine is uniform and code-owned** — Leak-Guard, Numbers-Guard, OTD, HMAC-fail-closed, per-store-keys-only-in-budtender, prod-fail-closed, guardrails-cannot-be-deleted-from-the-UI hold across all 12 docs.

---

## 10. GO / NO-GO for Wave 0 (P0)

**GO.** ✅

`10-P0-CHASSIS-FAQ.md` is authored to the full executable standard, its fork-gate (§0) is explicit, its deep dependencies (`20-SPEC` Vapi client/provision, `22-SPEC` KB content/embeddings, `23-SPEC` security/HMAC) are all authored and cross-checked, and its acceptance criteria (A–H) plus the 4-plane test plan are concrete and gated by the two mandatory cross-cutting tests (Leak-Guard, HMAC-fail-closed). Nothing P0 needs is missing or undecided.

**Before merging P0 (do these as part of the P0 change, since P0 owns the surfaces they touch — none blocks *starting*):**
1. **R-2 — pin the `tool-calls` field name** (`toolCalls` vs `toolCallList`) against the live Vapi schema/export and make the P0 webhook parser tolerate both; align `24-SPEC` fixtures. P0 builds the parser, so resolve it here.
2. **R-1 — fix the dangling `21-SPEC-webhook-contract.md` pointer** in `22-SPEC` (repoint to `10-P0 §4` + `23-SPEC`) so the executor following the KB spec isn't sent to a missing file.

**Trivia to sweep opportunistically (not gating):** R-3 (ADR-021→022 in `11-P1`), R-4 (`01` §2 + roadmap §7 reconcile), R-5 (drop the "`23`/`24` reserved" note).

After P0 lands green (fork-gate satisfied), dispatch **P1 ∥ P2 ∥ P3** in parallel worktrees exactly as designed, then P4 (serial), P5 (serial), EXP (independent).

---

*End of review. The plan set is now fully authored, internally consistent on every binding decision, and execution-ready end-to-end. The four prior BLOCKERs are closed and the `2X` specs exceed the recommended depth. Remaining items are cross-reference cleanups (a dangling spec filename, a webhook field-name to pin, two stale labels) — fix R-1/R-2 inside the P0 change; none blocks starting Wave 0. **GO.***
