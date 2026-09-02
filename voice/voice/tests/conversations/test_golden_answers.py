"""The golden answer set, offline: every entry × every offline channel, plus the guard tests that
keep the set honest (no fact literals; every policy category and every caller-facing tool has an
entry). Live channels (voice / web) run from ``manage.py eval_answers --live``.

Built on the conversations harness: the REAL seeded KB and the ``FakeBudtender`` catalog.
"""

from __future__ import annotations

import re

import pytest

from voice.evals import adapters, golden, run, score

ENTRIES = golden.load()
OFFLINE = list(adapters.OFFLINE_CHANNELS)
CASES = [(e, ch) for e in ENTRIES for ch in run.channels_for(e, OFFLINE)]


@pytest.mark.django_db
@pytest.mark.parametrize("entry,channel", CASES, ids=[f"{e.id}[{ch}]" for e, ch in CASES])
def test_golden_entry(entry, channel, seeded_kb, fake_bt):
    answer = run.ask(entry, channel)
    if not answer.applicable:
        pytest.skip(f"{channel} has no surface for {entry.id}")
    result = score.score(entry, answer)
    assert result.passed, f"{entry.id} on {channel}: {result.failures}\n  answer: {answer.text[:300]}"


@pytest.mark.django_db
def test_offline_pass_rate_and_consistency(seeded_kb, fake_bt):
    """The roll-up the owner reads: ≥95% per channel, consistency over the whole set."""
    results = run.run(ENTRIES, OFFLINE)
    stats = score.rollup(results)
    report = score.render(ENTRIES, results, title="offline")
    for ch, s in stats.items():
        assert s.pass_pct >= 95.0, f"{ch} at {s.pass_pct}%\n{report}"
    agree, total, bad = score.consistency(ENTRIES, results)
    assert agree >= total - 2, f"consistency {agree}/{total}, disagree: {bad}\n{report}"


# ── guards on the golden file itself ─────────────────────────────────────────

_HOUR_LITERAL = re.compile(r"\d{1,2}(?::\d{2})?\s*(?:AM|PM)", re.I)
_PRICE_LITERAL = re.compile(r"\$\s?\d")
_PHONE_LITERAL = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


def test_golden_has_no_fact_literals():
    """An hour, a price, or a phone number written into the golden file is a second source."""
    for e in ENTRIES:
        for pat in e.must_include + e.must_not_include:
            if "{{" in pat:
                continue
            assert not _HOUR_LITERAL.search(pat), f"{e.id}: hour literal in {pat!r}"
            assert not _PRICE_LITERAL.search(pat), f"{e.id}: price literal in {pat!r}"
            assert not _PHONE_LITERAL.search(pat), f"{e.id}: phone literal in {pat!r}"


def test_golden_size_and_spread():
    assert len(ENTRIES) >= 40
    per = {}
    for e in ENTRIES:
        per[e.category] = per.get(e.category, 0) + 1
    thin = {c: n for c, n in per.items() if n < 3}
    assert not thin, f"categories with fewer than 3 entries: {thin}"
    assert set(per) == set(golden.CATEGORIES), f"missing categories: {set(golden.CATEGORIES) - set(per)}"


@pytest.mark.django_db
def test_every_policy_category_has_an_entry(seeded_kb):
    from kb.models import PolicyCategory

    covered = " ".join(e.source_of_truth for e in ENTRIES)
    missing = [
        c.slug for c in PolicyCategory.objects.filter(is_active=True)
        if f"PolicyCategory({c.slug})" not in covered and f"PolicyDocument({c.slug})" not in covered
    ]
    assert not missing, f"PolicyCategory rows with no golden entry: {missing}"


def test_every_caller_facing_tool_has_an_entry():
    from voice.tools import TOOL_REGISTRY

    expected = {t for e in ENTRIES for t in e.expect_tools}
    missing = set(TOOL_REGISTRY) - expected - run.INTERNAL_TOOLS
    assert not missing, f"tools registered but never expected by a golden entry: {sorted(missing)}"


@pytest.mark.django_db
def test_every_template_resolves(seeded_kb):
    """A template that points at a row that no longer exists must fail loudly, not score as a miss."""
    for e in ENTRIES:
        for pat in e.must_include + e.must_not_include:
            if "{{" in pat:
                golden.resolve(pat)
