"""voice/budtender_client.py ranking-weights wire (14-P4 EXPANSIONS item 1).

The owner-tuned ``RankingWeights`` singleton (dashboard) is forwarded as the per-request
``ranking_weights`` config on EVERY ``products/search/`` call so the owner's "high margin first" /
taste levers reach budtender's ranker per call. Binding behavior:

  * OMITTED when the owner has not changed anything off budtender's baseline (zero behavior change
    until a lever is actually tuned) → existing P1 suggestion goldens stay byte-identical.
  * SENT (``{w_anon, w_known, margin_emphasis}``) the moment the owner tunes a lever.
  * FAIL-SAFE: a DB error / missing singleton must NEVER crash the turn — the param is just omitted.

Offline, SQLite, no network — the HTTP layer is a record/replay ``FakeSession``.
"""

from __future__ import annotations

import pytest

from voice.budtender_client import BudtenderClient

from .test_budtender_client import BASE, TOKEN, FakeSession


def _client(session):
    c = BudtenderClient(base_url=BASE, token=TOKEN, timeout=4)
    c._session = session
    return c


@pytest.mark.django_db
def test_default_weights_omit_ranking_param():
    """A fresh (un-tuned) singleton == budtender's baseline → the param is OMITTED."""
    from dashboard.models import RankingWeights

    RankingWeights.load()  # seed the byte-identical-to-budtender defaults
    fs = FakeSession(default={"results": []})
    _client(fs).search({"store": "yakima", "category": "flower"})
    assert "ranking_weights" not in fs.calls[0]["body"]


@pytest.mark.django_db
def test_tuned_weights_are_forwarded_per_request():
    """Once the owner tunes a lever, EVERY search carries the ranking_weights config."""
    from dashboard.models import RankingWeights

    w = RankingWeights.load()
    w.w_anon = {"margin": 0.8, "effect": 0.2}  # owner cranks the margin lever
    w.margin_emphasis = 1.5
    w.save()

    fs = FakeSession(default={"results": []})
    _client(fs).search({"store": "yakima", "category": "flower"})
    body = fs.calls[0]["body"]
    assert "ranking_weights" in body
    rw = body["ranking_weights"]
    assert rw["w_anon"] == {"margin": 0.8, "effect": 0.2}
    assert rw["margin_emphasis"] == 1.5
    assert "w_known" in rw  # the full config is sent (budtender selects anon vs known by phone)


@pytest.mark.django_db
def test_margin_emphasis_alone_triggers_send():
    """Tuning ONLY the margin-emphasis knob (weights untouched) still forwards the config."""
    from dashboard.models import RankingWeights

    w = RankingWeights.load()
    w.margin_emphasis = 2.0
    w.save()
    fs = FakeSession(default={"results": []})
    _client(fs).search({"store": "yakima", "category": "flower"})
    assert fs.calls[0]["body"]["ranking_weights"]["margin_emphasis"] == 2.0


def test_no_db_read_is_fail_safe():
    """No DB access in this test → the lazy RankingWeights read fails → the param is omitted, and
    the search still issues (the turn is never crashed by a weights-read failure)."""
    fs = FakeSession(default={"results": []})
    out = _client(fs).search({"store": "yakima", "category": "flower"})
    assert out == {"results": []}
    assert "ranking_weights" not in fs.calls[0]["body"]  # omitted, not crashed


@pytest.mark.django_db
def test_as_request_config_shape():
    """The model serializer emits the exact {w_anon, w_known, margin_emphasis} contract."""
    from dashboard.models import DEFAULT_W_ANON, DEFAULT_W_KNOWN, RankingWeights

    cfg = RankingWeights.load().as_request_config()
    assert cfg == {
        "w_anon": DEFAULT_W_ANON,
        "w_known": DEFAULT_W_KNOWN,
        "margin_emphasis": 1.0,
    }
